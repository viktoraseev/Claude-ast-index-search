"""Root registration I/O and fallback identities on disposable Java sources.

These are internal CLI/source-state contracts, never MCP equivalence. Missing
attachments remain registrable for compatibility with unavailable mounts.
"""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-root-errors'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: Java root registration '
          'cache/lock, SQLite read/write/migration failures, unavailable-path '
          'fallback and canonicalization policy, JSON/text and state preservation; '
          'complete file-type inventory; not MCP equivalence')
INVENTORY = {'Probe.java': '.java', 'Inventory.kt': '.kt', 'inventory.xml': '.xml'}
COMMANDS = ('add-root', 'remove-root', 'subtree-add', 'subtree-remove',
            'list-roots', 'subtree-list')
MUTATIONS = COMMANDS[:4]
FORMATS = ('text', 'json')
POSITIONS = (False, True)


def phases(command):
    cases = ['missing-index', 'cache-parent', 'publication-busy',
             'damaged-subtrees', 'migration-error', 'migration-rollback', 'positive']
    if command != 'remove-root':
        cases += ['bad-row']
    if command in MUTATIONS:
        cases += ['rebuild-busy', 'rebuild-io', 'write-error']
    if command == 'add-root':
        cases += ['allocation-error', 'raw-identity', 'canonical-identity',
                  'fallback-identity', 'broken-link-identity', 'file-identity', 'raw-overlap', 'raw-force']
    if command == 'subtree-add':
        cases += ['raw-identity', 'canonical-identity', 'fallback-identity',
                  'broken-link-identity', 'file-identity', 'raw-overlap', 'raw-force']
    if command == 'remove-root':
        cases += ['raw-identity', 'canonical-identity', 'fallback-identity',
                  'broken-link-identity', 'file-identity']
    return cases


def acceptance_keys():
    return {'inventory', 'applicable-java', 'source-preservation', 'java-declarations'} | {
        f'{command}:{phase}:{fmt}:{suffix}' for command in COMMANDS
        for phase in phases(command) for fmt in FORMATS for suffix in POSITIONS}


def acceptance_complete(expected, actual):
    required = acceptance_keys()
    return (all(required <= section.keys() and section.get('inventory') == INVENTORY
                and section.get('applicable-java') is True for section in (expected, actual)))


def plan_errors(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-root-errors'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java root I/O fixture')=0",
                      ('; Java root I/O fixture executes add/remove/list aliases, rebuild/publication '
                       'locks, DB read/write/migration errors and unavailable mount identities; '
                       'watch/VCS failures, restore commit/recovery I/O and '
                       'refresh/update publication failures remain unresolved',))


@contextmanager
def sql(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


@contextmanager
def locked(path):
    import fcntl
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('root error fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='root-errors-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    sources = {'Probe.java': 'class Probe { void ping() {} }\n',
               'Inventory.kt': '// inventory only\n', 'inventory.xml': '<inventory/>\n'}
    for name, content in sources.items():
        (runner.root / name).write_text(content)
    peer = directory / 'peer'
    peer.mkdir()
    (peer / 'Peer.java').write_text('class Peer {}\n')
    alias = directory / 'alias'
    alias.symlink_to(peer, target_is_directory=True)
    missing = directory / 'unavailable'
    broken = directory / 'broken'
    broken.symlink_to(missing, target_is_directory=True)
    trap = directory / 'parent-trap'
    trap.write_text('not a directory\n')
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if inventory != INVENTORY:
            raise ToolError('root error fixture full inventory incomplete')
        record('inventory', INVENTORY, inventory)
        record('applicable-java', True, any(ext == '.java' for ext in inventory.values()))
    finally:
        state.close()

    def args(command, path=peer, name='peer'):
        return {'add-root': ['add-root', str(path)], 'remove-root': ['remove-root', str(path)],
                'subtree-add': ['subtree', 'add', name, str(path)],
                'subtree-remove': ['subtree', 'remove', name],
                'list-roots': ['list-roots'], 'subtree-list': ['subtree', 'list']}[command]

    def invoke(command, phase, fmt, suffix, arguments=None, environment=None, positive=None):
        flags = ['--format', fmt]
        arguments = arguments or args(command)
        before = file_sha256(database) if database.exists() else None
        code, output = runner.command(*(arguments + flags if suffix else flags + arguments),
                                      environment=environment, acceptable=(0, 1, 2))
        with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
            error = stream.read(runner.output_budget + 1)
        if len(error) > runner.output_budget:
            raise ToolError('root error diagnostic exceeded its budget')
        if positive is not None:
            want = True
            try:
                got = code == 0 and not error.strip() and positive(output, fmt)
            except (ValueError, KeyError, TypeError):
                got = False
        elif phase == 'missing-index':
            message = "Index not found. Run 'ast-index rebuild' first."
            want = True
            got = (code == (1 if fmt == 'json' else 0) and
                   output == ('' if fmt == 'json' else message + '\n') and
                   (message in error.decode() if fmt == 'json' else not error) and
                   before == (file_sha256(database) if database.exists() else None))
        else:
            want = {'exit': 1, 'stdout-empty': True, 'diagnostic': True, 'database-preserved': True}
            got = {'exit': code, 'stdout-empty': not output, 'diagnostic': bool(error.strip()),
                   'database-preserved': before == (file_sha256(database) if database.exists() else None)}
        record(f'{command}:{phase}:{fmt}:{suffix}', want, got)

    def matrix(phase, commands=COMMANDS, **kwargs):
        for command in commands:
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    invoke(command, phase, fmt, suffix, **kwargs)

    matrix('missing-index')
    runner.command('rebuild', '--force')
    pristine = directory / 'pristine.sqlite'
    with sql(database) as src, sql(pristine) as dest:
        src.backup(dest)

    def reset():
        # Only disposable index generations are replaced; never the target.
        for ending in ('-wal', '-shm'):
            Path(str(database) + ending).unlink(missing_ok=True)
        with sql(pristine) as src, sql(database) as dest:
            src.backup(dest)

    matrix('cache-parent', environment={'AST_INDEX_DB_PATH': str(trap / 'index.sqlite')})
    with locked(database.with_suffix('.publish.lock')):
        matrix('publication-busy')
    with locked(database.with_suffix('.lock')):
        matrix('rebuild-busy', MUTATIONS)
    database.with_suffix('.lock').unlink()
    database.with_suffix('.lock').mkdir()
    matrix('rebuild-io', MUTATIONS)
    database.with_suffix('.lock').rmdir()

    # Recognizable current index, but a subtree read cannot decode its row.
    # A known-name row reaches lookup and removal too, rather than a no-match.
    with sql(database) as connection:
        connection.execute('INSERT INTO subtrees(name,canonical_path,original_path) VALUES (?,?,?)', ('peer', str(peer), sqlite3.Binary(b'invalid-text')))
    for command in COMMANDS:
        if command != 'remove-root':
            matrix('bad-row', (command,), arguments=args(command, directory / 'fresh' / 'peer')
                   if command == 'add-root' else args(command))
    reset()
    # Damage only a subtree data page, leaving sqlite_master and the files
    # table recognizable. Every command must surface the read/write failure.
    with sql(database) as connection:
        connection.execute('INSERT INTO subtrees(name,canonical_path,original_path) VALUES (?,?,?)',
                           ('peer', str(peer), str(peer)))
        page = connection.execute("SELECT rootpage FROM sqlite_master WHERE name='subtrees'").fetchone()[0]
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with database.open('r+b') as stream:
        stream.seek((page - 1) * page_size)
        stream.write(b'\xff')
    matrix('damaged-subtrees')
    # A corrupt SQLite target cannot be opened for backup; replace only this
    # disposable file after all preservation assertions have executed.
    for ending in ('', '-wal', '-shm'):
        Path(str(database) + ending).unlink(missing_ok=True)
    reset()
    with sql(database) as connection:
        connection.execute("INSERT INTO metadata VALUES ('extra_roots','invalid-json')")
    matrix('migration-error')
    reset()
    with sql(database) as connection:
        connection.execute('INSERT INTO metadata VALUES (?,?)',
                           ('extra_roots', json.dumps([str(directory / 'first'), str(directory / 'second')])))
        connection.execute("CREATE TRIGGER reject_migration BEFORE INSERT ON subtrees "
                           "WHEN NEW.name='second' BEGIN SELECT RAISE(ABORT,'authored migration failure'); END")
    matrix('migration-rollback')
    reset()
    # SQLite statements fail after normal index recognition. Both add and
    # delete must preserve the registered owner and emit no success payload.
    with sql(database) as connection:
        connection.execute('INSERT INTO subtrees(name,canonical_path,original_path) VALUES (?,?,?)', ('peer', str(peer), str(peer)))
        connection.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON subtrees BEGIN SELECT RAISE(ABORT,'authored write failure'); END")
        connection.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON subtrees BEGIN SELECT RAISE(ABORT,'authored write failure'); END")
    for command in MUTATIONS:
        matrix('write-error', (command,), arguments=args(command, directory / 'fresh', 'fresh')
               if command in ('add-root', 'subtree-add') else args(command))
    reset()
    with sql(database) as connection:
        connection.executemany('INSERT INTO subtrees(name,canonical_path,original_path) VALUES (?,?,?)',
            [('peer' if n == 1 else f'peer-{n}', str(directory / f'allocated-{n}'), str(n)) for n in range(1, 1000)])
    matrix('allocation-error', ('add-root',))
    reset()

    def registered():
        return runner.json('subtree', 'list')

    # Every positive executes a real mutation/list with independently authored
    # state expectations. Reported root identities must match registered ones.
    for fmt in FORMATS:
        for suffix in POSITIONS:
            for command in COMMANDS:
                reset()
                if command in ('remove-root', 'subtree-remove', 'list-roots', 'subtree-list'):
                    runner.command('subtree', 'add', 'peer', str(peer))
                def check(output, format, command=command):
                    rows = registered()
                    absent = command in ('remove-root', 'subtree-remove')
                    if rows != ([] if absent else [{'name': 'peer', 'canonical_path': str(peer),
                                                    'original_path': str(peer)}]):
                        return False
                    if format == 'json':
                        doc = json.loads(output)
                        if command in ('list-roots', 'subtree-list'):
                            return doc == rows
                        if command == 'add-root':
                            return doc == {'command': command, 'path': str(peer), 'status': 'complete'}
                        if command == 'subtree-add':
                            return doc == rows[0]
                        return doc == ({'command': command, 'removed': True, 'path': str(peer)}
                                       if command == 'remove-root' else {'name': 'peer', 'removed': True})
                    return {'add-root': 'Added source root:', 'subtree-add': 'Attached subtree peer',
                            'remove-root': 'Removed source root:', 'subtree-remove': 'Detached subtree peer',
                            'list-roots': 'Subtrees attached to this project:',
                            'subtree-list': 'Subtrees attached to this project:'}[command] in output
                invoke(command, 'positive', fmt, suffix, positive=check)

            for phase, path, environment, identity in (
                    ('raw-identity', alias, {'AST_INDEX_NO_CANONICALIZE': '1'}, alias),
                    ('canonical-identity', alias, {}, peer),
                    ('fallback-identity', missing, {}, missing),
                    ('broken-link-identity', broken, {}, broken),
                    ('file-identity', peer / 'Peer.java', {}, peer / 'Peer.java')):
                for command in ('add-root', 'subtree-add', 'remove-root'):
                    reset()
                    # Legacy names come from the supplied basename; named
                    # attachments preserve the explicit CLI name.
                    name = path.name.replace('.', '-') if command == 'add-root' else identity.name
                    if command == 'remove-root':
                        runner.command('add-root', str(path), environment=environment)
                    def check(output, format, command=command, identity=identity, name=name, path=path):
                        rows = runner.json('subtree', 'list', environment=environment)
                        if command == 'remove-root':
                            return rows == [] and (json.loads(output) == {
                                'command': 'remove-root', 'removed': True, 'path': str(identity)}
                                if format == 'json' else f'Removed source root: {path}' in output)
                        if rows != [{'name': name, 'canonical_path': str(identity), 'original_path': str(path)}]:
                            return False
                        if format == 'json':
                            doc = json.loads(output)
                            return (doc == {'command': 'add-root', 'status': 'complete', 'path': str(identity)}
                                    if command == 'add-root' else doc == rows[0])
                        return (f'Added source root: {path}' if command == 'add-root'
                                else f'Attached subtree {name} → {identity}') in output
                    invoke(command, phase, fmt, suffix, args(command, path, name), environment, check)

            for command in ('add-root', 'subtree-add'):
                for phase, force in (('raw-overlap', False), ('raw-force', True)):
                    reset()
                    arguments = args(command, runner.root, 'project') + (['--force'] if force else [])
                    def check(output, format, command=command, force=force):
                        rows = registered()
                        if force:
                            return rows == [{'name': 'project', 'canonical_path': str(runner.root),
                                             'original_path': str(runner.root)}] and (
                                json.loads(output) == ({'command': 'add-root', 'status': 'complete',
                                                        'path': str(runner.root)}
                                                       if command == 'add-root' else rows[0])
                                if format == 'json' else str(runner.root) in output)
                        return rows == [] and (json.loads(output) == {
                            'command': 'add-root' if command == 'add-root' else 'subtree-add',
                            'status': 'overlap-refused', 'path': str(runner.root)}
                            if format == 'json' else 'Warning:' in output and str(runner.root) in output)
                    invoke(command, phase, fmt, suffix, arguments, {'AST_INDEX_NO_CANONICALIZE': '1'}, check)
    reset()
    record('java-declarations', [('Probe', 'class'), ('ping', 'function')],
           sorted((row['name'], row['kind']) for row in runner.json('symbol', '--pattern', '*')['items']))
    record('source-preservation', {**sources, 'peer/Peer.java': 'class Peer {}\n'},
           {**{name: (runner.root / name).read_text() for name in sources},
            'peer/Peer.java': (peer / 'Peer.java').read_text()})
    return {FEATURE: expected}, {FEATURE: actual}
