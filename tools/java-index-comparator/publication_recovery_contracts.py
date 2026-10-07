"""Late Java generation handoff I/O: internal CLI/state, not MCP truth."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from publication_error_contracts import PROFILES, FORMATS, POSITIONS, SOURCES, INVENTORY
from root_contracts import Runner

FEATURE = 'global:format:java-publication-recovery'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: late preparing/commit marker '
          'write, file sync, install and directory sync; failed rollback and committed '
          'housekeeping, blocked readers and retry recovery across Java rebuild variants, '
          'restore and clear with/without a prior generation, JSON/text and flag positions; '
          'bounded complete inventory; not MCP equivalence')
MARKER_PHASES = tuple(f'{marker}-{phase}' for marker in ('state', 'commit')
                      for phase in ('write', 'file-sync', 'install', 'installed-sync', 'directory-sync'))
ROLLBACK_PHASES = ('rollback-remove', 'rollback-rename', 'recovery-directory-sync',
                   'recovery-staging', 'recovery-state-remove', 'state-remove-sync')
COMMITTED_PHASES = ('recovery-swap-remove', 'recovery-directory-sync', 'recovery-staging',
                    'recovery-state-remove', 'recovery-commit-remove', 'state-remove-sync', 'commit-remove-sync')
SCENARIOS = {phase: [phase] for phase in MARKER_PHASES}
SCENARIOS.update({f'rollback:{phase}': ['commit-write', phase] for phase in ROLLBACK_PHASES})
SCENARIOS.update({f'committed:{phase}': [phase] for phase in COMMITTED_PHASES})
STAGING_PHASES = ('staging-owner-remove',
                  'staging-directory-remove', 'staging-directory-sync')
SCENARIOS.update({f'committed:{phase}': [phase] for phase in STAGING_PHASES})
SCENARIOS['staging-failed:staging-db-remove'] = ['state-write', 'staging-db-remove']
SCENARIOS['positive'] = []


def samples():
    for prior in (False, True):
        for profile in PROFILES:
            for scenario, phases in SCENARIOS.items():
                # These actions need a recorded old snapshot. Missing generation
                # controls still execute every other late phase, including clear.
                if profile == 'clear' and any(phase in (*STAGING_PHASES, 'staging-db-remove') for phase in phases):
                    continue  # Clear has no staged directory; the clear lifecycle still executes.
                if not prior and ('rollback-rename' in phases or 'recovery-swap-remove' in phases):
                    continue
                for fmt in FORMATS:
                    for suffix in POSITIONS:
                        yield prior, profile, scenario, phases, fmt, suffix


def acceptance_keys():
    return {'inventory', 'applicable-java', 'source-preservation', 'backup-preservation'} | {
        f'{prior}:{profile}:{scenario}:{fmt}:{suffix}'
        for prior, profile, scenario, _, fmt, suffix in samples()}


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= section.keys() and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def plan_errors(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-publication-recovery'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java late publication fixture')=0",
                      ('; Java late publication fixture executes marker write/file-sync/install/'
                       'installed-sync/directory-sync, rollback remove/rename and retry, committed '
                       'swap/staging/marker cleanup and retry, prior/absent generations, all rebuild '
                       'profiles/restore/clear and JSON/text flag positions; simultaneous marker '
                       'invalidation I/O and persistent filesystem failure durability require '
                       'additional evidence; notification backend/channel and incremental moved-path '
                       'protocol failures and other unresolved parent criteria remain pending',))


def exercise(binary, base):
    # Each lane owns its DB, locks, sources and logs. Keep the full acceptance
    # matrix while bounding concurrency and avoiding serial repetition of fsync.
    cases = list(samples())
    lanes = [cases[index::4] for index in range(4)]
    expected, actual = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for wanted, observed in pool.map(lambda lane: _exercise(binary, base, lane), lanes):
            for key, want in wanted[FEATURE].items():
                got = observed[FEATURE][key]
                if key in expected and expected[key] != want:
                    raise ToolError('publication lane acceptance disagrees')
                if key not in actual or actual[key] == expected[key]:
                    actual[key] = got
                expected[key] = want
    return {FEATURE: expected}, {FEATURE: actual}


def _exercise(binary, base, cases):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('publication recovery fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='publication-recovery-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    for name, source in SOURCES.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    database = directory / 'index.sqlite'
    control = directory / 'fault.json'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got
        if len(expected) % 16 == 0 or key == 'source-preservation':
            pending = directory / 'results.next.json'
            pending.write_text(json.dumps({'expected': expected, 'actual': actual}))
            pending.replace(directory / 'results.json')

    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if inventory != INVENTORY:
            raise ToolError('publication recovery fixture full inventory incomplete')
        record('inventory', INVENTORY, inventory)
        record('applicable-java', True, any(ext == '.java' for ext in inventory.values()))
    finally:
        state.close()
    runner.command('rebuild', '--force')
    backup = directory / 'backup.sqlite'
    source = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    backup_hash = file_sha256(backup)
    replacement = 'class ProbeNew { void pong() {} }\n'
    (runner.root / 'Probe.java').write_text(replacement)

    def reset(prior):
        for suffix in ('', '-wal', '-shm', '-journal', '.swap', '.swap-pending',
                       '.publish-state-v1', '.publish-commit-v1'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        for path in directory.iterdir():
            if path.name.startswith(('.rebuild-', '.restore-')) and path.is_dir():
                shutil.rmtree(path)
            elif path.name.startswith('.owner-manifest.') and path.is_file():
                path.unlink()
        if prior:
            shutil.copyfile(backup, database)

    def logical_state():
        if not database.exists():
            return None
        digest = hashlib.sha256()
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            for statement in connection.iterdump():
                digest.update((statement + '\n').encode())
        finally:
            connection.close()
        return digest.hexdigest()

    def names():
        if not database.exists():
            return []
        try:
            return sorted(row['name'] for row in runner.json('class', '--pattern', '*', '--limit', 100)['items'])
        except ToolError:
            return ['<reader blocked>']

    def clean():
        return not any(path.name.startswith(('.rebuild-', '.restore-', '.owner-manifest.')) or
                       path.name.startswith(database.name + '.swap') or
                       path.name.startswith(database.name + '.publish-') for path in directory.iterdir())

    def stderr():
        path = directory / f'{runner.sequence:03d}.stderr.log'
        with path.open('rb') as stream:
            value = stream.read(runner.output_budget + 1)
        if len(value) > runner.output_budget:
            raise ToolError('publication recovery diagnostic exceeded budget')
        return value.decode('utf-8')

    for prior, profile, scenario, phases, fmt, suffix in cases:
        reset(prior)
        previous_state = logical_state()
        control.write_text(json.dumps({'database': str(database), 'phases': phases, 'reached': []}))
        args = [*PROFILES[profile], *([str(backup)] if profile == 'restore' else [])]
        flags = ['--format', fmt]
        code, output = runner.command(*(args + flags if suffix else flags + args), acceptable=(0, 1, 2),
                                      environment={'AST_INDEX_TEST_PUBLICATION_FAULT_FILE': str(control)})
        error = stderr()
        hits = json.loads(control.read_text())
        terminal = any(line.startswith(('Indexed ', 'Done:', 'Restored index from:',
                                        'Index cleared for ', 'Persisted --force')) for line in output.splitlines())
        committed = scenario.startswith('committed:') or scenario == 'positive'
        old = ['Peer', 'ProbeOld'] if prior else []
        new = [] if profile == 'clear' else (['Peer', 'ProbeNew'] if profile in {
            'all', 'files', 'symbols', 'fast', 'sub-projects', 'sub-projects-fast', 'remember'
        } else (['Peer', 'ProbeOld'] if prior or profile == 'restore' else []))
        # Durable decision markers block reads until recovery. Once the last
        # marker is removed, a sync failure may leave a safe readable generation. A failed
        # command must not advertise a complete result, even post-commit.
        recovering = scenario.startswith(('rollback:', 'committed:', 'staging-failed:'))
        # Full schema/row retention complements authored declarations: partial
        # module rebuilds and restore may keep the same class names while still
        # publishing a different generation. This is internal atomicity evidence,
        # independent of (and never counted as) MCP equivalence.
        committed_state = logical_state() if committed and recovering else None
        if recovering:
            read_code, read_output = runner.command('--format', 'json', 'class', '--pattern', '*', '--limit', 100, acceptable=(0, 1, 2))
            reader_safe = read_code == 1 and read_output == ''
            if scenario in ('rollback:state-remove-sync', 'committed:commit-remove-sync') or scenario.startswith('staging-failed:'):
                try:
                    read_items = sorted(row['name'] for row in json.loads(read_output)['items'])
                    reader_safe = read_code == 0 and read_items == (new if committed else old)
                except (ValueError, KeyError, TypeError):
                    reader_safe = (not (new if committed else old) and not database.exists()
                                   and read_code == 1 and read_output == ''
                                   and 'Index not found' in stderr())
            # A no-subproject rebuild recovers under its publication guard and
            # returns before allocating any replacement. This observes recovery
            # without a later restore overwriting the generation being tested.
            retry_code, retry_output = runner.command('--format', 'json', 'rebuild', '--force',
                '--sub-projects', '--include', '__absent__/**', acceptable=(0, 1, 2),
                environment={'AST_INDEX_TEST_PUBLICATION_FAULT_FILE': str(control)})
            try:
                retried = retry_code == 0 and json.loads(retry_output)['status'] == 'no-sub-projects'
            except (ValueError, KeyError, TypeError):
                retried = False
        else:
            reader_safe = True
            retried = True
        try:
            success_document = (fmt != 'json' or scenario != 'positive' or
                                (json.loads(output)['command'] == args[0] and
                                 json.loads(output)['status'] == 'complete'))
        except (ValueError, KeyError, TypeError):
            success_document = False
        got = {'success-document': success_document, 'exit': code, 'no-success': not terminal if scenario != 'positive' else terminal or fmt == 'json',
               'json-empty': fmt != 'json' or output == '' if scenario != 'positive' else True,
               'faults-reached': hits['reached'] == phases and hits['phases'] == [],
               'diagnostic': bool(error.strip()) if scenario != 'positive' else True,
               'reader-safe': reader_safe, 'retry-recovered': retried,
               'identities': names(), 'clean': clean(),
               'generation-state-preserved': (logical_state() ==
                    (committed_state if committed else previous_state)) if recovering or not committed else True}
        want = {'success-document': True, 'exit': 0 if scenario == 'positive' else 1, 'no-success': True,
                'json-empty': True, 'faults-reached': True, 'diagnostic': True,
                'reader-safe': True, 'retry-recovered': True, 'identities': new if committed else old, 'clean': True, 'generation-state-preserved': True}
        record(f'{prior}:{profile}:{scenario}:{fmt}:{suffix}', want, got)
    record('backup-preservation', True, backup_hash == file_sha256(backup))
    record('source-preservation', {**SOURCES, 'Probe.java': replacement},
           {name: (runner.root / name).read_text() for name in SOURCES})
    return {FEATURE: expected}, {FEATURE: actual}
