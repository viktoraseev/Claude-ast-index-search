"""Executed lifecycle rendering on disposable Java sources, never MCP evidence."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-lifecycle'
FEATURES = {FEATURE}
REASON = ('internal CLI/DB plus independent source/state: disposable Java lifecycle JSON/text '
          'responses, published counts, declaration changes, empty/missing states and watch events; '
          'not MCP equivalence or management installation/root mutation formats')

# Immutable assertion IDs from the original acceptance population, in authored
# command order. These are public regression identities, not private log
# ordinals: asynchronous watch probes may consume any number of log entries.
ANSI_ASSERTION_IDS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    22, 24, 26, 28, 29, 31, 33, 34, 35, 39, 40, 41, 42, 43,
    51, 52, 53, 55, 56, 58, 59, 61,
)


def plan_formats(state, root):
    if root is None:
        return
    subject = 'disposable-java-lifecycle-formats'
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java read-only and lifecycle formats have separate executed contracts; '
                       'management installation and root mutation formats remain unresolved',))


def watch_readiness(runner, child, log_path, deadline):
    """Observe readiness while the process is alive, before testing changes."""
    locked = False
    while True:
        if child.poll() is not None:
            raise ToolError('lifecycle format watcher failed readiness; see private logs')
        locked = runner.json('watch-status', acceptable=(0, 1))['watching'] is True
        # The lease precedes notification registration and the flushed event.
        # Observe a complete line while the child lives, never after termination
        # (which would also flush buffered output).
        if log_path.stat().st_size > runner.output_budget:
            raise ToolError('lifecycle format watch stream exceeded its output budget')
        with log_path.open('rb') as stream:
            line = stream.readline(runner.output_budget + 1)
        if locked and line.endswith(b'\n'):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                return False
            return event == {'command': 'watch', 'status': 'watching', 'root': str(runner.root)}
        if time.monotonic() >= deadline:
            if not locked:
                raise ToolError('lifecycle format watcher failed readiness; see private logs')
            return False
        time.sleep(.05)


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('lifecycle format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='lifecycle-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    expected, actual = {}, {}
    ansi_ids = iter(ANSI_ASSERTION_IDS)

    def record(key, want, got):
        expected[key], actual[key] = want, got

    def decode(output):
        try:
            return json.loads(output)
        except ValueError:
            return '<invalid-json>'

    def command(format, *args, acceptable=(0, 1)):
        code, output = runner.command('--format', format, *args, acceptable=acceptable)
        try:
            identity = next(ansi_ids)
        except StopIteration:
            raise ToolError('new lifecycle command needs a retained ANSI assertion ID') from None
        record('ansi:' + str(identity), False, '\x1b' in output)
        return code, output

    def counts(files, modules=0):
        # Symbols and refs are internal metadata checks, not independent declarations.
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as state:
            values = {key: state.execute(f'SELECT count(*) FROM {key}').fetchone()[0]
                      for key in ('symbols', 'refs')}
        return {'files': files, 'modules': modules, **values}

    def summary(label, format, args, files, status='complete', modules=0):
        code, output = command(format, *args)
        if format == 'json':
            want = {'command': args[0], 'status': status}
            if args[0] == 'rebuild':
                want['index_type'] = args[args.index('--type') + 1] if '--type' in args else 'all'
            if status == 'complete':
                want.update(counts(files, modules))
            if args[0] == 'restore':
                want.update(source=str(args[1]), db_path=str(database))
            record(label, (0, want), (code, decode(output)))
        else:
            fragments = {'rebuild': ['Indexed'],
                         'update': ['Checking for changes...', 'Index is up to date.'],
                         'restore': ['Restored index from:', 'DB path:', 'Contains:']}[args[0]]
            if args[0] == 'rebuild' and '--sub-projects' in args:
                fragments = ['Done:', 'files']
            record(label, (0, True), (code, all(part in output for part in fragments)))

    # Full type inventory also sees ignored/generated files. No feature is skipped.
    (runner.root / 'build').mkdir()
    (runner.root / 'build/Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    for name in ('Alpha', 'Removed'):
        (runner.root / f'{name}.java').write_text(f'class {name} {{}}\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        record('inventory', {'.java': 2, '.kt': 1, '.xml': 1}, inventory)
        if inventory != {'.java': 2, '.kt': 1, '.xml': 1}:
            raise ToolError('lifecycle fixture full inventory incomplete')
    finally:
        state.close()

    def declarations(label, names):
        rows = runner.json('class', '--pattern', '*', '--limit', '100')
        record(label, sorted((f'{file}.java', name, 1) for file, name in names),
               sorted((row['path'], row['name'], row['line']) for row in rows['items']))
        record(label + ':complete', False, rows['pagination']['truncated'])

    for format in ('json', 'text'):
        code, output = command(format, 'update')
        record('missing:update:' + format,
               (1, '') if format == 'json' else (0, "Index not found. Run 'ast-index rebuild' first.\n"),
               (code, output))
        code, output = command(format, 'watch')
        record('missing:watch:' + format,
               (1, '') if format == 'json' else (0, "Index not found. Run 'ast-index rebuild' first.\n"),
               (code, output))
        code, output = command(format, 'clear')
        want = {'command': 'clear', 'status': 'complete', 'root': str(runner.root)}
        record('clear:missing:' + format, (0, want if format == 'json' else f'Index cleared for {runner.root}\n'),
               (code, decode(output) if format == 'json' else output))
        record('clear:missing:state:' + format, False, database.exists())
    code, output = command('json', 'rebuild', '--type', 'invalid')
    record('invalid:rebuild', (1, '', False), (code, output, database.exists()))
    summary('no-sub-projects:json', 'json', ['rebuild', '--sub-projects'], 0, 'no-sub-projects')
    record('no-sub-projects:preserved-missing', False, database.exists())
    for format in ('json', 'text'):
        for index_type in ('all', 'files', 'symbols', 'modules', 'deps'):
            summary('rebuild:' + index_type + ':' + format, format,
                    ['rebuild', '--force', '--type', index_type], 2)
        summary('unchanged:' + format, format, ['update'], 2)
    declarations('initial:source', [('Alpha', 'Alpha'), ('Removed', 'Removed')])
    backup = directory / 'snapshot.sqlite'
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    (runner.root / 'Alpha.java').write_text('class Beta {}\n')
    (runner.root / 'Removed.java').unlink()
    (runner.root / 'Gamma.java').write_text('class Gamma {}\n')
    summary('changed:json', 'json', ['update', '--verbose'], 2)
    declarations('changed:source', [('Alpha', 'Beta'), ('Gamma', 'Gamma')])
    for format in ('json', 'text'):
        summary('restore:' + format, format, ['restore', str(backup)], 2)
        declarations('restored:source:' + format, [('Alpha', 'Alpha'), ('Removed', 'Removed')])
        code, output = command(format, 'restore', str(database))
        record('restore:self:' + format, (1, ''), (code, output))
        declarations('restore:self:preserved:' + format, [('Alpha', 'Alpha'), ('Removed', 'Removed')])
        summary('restore:update:' + format, 'json', ['update'], 2)
    # Queuing is reported as pending work; publication is independently observed.
    (runner.root / 'Gamma.java').write_text('class Queued {}\n')
    code, output = command('json', 'update', '--background', '--debounce-ms', '10')
    value = decode(output)
    record('background:json', (0, True), (code, isinstance(value, dict) and
           set(value) == {'command', 'status', 'generation'} and value['command'] == 'update' and
           value['status'] == 'queued' and type(value['generation']) is int and value['generation'] > 0))
    summary('background:wait', 'json', ['update'], 2)
    declarations('background:source', [('Alpha', 'Beta'), ('Gamma', 'Queued')])

    # JSON watch emits one object per event and flushes its readiness event.
    log_path = directory / 'watch.stdout.log'
    with log_path.open('wb') as stdout, (directory / 'watch.stderr.log').open('wb') as stderr:
        child = subprocess.Popen([str(runner.binary), '--format', 'json', 'watch'],
                                 cwd=runner.root, env=runner.environment, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 12
            record('watch:flushed', True, watch_readiness(runner, child, log_path, deadline))
            code, output = command('json', 'watch')
            record('watch:singleton', (0, {'command': 'watch', 'status': 'already-running'}), (code, decode(output)))
            for format in ('json', 'text'):
                code, output = command(format, 'watch-status')
                record('watch:active:' + format, (0, {'watching': True} if format == 'json' else 'watching\n'),
                       (code, decode(output) if format == 'json' else output))
                code, output = command(format, 'watch-status', '--quiet')
                record('watch:quiet:' + format, (0, ''), (code, output))
            # Startup and rendering probes must not consume the source-change budget.
            deadline = time.monotonic() + 12
            last_touch = 0
            while True:
                if time.monotonic() - last_touch >= 1:
                    (runner.root / 'Gamma.java').write_text('class Watched {}\n')
                    last_touch = time.monotonic()
                rows = runner.json('class', 'Watched')['items']
                if rows:
                    break
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise ToolError('lifecycle format watcher did not publish source change')
                time.sleep(.1)
            declarations('watch:source', [('Alpha', 'Beta'), ('Gamma', 'Watched')])
        finally:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
    if log_path.stat().st_size > runner.output_budget:
        raise ToolError('lifecycle format watch stream exceeded its output budget')
    with log_path.open() as stream:
        events = [decode(line) for line in stream if line.strip()]
    record('watch:events', True, len(events) >= 2 and
           events[0] == {'command': 'watch', 'status': 'watching', 'root': str(runner.root)} and
           all(isinstance(event, dict) and event.get('command') == 'watch' and
               event.get('status') == 'updated' and type(event.get('updated')) is int and
               event['updated'] >= 0 and type(event.get('deleted')) is int and event['deleted'] >= 0
               for event in events[1:]) and any(event['updated'] > 0 for event in events[1:]))
    for format in ('json', 'text'):
        code, output = command(format, 'watch-status')
        record('watch:stopped:' + format, (1, {'watching': False} if format == 'json' else 'not-watching\n'),
               (code, decode(output) if format == 'json' else output))
    for source in runner.root.glob('*.java'):
        source.unlink()
    for format in ('json', 'text'):
        summary('empty:rebuild:' + format, format, ['rebuild', '--force'], 0)
        declarations('empty:source:' + format, [])
        code, output = command(format, 'clear')
        record('clear:populated:' + format,
               (0, {'command': 'clear', 'status': 'complete', 'root': str(runner.root)} if format == 'json'
                else f'Index cleared for {runner.root}\n'),
               (code, decode(output) if format == 'json' else output))
        record('clear:populated:state:' + format, False, database.exists())
    # Exercise the separate sub-project publication path and its progress output.
    child_root = runner.root / 'child'
    child_root.mkdir()
    (child_root / 'Mini.java').write_text('class Mini {}\n')
    (child_root / 'pom.xml').write_text('<project><modelVersion>4.0.0</modelVersion>'
        '<groupId>fixture</groupId><artifactId>mini</artifactId><version>1</version></project>\n')
    for format in ('json', 'text'):
        summary('sub-projects:' + format, format,
                ['rebuild', '--force', '--sub-projects', '--include', 'child'], 1, modules=1)
        rows = runner.json('class', 'Mini')['items']
        record('sub-projects:source:' + format, [('child/Mini.java', 'Mini', 1)],
               [(row['path'], row['name'], row['line']) for row in rows])
    if next(ansi_ids, None) is not None:
        raise ToolError('lifecycle fixture omitted a retained ANSI assertion')
    return {FEATURE: expected}, {FEATURE: actual}
