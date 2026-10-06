"""Staged Java publication failures: internal CLI/state, never MCP truth."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-publication-errors'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: disposable Java staged rebuild '
          'variants, restore and clear; shared-reader contention, lock I/O, invalid '
          'recovery markers, untracked swaps, preservation/cleanup and successful '
          'publication controls in JSON/text; complete inventory; not MCP equivalence')
PROFILES = {
    'all': ['rebuild', '--force'],
    'files': ['rebuild', '--force', '--type', 'files'],
    'symbols': ['rebuild', '--force', '--type', 'symbols'],
    'modules': ['rebuild', '--force', '--type', 'modules'],
    'modules-no-deps': ['rebuild', '--force', '--type', 'modules', '--no-deps'],
    'deps': ['rebuild', '--force', '--type', 'deps'],
    'fast': ['rebuild', '--force', '--experimental-fast-rebuild'],
    'sub-projects': ['rebuild', '--force', '--sub-projects'],
    'sub-projects-fast': ['rebuild', '--force', '--sub-projects', '--experimental-fast-rebuild'],
    'remember': ['rebuild', '--force', '--remember'],
    'restore': ['restore'],
    'clear': ['clear'],
}
PHASES = ('reader-busy', 'writer-busy', 'publication-lock-io', 'state-json',
          'commit-json', 'marker-directory', 'untracked-swap', 'positive')
FORMATS = ('text', 'json')
POSITIONS = (False, True)
SOURCES = {
    'Probe.java': 'class ProbeOld { void ping() {} }\n',
    'part/build.gradle': '// synthetic Java module marker\n',
    'part/Peer.java': 'class Peer {}\n',
    'build/Inventory.kt': '// inventory only\n',
    'descriptor.xml': '<fixture/>\n',
}
INVENTORY = {name: Path(name).suffix for name in SOURCES}


def acceptance_keys():
    return {'inventory', 'applicable-java', 'source-preservation', 'backup-preservation'} | {
        f'{profile}:{phase}:{fmt}:{suffix}' for profile in PROFILES
        for phase in PHASES for fmt in FORMATS for suffix in POSITIONS}


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= section.keys()
               and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def plan_errors(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-publication-errors'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java publication error fixture')=0",
                      ('; Java publication error fixture executes staged rebuild/restore/clear '
                       'shared-reader and writer contention, lock I/O, marker validation, '
                       'untracked swap preservation and success-after-publication controls; '
                       'late commit-marker write/fsync and rollback/recovery I/O, '
                       'foreground/background update completion and graph refresh failures, '
                       'watch/VCS failures remain pending',))


@contextmanager
def held(path, shared=False):
    import fcntl
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('publication error fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='publication-errors-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    for name, source in SOURCES.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    inventory = connect(directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        files = dict(inventory.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if files != INVENTORY:
            raise ToolError('publication fixture full inventory incomplete')
        record('inventory', INVENTORY, files)
        record('applicable-java', True, any(ext == '.java' for ext in files.values()))
    finally:
        inventory.close()
    runner.command('rebuild', '--force')
    backup = directory / 'backup.sqlite'
    with held(database.with_suffix('.publish.lock'), shared=True):
        source = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()
    backup_hash = file_sha256(backup)
    # Changed authored source makes preservation of the OLD published index
    # observable. Native DB state comparison is only an internal atomicity check.
    replacement = 'class ProbeNew { void pong() {} }\n'
    (runner.root / 'Probe.java').write_text(replacement)
    publication_lock = database.with_suffix('.publish.lock')
    mutation_lock = database.with_suffix('.lock')

    def reset():
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        shutil.copyfile(backup, database)

    def state():
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    def invoke(profile, phase, fmt, suffix):
        args = [*PROFILES[profile], *([str(backup)] if profile == 'restore' else [])]
        flags = ['--format', fmt]
        before = state()
        code, output = runner.command(*(args + flags if suffix else flags + args), acceptable=(0, 1, 2))
        with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
            error = stream.read(runner.output_budget + 1)
        if len(error) > runner.output_budget:
            raise ToolError('publication error diagnostic exceeded its budget')
        # Progress is allowed in text. A terminal success claim is only valid
        # after the new generation is published. JSON errors emit no document.
        success = any(line.startswith(('Indexed ', 'Done:', 'Restored index from:',
                                       'Index cleared for ', 'Persisted --force'))
                      for line in output.splitlines())
        cleaned = not any(path.is_dir() and path.name.startswith(('.rebuild-', '.restore-'))
                          for path in directory.iterdir())
        if phase != 'positive':
            want = {'exit': 1, 'diagnostic': True, 'success': False, 'json-empty': True,
                    'published-state-preserved': True, 'staging-cleaned': True}
            got = {'exit': code, 'diagnostic': bool(error.strip()), 'success': success,
                   'json-empty': fmt != 'json' or output == '',
                   'published-state-preserved': state() == before, 'staging-cleaned': cleaned}
        else:
            want = True
            try:
                doc = json.loads(output) if fmt == 'json' else None
                if profile == 'clear':
                    published = not database.exists()
                else:
                    rows = runner.json('class', '--pattern', '*', '--limit', 100)['items']
                    names = sorted(row['name'] for row in rows)
                    published = names == (['Peer', 'ProbeNew'] if profile in {
                        'all', 'files', 'symbols', 'fast', 'sub-projects', 'sub-projects-fast', 'remember'
                    } else ['Peer', 'ProbeOld'])
                got = (code == 0 and published and cleaned and
                       (doc['command'] == args[0] and doc['status'] == 'complete'
                        if doc is not None else success))
            except (ValueError, KeyError, TypeError):
                got = False
        record(f'{profile}:{phase}:{fmt}:{suffix}', want, got)

    @contextmanager
    def fault(phase):
        if phase in ('reader-busy', 'writer-busy'):
            with held(publication_lock if phase == 'reader-busy' else mutation_lock,
                      shared=phase == 'reader-busy'):
                yield
            return
        if phase == 'publication-lock-io':
            publication_lock.unlink(missing_ok=True)
            publication_lock.mkdir()
            try:
                yield
            finally:
                publication_lock.rmdir()
            return
        suffix = {'state-json': '.publish-state-v1', 'commit-json': '.publish-commit-v1',
                  'marker-directory': '.publish-state-v1', 'untracked-swap': '.swap'}.get(phase)
        if suffix:
            path = Path(str(database) + suffix)
            if phase == 'marker-directory':
                path.mkdir()
            else:
                path.write_bytes(b'authored invalid publication artifact')
            before = None if path.is_dir() else file_sha256(path)
            try:
                yield
                if not path.exists() or (before is not None and file_sha256(path) != before):
                    raise ToolError('publication fixture deleted or changed unowned recovery evidence')
            finally:
                path.rmdir() if path.is_dir() else path.unlink(missing_ok=True)
            return
        yield

    for phase in PHASES:
        for profile in PROFILES:
            # Clear takes the publication lock, but has no rebuild mutation
            # phase. A held writer lock still serializes clear at CLI dispatch.
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    reset()
                    with fault(phase):
                        invoke(profile, phase, fmt, suffix)
    record('backup-preservation', backup_hash, file_sha256(backup))
    record('source-preservation', {**SOURCES, 'Probe.java': replacement},
           {name: (runner.root / name).read_text() for name in SOURCES})
    return {FEATURE: expected}, {FEATURE: actual}
