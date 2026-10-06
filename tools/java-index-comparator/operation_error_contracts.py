"""Programmatic failure composition on disposable Java fixtures, not MCP truth."""
from pathlib import Path
from contextlib import closing, contextmanager
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

QUERY = 'global:format:java-query-errors'
SOURCE = 'global:format:java-source-errors'
RESTORE = 'global:format:java-restore-errors'
DELEGATE = 'global:format:java-delegate-errors'
FEATURES = {QUERY, SOURCE, RESTORE, DELEGATE}
REASON = ('internal CLI: disposable Java source read errors, SQLite query/schema '
          'failures and read-only enforcement, restore validation/destination failures '
          'and publication contention, '
          'delegate availability/exit composition; full file-type inventory; not MCP equivalence')
FORMATS = ('text', 'json')
POSITIONS = (False, True)
QUERY_CASES = {
    'syntax': ('SELECT FROM', 'syntax'),
    'unknown-table': ('SELECT * FROM absent_contract_table', 'no such table'),
    'row-error': ('SELECT abs(-9223372036854775808)', 'overflow'),
    'mutation': ('DELETE FROM symbols', 'allowed'),
    # Whitespace/comments defeat substring guards. LIMIT deliberately exists:
    # safety must not depend on appending a syntactically invalid LIMIT.
    'cte-delete': ('WITH x AS (SELECT 1) DELETE\nFROM symbols RETURNING name /* LIMIT */', 'allowed'),
    'cte-update': ('WITH x AS (SELECT 1) UPDATE/*gap*/symbols SET name=\'Lost\' RETURNING name /* LIMIT */', 'allowed'),
    'cte-insert': ('WITH x AS (SELECT 1) INSERT\nINTO modules(name,path) VALUES(\'Lost\',\'lost\') RETURNING name /* LIMIT */', 'allowed'),
}
RESTORE_CASES = ('missing', 'directory', 'symlink', 'same-file', 'hard-link',
                 'foreign', 'corrupt', 'publication', 'publication-lock')
DELEGATE_CASES = ('missing', 'unusable', 'no-match', 'failure', 'success')


@contextmanager
def sqlite_connection(path):
    # sqlite3's transaction context does not close its file descriptor.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


def acceptance_keys(feature):
    """Enumerate obligations before execution, including positive controls."""
    keys = {'inventory', 'source-preservation'}
    cases = {QUERY: [*QUERY_CASES, 'damaged-query', 'damaged-schema', 'quoted-schema',
                     'select', 'cte-select', 'explain', 'literal-keywords'],
             SOURCE: ['outline', 'outline-full', 'imports', 'api', 'api-limit-one',
                      'api-zero-limit', 'positive-outline', 'positive-imports', 'positive-api'],
             RESTORE: [*RESTORE_CASES, 'positive'],
             DELEGATE: list(DELEGATE_CASES)}[feature]
    for case in cases:
        for fmt in FORMATS:
            for suffix in POSITIONS:
                keys.add(f'{case}:{fmt}:{suffix}')
    if feature == QUERY:
        keys.add('java-declarations-preserved')
        keys.add('query-state-preserved')
        keys.add('damaged-state-preserved')
    if feature == RESTORE:
        keys.update({'restore-live-preserved', 'restore-source-preserved', 'staging-cleanup'})
    return keys


def plan_errors(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-operation-errors'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        # This child cannot close all operation-specific branches of the parent.
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java operation failure fixture')=0",
                      ('; Java operation failure fixture executes query/schema, source views, '
                       'restore and delegate composition; lexical scan I/O, root-registration I/O, '
                       'watch/VCS failures, restore commit/recovery I/O and '
                       'refresh/update publication failures remain unresolved',))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('operation error fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='operation-errors-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    sources = {'Probe.java': b'public class Probe { public void ping() {} }\n',
               'ignored/Inventory.kt': b'// inventory sentinel only\n',
               'inventory.xml': b'<inventory/>\n'}
    for name, content in sources.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        want = {name: Path(name).suffix for name in sources}
        if inventory != want:
            raise ToolError('operation fixture full inventory incomplete')
        for feature in FEATURES:
            record(feature, 'inventory', want, inventory)
    finally:
        state.close()
    runner.command('rebuild', '--force')
    # Backup uses SQLite's snapshot operation; no WAL generation is omitted.
    pristine = directory / 'pristine.sqlite'
    with sqlite_connection(database) as src, sqlite_connection(pristine) as dest:
        src.backup(dest)

    def invoke(feature, case, args, want, summarize=None, environment=None):
        for fmt in FORMATS:
            for suffix in POSITIONS:
                flags = ['--format', fmt]
                code, output = runner.command(*(args + flags if suffix else flags + args),
                                               environment=environment, acceptable=(0, 1, 2))
                with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
                    error = stream.read(runner.output_budget + 1)
                if len(error) > runner.output_budget:
                    raise ToolError('operation error diagnostic exceeded its budget')
                diagnostic = error.decode()
                got = summarize(code, output, diagnostic, fmt) if summarize else {
                    'exit': code, 'stdout': output, 'diagnostic': bool(diagnostic.strip())}
                record(feature, f'{case}:{fmt}:{suffix}', want, got)

    failure = {'exit': 1, 'stdout': '', 'diagnostic': True}
    with sqlite_connection(database) as connection:
        before = tuple(connection.execute('SELECT name,path FROM modules ORDER BY name,path'))
    for case, (sql, marker) in QUERY_CASES.items():
        invoke(QUERY, case, ['query', sql], failure,
               lambda code, output, error, fmt, marker=marker:
               {'exit': code, 'stdout': output, 'diagnostic': marker in error.lower()})
    record(QUERY, 'java-declarations-preserved', [('Probe', 'class'), ('ping', 'function')],
           [(r['name'], r['kind']) for r in runner.json('outline', 'Probe.java')['symbols']])
    # Check indexed lookup as well; an outline-only parser would miss mutation.
    with sqlite_connection(database) as connection:
        modules = tuple(connection.execute('SELECT name,path FROM modules ORDER BY name,path'))
    record(QUERY, 'query-state-preserved', {'symbols': ['Probe', 'ping'], 'modules': before},
           {'symbols': sorted(r['name'] for r in runner.json('symbol', '--pattern', '*', '--limit', '100')['items']),
            'modules': modules})
    positives = {'select': ('SELECT 7 AS n', [{'n': 7}]),
                 'cte-select': ('WITH x AS (SELECT 7 AS n) SELECT n FROM x', [{'n': 7}]),
                 'literal-keywords': ("SELECT ' DROP table ' AS n", [{'n': ' DROP table '}])}
    for case, (sql, rows) in positives.items():
        def read(code, output, error, fmt):
            try:
                doc = json.loads(output)
            except ValueError:
                doc = {}
            return {'exit': code, 'rows': doc.get('rows'), 'count': doc.get('count'), 'stderr': error}
        invoke(QUERY, case, ['query', sql], {'exit': 0, 'rows': rows, 'count': 1, 'stderr': ''}, read)
    invoke(QUERY, 'explain', ['query', 'EXPLAIN SELECT 7'], True,
           lambda code, output, error, fmt: code == 0 and not error and bool(json.loads(output)['rows']))
    # The same source-view paths must also return real authored identities.
    source = runner.root / 'Probe.java'
    invoke(SOURCE, 'positive-outline', ['outline', 'Probe.java'], True,
           lambda code, output, error, fmt: code == 0 and not error and
           ([(r['name'], r['line']) for r in json.loads(output)['symbols']] == [('Probe', 1), ('ping', 1)]
            if fmt == 'json' else 'Probe [class]' in output and 'ping [function]' in output))
    invoke(SOURCE, 'positive-api', ['api', ''], True,
           lambda code, output, error, fmt: code == 0 and not error and
           ([(r['path'], r['line'], r['content']) for r in json.loads(output)['items']] ==
            [('Probe.java', 1, sources['Probe.java'].decode().strip())]
            if fmt == 'json' else 'Probe.java:1' in output and sources['Probe.java'].decode().strip() in output))
    source.write_text('import java.util.List;\npublic class Probe {}\n')
    invoke(SOURCE, 'positive-imports', ['imports', 'Probe.java'], True,
           lambda code, output, error, fmt: code == 0 and not error and
           (json.loads(output).get('imports') == ['java.util.List'] if fmt == 'json' else 'java.util.List;' in output))
    # Every failure must be composed before writing any result document/header.
    source.write_bytes(b'\xffinvalid UTF-8 Java source')
    for case, args in [('outline', ['outline', 'Probe.java']),
                       ('outline-full', ['outline', 'Probe.java', '--full']),
                       ('imports', ['imports', 'Probe.java']), ('api', ['api', '']),
                       ('api-limit-one', ['api', '', '--limit', '1'])]:
        invoke(SOURCE, case, args, failure)
    # A zero page explicitly avoids reading Java sources; it is a format/limit
    # contract, never evidence that unreadable sources are language-inapplicable.
    invoke(SOURCE, 'api-zero-limit', ['api', '', '--limit', '0'], True,
           lambda code, output, error, fmt: code == 0 and not error and
           (json.loads(output).get('items') == [] if fmt == 'json' else 'No public API found.' in output))
    source.write_bytes(sources['Probe.java'])

    # A recognizable index with an independently authored corrupt data page.
    with sqlite_connection(database) as connection:
        connection.execute('CREATE TABLE "quote""-probe"(n INTEGER)')
        connection.execute('INSERT INTO "quote""-probe" VALUES (7)')
    def quoted(code, output, error, fmt):
        try:
            table = json.loads(output).get('quote"-probe', {})
        except ValueError:
            table = {}
        return (code, error, table.get('row_count'), [row['name'] for row in table.get('columns', [])])
    invoke(QUERY, 'quoted-schema', ['schema'], (0, '', 1, ['n']), quoted)
    with sqlite_connection(database) as connection:
        connection.execute('DROP TABLE "quote""-probe"')
        connection.execute('CREATE TABLE ProbeErrors(n INTEGER)')
        connection.execute('INSERT INTO ProbeErrors VALUES (7)')
        page = connection.execute("SELECT rootpage FROM sqlite_master WHERE name='ProbeErrors'").fetchone()[0]
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with database.open('r+b') as stream:
        stream.seek((page - 1) * page_size)
        stream.write(b'\xff')
    damaged = file_sha256(database)
    for case, args in [('damaged-query', ['query', 'SELECT * FROM ProbeErrors']), ('damaged-schema', ['schema'])]:
        invoke(QUERY, case, args, failure)
    record(QUERY, 'damaged-state-preserved', damaged, file_sha256(database))
    # Replace only the disposable DB, retaining the read-only target untouched.
    for suffix in ('', '-wal', '-shm'):
        Path(str(database) + suffix).unlink(missing_ok=True)
    shutil.copyfile(pristine, database)
    foreign = directory / 'foreign.sqlite'
    with sqlite_connection(foreign) as connection:
        connection.execute('CREATE TABLE foreign_data(n INTEGER)')
    corrupt = directory / 'corrupt.sqlite'
    corrupt.write_bytes(b'authored invalid sqlite')
    link = directory / 'link.sqlite'
    link.symlink_to(pristine)
    hard = directory / 'hard.sqlite'
    os.link(database, hard)
    trap = directory / 'publication-trap'
    trap.write_text('not a directory\n')
    source_before = file_sha256(pristine)
    live_before = file_sha256(database)
    restore_inputs = {'missing': directory / 'missing.sqlite', 'directory': runner.root,
                      'symlink': link, 'same-file': database, 'hard-link': hard,
                      'foreign': foreign, 'corrupt': corrupt, 'publication': pristine}
    for case, path in restore_inputs.items():
        invoke(RESTORE, case, ['restore', str(path)], failure,
               environment={'AST_INDEX_DB_PATH': str(trap / 'index.sqlite')} if case == 'publication' else None)
    # Hold a real shared reader lock while restore attempts exclusive
    # publication after staging. This is distinct from destination discovery.
    try:
        import fcntl
    except ImportError as error:
        raise ToolError('publication lock contract requires a supported OS lock fixture') from error
    with database.with_suffix('.publish.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            invoke(RESTORE, 'publication-lock', ['restore', str(pristine)], failure,
                   lambda code, output, error, fmt:
                   {'exit': code, 'stdout': output, 'diagnostic': 'retry' in error.lower()})
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    record(RESTORE, 'restore-live-preserved', live_before, file_sha256(database))
    record(RESTORE, 'restore-source-preserved', source_before, file_sha256(pristine))
    record(RESTORE, 'staging-cleanup', [], sorted(p.name for p in directory.iterdir() if p.name.startswith('.restore-')))
    invoke(RESTORE, 'positive', ['restore', str(pristine)], True,
           lambda code, output, error, fmt: code == 0 and not error and
           (json.loads(output).get('status') == 'complete' if fmt == 'json' else 'Restored index from:' in output))
    scripts = directory / 'providers'
    scripts.mkdir()
    provider = scripts / 'sg'
    environment = {'PATH': str(scripts)}
    for case in DELEGATE_CASES:
        if case != 'missing':
            provider.write_text('#!' + sys.executable + '\nimport sys\n' +
                ("sys.exit(3)\n" if case == 'unusable' else
                 "if '--version' in sys.argv: sys.exit(0)\n" +
                 {'no-match': 'sys.exit(1)\n', 'failure': 'sys.exit(3)\n',
                  'success': 'print(\'[]\'); sys.exit(0)\n'}[case]))
            provider.chmod(0o755)
        want = failure if case in ('missing', 'unusable', 'failure') else {
            'exit': 0, 'stdout': '[]\n' if case == 'success' else '', 'diagnostic': False}
        invoke(DELEGATE, case, ['agrep', 'class Probe {}', '--lang', 'java', '--json'], want,
               environment=environment)
    for feature in FEATURES:
        record(feature, 'source-preservation', sources,
               {name: (runner.root / name).read_bytes() for name in sources})
    # JSON evidence stores strings rather than binary fixture payloads.
    for feature in FEATURES:
        for section in (expected, actual):
            section[feature]['source-preservation'] = {
                name: content.decode() for name, content in section[feature]['source-preservation'].items()}
    return expected, actual
