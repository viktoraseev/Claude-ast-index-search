"""Java update/graph freshness failures: internal CLI/state, not MCP truth."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from publication_error_contracts import held
from root_contracts import Runner

FEATURE = 'global:format:java-freshness-errors'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: disposable Java foreground/background '
          'update read/write/completion/module failures, pending-generation retries and '
          'graph build/refresh read/write rollback across every refresh consumer; '
          'complete file-type inventory; not MCP equivalence')
SOURCES = {'Probe.java': 'class Probe { void ping() {} void use() { ping(); } }\n',
           'part/build.gradle': '// synthetic Java module\n',
           'part/Peer.java': 'class Peer {}\n',
           'build/Inventory.kt': '// inventory only\n', 'inventory.xml': '<fixture/>\n'}
INVENTORY = {name: Path(name).suffix for name in SOURCES}
UPDATE = {'foreground': ['update'], 'verbose': ['update', '--verbose'],
          'background': ['update', '--background', '--debounce-ms', '0'],
          'debounced': ['update', '--background', '--verbose', '--debounce-ms', '30']}
UPDATE_CASES = ('utf8', 'walk', 'insert', 'delete', 'completion', 'module', 'module-only',
                'fingerprint', 'mutation-busy', 'worker-failure', 'positive', 'noop', 'excluded')
GRAPH = {'build': ['graph', 'build', '--verbose'],
         'dependencies': ['graph', 'dependencies', 'Probe#use'],
         'dependents': ['graph', 'dependents', 'Probe#ping'],
         'impact': ['graph', 'impact', 'Probe#ping'],
         'path': ['graph', 'path', 'Probe#use', 'Probe#ping'],
         'cycles': ['graph', 'cycles'], 'top': ['graph', 'top'],
         'metrics': ['graph', 'metrics', 'Probe#ping']}
GRAPH_CASES = ('utf8', 'missing-source', 'source-directory', 'edge-write', 'metric-write',
               'completion', 'positive', 'unbuilt', 'fresh-no-read')
FORMATS = ('json', 'text')
POSITIONS = (False, True)


def acceptance_keys():
    return {'inventory', 'applicable-java', 'source-preservation', 'backup-preservation'} | {
        f'update:{profile}:{case}:{fmt}:{suffix}' for profile in UPDATE for case in UPDATE_CASES
        for fmt in FORMATS for suffix in POSITIONS} | {
        f'graph:{profile}:{case}:{fmt}:{suffix}' for profile in GRAPH for case in GRAPH_CASES
        for fmt in FORMATS for suffix in POSITIONS}


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= section.keys() and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def plan_errors(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-freshness-errors-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java freshness error fixture')=0",
                      ('; Java freshness error fixture executes foreground/background Java source '
                       'read, SQL write, completion and module refresh failures, pending generation '
                       'retry and every graph refresh consumer read/write rollback; restore '
                       'late-marker write/fsync, rollback/recovery I/O, notification backend/channel '
                       'and incremental moved-path protocol failures remain pending',))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('freshness fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='freshness-errors-', dir=base)).resolve())
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    database = runner.directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database),
                              AST_INDEX_UPDATE_WAIT_TIMEOUT_MS='3000')
    for name, content in SOURCES.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        files = dict(inventory.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if files != INVENTORY:
            raise ToolError('freshness fixture full inventory incomplete')
    finally:
        inventory.close()
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got
        checkpoint = runner.directory / 'results.next.json'
        checkpoint.write_text(json.dumps({'expected': expected, 'actual': actual}))
        checkpoint.replace(runner.directory / 'results.json')

    record('inventory', INVENTORY, files)
    record('applicable-java', True, any(ext == '.java' for ext in files.values()))
    runner.command('rebuild', '--force')
    runner.command('graph', 'build')
    backup = runner.directory / 'baseline.sqlite'
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    backup_hash = file_sha256(backup)
    state_path = database.with_suffix('.db.update-state-v1.json')

    def sql(statement):
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(statement)

    def snapshot():
        with closing(sqlite3.connect(database)) as connection:
            return tuple(connection.iterdump())

    def dirty():
        with closing(sqlite3.connect(database)) as connection:
            return bool(connection.execute("SELECT 1 FROM metadata WHERE key='index_update_dirty_at'").fetchone())

    def state():
        return json.loads(state_path.read_text()) if state_path.exists() else {}

    def reset():
        deadline = time.monotonic() + 5
        while state().get('worker_scheduled'):
            if time.monotonic() > deadline:
                raise ToolError('freshness fixture worker did not finish')
            time.sleep(.02)
        state_path.unlink(missing_ok=True)
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
        for name, content in SOURCES.items():
            path = runner.root / name
            if path.is_dir(): path.rmdir()
            path.write_text(content)
        (runner.root / '.ast-index.yaml').unlink(missing_ok=True)
        (runner.root / 'Excluded.java').unlink(missing_ok=True)

    def invoke(args, fmt, suffix, env=None):
        flags = ['--format', fmt]
        code, output = runner.command(*(args + flags if suffix else flags + args),
                                      environment=env, acceptable=(0, 1, 2, 101))
        with (runner.directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
            error = stream.read(runner.output_budget + 1)
        if len(error) > runner.output_budget:
            raise ToolError('freshness diagnostic exceeded its budget')
        return code, output, error

    def trigger(table, operation, condition=''):
        sql(f"CREATE TRIGGER fixture_fault BEFORE {operation} ON {table} {condition} "
            "BEGIN SELECT RAISE(ABORT,'authored freshness failure'); END;")

    @contextmanager
    def mutation_fault(case):
        if case == 'mutation-busy':
            with held(database.with_suffix('.lock')): yield
        else:
            yield

    for profile, args in UPDATE.items():
        for case in UPDATE_CASES:
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    reset()
                    source = runner.root / 'Probe.java'
                    if case not in ('noop', 'module-only', 'excluded'):
                        source.write_text('class Probe { void ping() {} void use() { ping(); } void added() {} }\n')
                    if case == 'utf8': source.write_bytes(b'\xff invalid Java source')
                    if case == 'walk': (runner.root / 'part').chmod(0)
                    if case == 'delete': (runner.root / 'part/Peer.java').unlink()
                    if case in ('module', 'module-only', 'fingerprint'):
                        (runner.root / 'part/build.gradle').write_text('// changed Java module descriptor\n')
                    if case == 'excluded':
                        (runner.root / 'Excluded.java').write_bytes(b'\xff')
                        (runner.root / '.ast-index.yaml').write_text('exclude: [Excluded.java]\n')
                    if case in ('insert', 'delete'): trigger('files', 'INSERT' if case == 'insert' else 'DELETE')
                    if case == 'completion': trigger('metadata', 'INSERT', "WHEN NEW.key='last_update_at'")
                    if case in ('module', 'module-only'): trigger('modules', 'INSERT')
                    if case == 'fingerprint': trigger('metadata', 'INSERT', "WHEN NEW.key='build_files_fingerprint'")
                    env = {}
                    if case == 'worker-failure':
                        fail = runner.directory / 'fail-once'
                        fail.write_text('authored fault')
                        env['AST_INDEX_TEST_UPDATE_FAIL_ONCE_FILE'] = str(fail)
                    successful = case in ('positive', 'noop', 'excluded')
                    try:
                        with mutation_fault(case):
                            code, output, error = invoke(args, fmt, suffix, env)
                            if '--background' in args:
                                # Queue acknowledgement is not completion. The first reader
                                # must wait for the worker, or fail without a success document.
                                queued = code == 0 and ('"queued"' in output if fmt == 'json' else 'Queued background' in output)
                                code, output, error = invoke(['stats'], fmt, suffix)
                            else:
                                queued = True
                    finally:
                        if case == 'walk': (runner.root / 'part').chmod(0o755)
                    current = state()
                    want = {'exit': 0 if successful else 1, 'acknowledged': successful,
                            'success-cycles': 1 if successful else 0, 'queue': True,
                            'error-output': True, 'dirty': case in ('utf8', 'insert', 'delete', 'completion',
                                                                   'module', 'module-only', 'fingerprint'),
                            'retry': True}
                    got = {'exit': code, 'acknowledged': current.get('completed_generation', 0) == current.get('requested_generation', 1),
                           'success-cycles': current.get('successful_cycles', 0), 'queue': queued,
                           'error-output': successful or (bool(error.strip()) and (fmt != 'json' or output == '')
                                           and 'Index is up to date.' not in output and 'Updated:' not in output),
                           'dirty': dirty(), 'retry': False}
                    sql('DROP TRIGGER IF EXISTS fixture_fault;')
                    source.write_text(SOURCES['Probe.java'] + 'class AddedOnRetry {}\n')
                    (runner.root / 'part/Peer.java').write_text(SOURCES['part/Peer.java'])
                    runner.command('update')
                    rows = runner.json('class', '--pattern', '*', '--limit', 100)['items']
                    current = state()
                    got['retry'] = (sorted(row['name'] for row in rows) == ['AddedOnRetry', 'Peer', 'Probe']
                                    and not dirty() and current['requested_generation'] == current['completed_generation']
                                    and not current.get('last_error'))
                    record(f'update:{profile}:{case}:{fmt}:{suffix}', want, got)

    for profile, args in GRAPH.items():
        for case in GRAPH_CASES:
            for fmt in FORMATS:
                for suffix in POSITIONS:
                    reset()
                    if case != 'fresh-no-read':
                        sql("INSERT OR REPLACE INTO metadata VALUES ('index_generation', '999999')")
                    if case == 'unbuilt':
                        sql("DELETE FROM metadata WHERE key LIKE 'symbol_graph_%'")
                    source = runner.root / 'Probe.java'
                    if case in ('utf8', 'fresh-no-read'): source.write_bytes(b'\xff')
                    if case in ('missing-source', 'source-directory'): source.unlink()
                    if case == 'source-directory': source.mkdir()
                    if case == 'edge-write': trigger('symbol_edges', 'INSERT')
                    if case == 'metric-write': trigger('symbol_metrics', 'INSERT')
                    if case == 'completion': trigger('metadata', 'INSERT', "WHEN NEW.key='symbol_graph_fingerprint'")
                    # Build explicitly always reads; refresh consumers avoid a fresh rebuild.
                    fresh_skip = case == 'fresh-no-read' and profile != 'build'
                    successful = case in ('positive', 'unbuilt') or fresh_skip
                    command = args if profile == 'build' else [*args, '--refresh']
                    before = snapshot()
                    code, output, error = invoke(command, fmt, suffix)
                    after = snapshot()
                    want = {'exit': 0 if successful else 1, 'output': True,
                            'rollback': True, 'fresh': True, 'retry': True}
                    got = {'exit': code, 'output': (bool(output.strip()) and (fmt != 'json' or isinstance(json.loads(output), dict)))
                           if successful and code == 0 else bool(error.strip()) and output == '',
                           'rollback': successful or before == after, 'fresh': True, 'retry': False}
                    if successful:
                        published = runner.json('graph', 'status')['graph']
                        got['fresh'] = published['built'] and not published['stale']
                        if fresh_skip:
                            got['rollback'] = before == after
                    sql('DROP TRIGGER IF EXISTS fixture_fault;')
                    if source.is_dir(): source.rmdir()
                    source.write_text(SOURCES['Probe.java'])
                    runner.command('graph', 'build')
                    edges = runner.json('graph', 'dependencies', 'Probe#use')['items']
                    got['retry'] = any(edge['other']['name'] == 'ping' for edge in edges)
                    record(f'graph:{profile}:{case}:{fmt}:{suffix}', want, got)
    reset()
    record('source-preservation', SOURCES, {name: (runner.root / name).read_text() for name in SOURCES})
    record('backup-preservation', backup_hash, file_sha256(backup))
    return {FEATURE: expected}, {FEATURE: actual}
