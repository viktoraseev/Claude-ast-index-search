"""Java watch/Git failure composition; internal CLI/source checks, not MCP truth."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner
from vcs_contracts import History
from publication_error_contracts import held

VCS = 'global:format:java-vcs-errors'
WATCH = 'global:format:java-watch-errors'
FEATURES = {VCS, WATCH}
REASON = ('internal CLI and independent source/state: disposable Java watch lock/startup '
          'and update failure recovery; Git subprocess/protocol failure composition, '
          'history transaction preservation and authored positive controls; complete '
          'file-type inventory; not MCP equivalence')
FORMATS = ('text', 'json')
POSITIONS = (False, True)
INVENTORY = {'Probe.java': '.java', 'Inventory.kt': '.kt', 'inventory.xml': '.xml'}
SOURCES = {'Probe.java': 'class Probe { int value = 1; }\n',
           'Inventory.kt': '// inventory only\n', 'inventory.xml': '<fixture/>\n'}
COMMON = ('missing-executable', 'unusable-executable', 'exit', 'timeout', 'no-repository')
CHANGED = (*COMMON, 'missing-ref', 'unsafe-base', 'invalid-symbolic-ref',
           'bad-status', 'missing-path', 'missing-rename', 'unsafe-path',
           'invalid-utf8', 'missing-nul', 'overflow', 'positive')
HISTORY = (*COMMON, 'no-commits', 'bad-count', 'bad-head', 'short-graph', 'bad-graph-sha',
           'bad-graph-parent', 'duplicate-graph', 'bad-graph-timestamp',
           'short-log', 'bad-header-sha', 'bad-header-timestamp', 'short-header',
           'bad-numstat', 'negative-numstat', 'overflow-numstat', 'mixed-binary',
           'short-numstat', 'short-rename', 'empty-rename', 'missing-nul',
           'orphan-stat', 'duplicate-log', 'foreign-log', 'write-failure', 'positive')
WATCH_START = ('lock-io', 'damaged-db', 'missing-index', 'idle', 'idle-quiet', 'positive')
WATCH_RUNTIME = ('mutation-busy', 'sql-failure', 'cleared-index')


def acceptance_keys(feature):
    keys = {'inventory', 'applicable-java', 'source-preservation'}
    if feature == VCS:
        keys |= {f'{profile}:{case}:{fmt}:{suffix}' for profile, cases in
                 (('changed', CHANGED), ('collect', HISTORY), ('full', HISTORY))
                 for case in cases for fmt in FORMATS for suffix in POSITIONS}
        keys |= {'history-control', 'literal-rename-paths', 'separator-subject', 'binary-stat'}
    else:
        keys |= {f'{profile}:{case}:{fmt}:{suffix}' for profile in ('watch', 'watch-status')
                 for case in WATCH_START for fmt in FORMATS for suffix in POSITIONS}
        keys |= {f'runtime:{case}:{fmt}:{suffix}' for case in WATCH_RUNTIME
                 for fmt in FORMATS for suffix in POSITIONS}
    return keys


def acceptance_complete(feature, expected, actual):
    return all(acceptance_keys(feature) <= section.keys()
               and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def declarations_ready(runner, name):
    # Watch can publish a replacement after a clear. Its readiness poll must
    # defer the documented transient rejection without hiding other failures.
    code, output = runner.command('--format', 'json', 'class', name, acceptable=(0, 1))
    with (runner.directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
        diagnostic = stream.read(runner.output_budget + 1)
    if len(diagnostic) > runner.output_budget:
        raise ToolError('watch readiness diagnostic exceeded its budget')
    if code:
        lock = Path(runner.environment['AST_INDEX_DB_PATH']).with_suffix('.publish.lock')
        busy = f'Error: index generation is being published or recovered; retry shortly ({lock})'
        if output == '' and diagnostic.strip() == busy.encode('utf-8'):
            return False
        raise ToolError('watch readiness command failed; see private fixture logs')
    try:
        return bool(json.loads(output)['items'])
    except (ValueError, KeyError, TypeError) as error:
        raise ToolError('watch readiness expected class JSON; see private fixture logs') from error


def plan_errors(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-watch-vcs-errors-v1'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java watch/Git error fixture')=0",
                      ('; Java watch/Git error fixture executes startup locks/index errors, '
                       'update contention/SQL/cleared-index recovery, Git process and '
                       'history protocol failures with transaction preservation; notification '
                       'backend/channel failures, incremental moved-path protocol failures, '
                       'restore late-marker/rollback/recovery I/O and foreground/background '
                       'update/graph refresh failures remain pending',))


# This provider mutates no configuration. It delegates to the fixture's real
# Git, replacing exactly one response to exercise production protocol readers.
PROVIDER = r'''
import os, subprocess, sys, time
args = sys.argv[1:]
mode = os.environ.get('FIXTURE_FAULT', '')
cmd = args[0]
if mode == 'exit': sys.exit(23)
if mode == 'timeout': time.sleep(2); sys.exit(0)
if mode == 'invalid-symbolic-ref' and cmd == 'symbolic-ref':
    sys.stdout.buffer.write(b'-unsafe\n'); sys.exit(0)
if mode == 'no-commits' and cmd == 'rev-parse': sys.exit(1)
if mode == 'bad-head' and cmd == 'rev-parse':
    sys.stdout.buffer.write(b'not-a-commit\n'); sys.exit(0)
if mode == 'bad-count' and cmd == 'rev-list' and '--count' in args:
    sys.stdout.buffer.write(b'not-a-count\n'); sys.exit(0)
result = subprocess.run([os.environ['FIXTURE_GIT'], *args], capture_output=True)
data = result.stdout
if cmd == 'diff':
    data = {'bad-status': b'X\0Probe.java\0', 'missing-path': b'M\0',
            'missing-rename': b'R100\0Probe.java\0', 'unsafe-path': b'M\0../Probe.java\0',
            'invalid-utf8': b'M\0\xff.java\0', 'missing-nul': b'M\0Probe.java'}.get(mode, data)
    if mode == 'overflow': data = b' ' * (17 * 1024 * 1024)
if cmd == 'rev-list' and '--parents' in args:
    lines = data.splitlines()
    if mode == 'short-graph': data = b'broken\n'
    if lines:
        fields = lines[0].split()
        if mode == 'bad-graph-sha': fields[1] = b'not-a-hash'
        if mode == 'bad-graph-parent': fields.append(b'not-a-parent')
        if mode == 'bad-graph-timestamp': fields[0] = b'bad-time'
        if mode.startswith('bad-graph-'): data = b' '.join(fields) + b'\n' + b'\n'.join(lines[1:]) + b'\n'
        if mode == 'duplicate-graph': data = b'\n'.join([lines[0]] * len(lines)) + b'\n'
if cmd == 'log' and data:
    header = data.split(b'\0', 1)[0]
    fields = header.split(b'\x1f', 4)
    if mode == 'bad-header-sha': fields[0] = b'\x01not-a-hash'
    if mode == 'bad-header-timestamp': fields[1] = b'bad-time'
    if mode == 'foreign-log': fields[0] = b'\x01' + b'f' * 40
    changed = b'\x1f'.join(fields)
    faults = {'short-log': b'', 'short-header': b'\x01' + b'a'*40 + b'\0',
              'bad-numstat': changed+b'\0\nwrong\t0\tProbe.java\0',
              'negative-numstat': changed+b'\0\n-1\t0\tProbe.java\0',
              'overflow-numstat': changed+b'\0\n9223372036854775808\t0\tProbe.java\0',
              'mixed-binary': changed+b'\0\n-\t0\tProbe.java\0',
              'short-numstat': changed+b'\0\n1\tProbe.java\0',
              'short-rename': changed+b'\0\n0\t0\t\0Old.java\0',
              'empty-rename': changed+b'\0\n0\t0\t\0\0New.java\0',
              'orphan-stat': b'1\t0\tProbe.java\0'+data,
              'missing-nul': data.rstrip(b'\0'),
              'duplicate-log': (header+b'\0')*len([p for p in data.split(b'\0') if p.startswith(b'\x01')])}
    data = faults.get(mode, data)
    if mode in ('bad-header-sha', 'bad-header-timestamp', 'foreign-log'):
        data = changed+b'\0'+data.split(b'\0',1)[1]
sys.stdout.buffer.write(data)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
'''


def exercise(binary, base, features=FEATURES):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('watch/VCS fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='watch-vcs-errors-', dir=base)).resolve())
    runner.root.mkdir()
    database = runner.directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    for name, source in SOURCES.items():
        (runner.root / name).write_text(source)
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        files = dict(inventory.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if files != INVENTORY:
            raise ToolError('watch/VCS fixture full inventory incomplete')
    finally:
        inventory.close()
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got
        checkpoint = runner.directory / 'results.next.json'
        checkpoint.write_text(json.dumps({'expected': expected, 'actual': actual}))
        checkpoint.replace(runner.directory / 'results.json')

    for feature in FEATURES:
        record(feature, 'inventory', INVENTORY, files)
        record(feature, 'applicable-java', True, any(ext == '.java' for ext in files.values()))
    history = History(runner)
    history.git('init', '-q', '--template=', '--initial-branch=main')
    history.commit(SOURCES)
    history.commit({'Probe.java': 'class Probe { int value = 2; }\n'}, fix=True)
    runner.command('rebuild', '--force')
    runner.json('hotspots', '--collect')
    rows = runner.json('hotspots')['items']
    probe = next(row for row in rows if row['path'] == 'Probe.java')
    record(VCS, 'history-control', [2, 2, 1, 1],
           [probe[k] for k in ('commits', 'lines_added', 'lines_deleted', 'fix_commits')])
    baseline = runner.directory / 'collected.sqlite'
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(baseline)) as target:
        source.backup(target)
    # Both collection modes see two real new commits. --collect must exercise
    # incremental planning, not an invalid cursor's fallback to a full scan.
    history.commit({'Probe.java': 'class Probe { int value = 3; }\n'})
    history.commit({'Probe.java': 'class Probe { int value = 4; }\n'})
    authored_head = history.git('rev-parse', 'HEAD')
    original_hashes = {name: file_sha256(runner.root / name) for name in SOURCES}

    def reset_history():
        with closing(sqlite3.connect(baseline)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
    provider = runner.directory / 'git-provider'
    provider.write_text('#!' + sys.executable + '\n' + PROVIDER)
    provider.chmod(0o700)
    runner.environment.update(FIXTURE_GIT=shutil.which('git'), GIT_CEILING_DIRECTORIES=str(runner.directory))
    before_db = None

    def state():
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as connection:
            return tuple(connection.iterdump())

    def invoke(args, fmt, suffix, env=None):
        flags = ['--format', fmt]
        code, output = runner.command(*(args + flags if suffix else flags + args),
                                      environment=env, acceptable=(0, 1, 2, 101))
        error = (runner.directory / f'{runner.sequence:03d}.stderr.log').read_bytes()
        if len(error) > runner.output_budget:
            raise ToolError('watch/VCS diagnostic exceeded its budget')
        return code, output, error

    for profile, cases in ((('changed', CHANGED), ('collect', HISTORY), ('full', HISTORY))
                           if VCS in features else ()):
        for case in cases:
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    args = (['changed', '--base', 'main'] if profile == 'changed' else
                            ['hotspots', '--full', '--window', 100] if profile == 'full' else
                            ['hotspots', '--collect', '--window', 100])
                    env = {'AST_INDEX_VCS_BIN': str(provider), 'FIXTURE_FAULT': case}
                    if case == 'missing-executable': env['AST_INDEX_VCS_BIN'] = str(runner.directory / 'absent')
                    if case == 'unusable-executable': env['AST_INDEX_VCS_BIN'] = str(runner.directory)
                    if case == 'missing-ref': args[-1] = 'missing-fixture-ref'
                    if case == 'unsafe-base': args = ['changed', '--base=-unsafe']
                    if case == 'invalid-symbolic-ref': args = ['changed']
                    if case == 'timeout': args += ['--timeout-ms', 50]
                    reset_history()
                    if case == 'write-failure':
                        with closing(sqlite3.connect(database)) as connection:
                            connection.execute("CREATE TRIGGER reject_history BEFORE INSERT ON git_commits BEGIN SELECT RAISE(ABORT,'fixture write error'); END")
                            connection.commit()
                    if case == 'no-repository': (runner.root / '.git').rename(runner.directory / 'hidden-git')
                    before_db = state()
                    try:
                        code, output, error = invoke(args, fmt, suffix, env)
                    finally:
                        if case == 'no-repository': (runner.directory / 'hidden-git').rename(runner.root / '.git')
                    if case == 'positive':
                        if fmt == 'json':
                            doc = json.loads(output)
                            if profile == 'changed':
                                valid = doc == {'schema_version': 1, 'vcs': 'git', 'base': 'main',
                                                'head': 'HEAD', 'scope': None, 'changes': []}
                            else:
                                source_row = next((r for r in doc['items'] if r['path'] == 'Probe.java'), {})
                                valid = (doc['head'] == authored_head and doc['commits_analyzed'] == 4
                                         and [source_row.get(k) for k in ('commits', 'lines_added', 'lines_deleted', 'fix_commits')]
                                         == [4, 4, 3, 1] and doc['collection']['mode'] ==
                                         ('full' if profile == 'full' else 'incremental'))
                        else:
                            valid = (output == 'Changed files against main (0):\n' if profile == 'changed' else
                                     '4 commit(s) analyzed' in output and 'Probe.java' in output and
                                     ('[Full]' if profile == 'full' else '[Incremental]') in output)
                        valid = code == 0 and valid
                        record(VCS, f'{profile}:{case}:{fmt}:{suffix}', True, valid)
                    else:
                        record(VCS, f'{profile}:{case}:{fmt}:{suffix}',
                               {'exit': 1, 'empty': True, 'diagnostic': True, 'preserved': True},
                               {'exit': code, 'empty': output == '', 'diagnostic': bool(error.strip()),
                                'preserved': before_db == state()})
                    # Reset only disposable history, preserving failure logs.
                    reset_history()

    # Real Git controls protect binary numstat, subject separators and literal
    # newline/tab rename paths from over-strict fault handling.
    old, new = '\nOld\t.java', '\rNew\t.java'
    history.commit({old: 'class Renamed {}\n', 'binary.bin': '\0binary\0'})
    history.commit({}, rename=(old, new))
    original_probe = history.contents['Probe.java']
    history.commit({'Probe.java': 'class Probe { int value = 5; }\n'})
    history.git('commit', '--amend', '-qm', 'subject\x1ffix Java value')
    control = runner.json('hotspots', '--full', '--limit', 100)
    renamed = next((row for row in control['items'] if row['path'] == new), {})
    record(VCS, 'literal-rename-paths', 2, renamed.get('commits'))
    record(VCS, 'binary-stat', 0, next(row['churn'] for row in control['items'] if row['path'] == 'binary.bin'))
    record(VCS, 'separator-subject', 2,
           next(row['fix_commits'] for row in control['items'] if row['path'] == 'Probe.java'))
    (runner.root / 'Probe.java').write_text(original_probe)
    record(VCS, 'source-preservation', original_hashes,
           {name: file_sha256(runner.root / name) for name in SOURCES})

    for profile in (('watch', 'watch-status') if WATCH in features else ()):
        for case in WATCH_START:
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    args = [profile] + (['--quiet'] if case == 'idle-quiet' and profile == 'watch-status' else [])
                    lock = database.with_suffix('.watch.lock')
                    if case == 'lock-io': lock.unlink(missing_ok=True); lock.mkdir()
                    saved = runner.directory / 'saved-index.sqlite'
                    if case in ('damaged-db', 'missing-index'):
                        # Close and consolidate WAL before exchanging disposable data.
                        with closing(sqlite3.connect(database)) as connection:
                            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        database.rename(saved)
                        if case == 'damaged-db': database.write_bytes(b'fixture corrupt database')
                    # Idle watch is long-lived; readiness/runtime controls below
                    # execute that branch. Here exercise singleton and status.
                    try:
                        if case in ('positive', 'idle', 'idle-quiet') and profile == 'watch':
                            with held(lock): code, output, error = invoke(args, fmt, suffix)
                            valid = code == 0 and (json.loads(output) == {'command': 'watch', 'status': 'already-running'}
                                                  if fmt == 'json' else output == '')
                        elif case == 'positive':
                            with held(lock): code, output, error = invoke(args, fmt, suffix)
                            valid = code == 0 and (json.loads(output) == {'watching': True} if fmt == 'json' else output == 'watching\n')
                        else:
                            code, output, error = invoke(args, fmt, suffix)
                            if case == 'lock-io' or case == 'damaged-db' and profile == 'watch':
                                valid = code == 1 and output == '' and bool(error.strip())
                            elif profile == 'watch-status':
                                valid = code == 1 and not error and (output == '' if case == 'idle-quiet' else
                                        json.loads(output) == {'watching': False} if fmt == 'json' else output == 'not-watching\n')
                            else:
                                valid = ((code == 1 and output == '' and bool(error.strip())) if fmt == 'json' else
                                         (code == 0 and 'Index not found' in output))
                        record(WATCH, f'{profile}:{case}:{fmt}:{suffix}', True, valid)
                    finally:
                        if case == 'lock-io': lock.rmdir()
                        if case in ('damaged-db', 'missing-index'):
                            database.unlink(missing_ok=True); saved.rename(database)

    def declarations(name):
        return declarations_ready(runner, name)

    for fmt in (FORMATS if WATCH in features else ()):
        for suffix in POSITIONS:
            stdout_path, stderr_path = runner.directory / 'watch.stdout.log', runner.directory / 'watch.stderr.log'
            flags = ['--format', fmt]
            with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
                child = subprocess.Popen([str(runner.binary), *( ['watch'] + flags if suffix else flags + ['watch'])],
                                         cwd=runner.root, env=runner.environment, stdout=stdout, stderr=stderr)
                try:
                    def until(predicate):
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline and child.poll() is None:
                            if max(stdout_path.stat().st_size, stderr_path.stat().st_size) > runner.output_budget:
                                raise ToolError('watch fixture stream exceeded its budget')
                            if predicate(): return True
                            time.sleep(.05)
                        return False
                    if not until(lambda: b'watching' in stdout_path.read_bytes().lower()):
                        raise ToolError('watch failure fixture did not become ready')
                    def touch_until(source, predicate):
                        last_touch = 0
                        def observed():
                            nonlocal last_touch
                            if time.monotonic() - last_touch >= .8:
                                (runner.root / 'Probe.java').write_text(source)
                                last_touch = time.monotonic()
                            return predicate()
                        return until(observed)
                    for case in WATCH_RUNTIME:
                        baseline_error = stderr_path.stat().st_size
                        if case == 'sql-failure':
                            with closing(sqlite3.connect(database)) as connection:
                                connection.execute("CREATE TRIGGER reject_watch BEFORE INSERT ON symbols BEGIN SELECT RAISE(ABORT,'fixture watch error'); END"); connection.commit()
                        if case == 'cleared-index':
                            runner.command('clear')
                        context = held(database.with_suffix('.lock')) if case == 'mutation-busy' else closing(sqlite3.connect(':memory:'))
                        with context:
                            failed = touch_until('class Recovered {}\n',
                                                 lambda: b'Update error:' in stderr_path.read_bytes()[baseline_error:])
                        if case == 'sql-failure':
                            with closing(sqlite3.connect(database)) as connection:
                                connection.execute('DROP TRIGGER reject_watch'); connection.commit()
                        if case == 'cleared-index': runner.command('rebuild', '--force')
                        recovered = touch_until('class Recovered { int retried = 1; }\n',
                                                lambda: declarations('Recovered'))
                        # Successful fresh events remain parseable after failed batches.
                        events = stdout_path.read_text().splitlines()
                        valid = all(json.loads(line).get('command') == 'watch' for line in events) if fmt == 'json' else '\x1b' not in '\n'.join(events)
                        record(WATCH, f'runtime:{case}:{fmt}:{suffix}',
                               {'failed': True, 'recovered': True, 'alive': True, 'valid': True},
                               {'failed': failed, 'recovered': recovered, 'alive': child.poll() is None, 'valid': valid})
                        if not touch_until(SOURCES['Probe.java'], lambda: declarations('Probe')):
                            raise ToolError('watch fixture recovery did not restore declarations')
                finally:
                    child.terminate()
                    try: child.wait(timeout=3)
                    except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
    # Only fixture-authored mutations are restored; target/evidence is untouched.
    (runner.root / 'Probe.java').write_text(original_probe)
    for feature in FEATURES:
        record(feature, 'source-preservation', original_hashes,
               {name: file_sha256(runner.root / name) for name in SOURCES})
    return expected, actual
