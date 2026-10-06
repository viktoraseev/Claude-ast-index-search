"""Java CLI selector/availability failures on disposable fixtures, not MCP truth."""
from pathlib import Path
import json
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

SELECTORS = 'global:format:java-selector-errors'
AVAILABILITY = 'global:format:java-index-availability'
FEATURES = {SELECTORS, AVAILABILITY}
REASON = ('internal CLI: disposable Java global selector conflicts, unsupported formats, '
          'explicit-root failures and missing/unrecognizable index responses; full fixture '
          'inventory and no source/index mutation on rejected requests; not MCP equivalence')
MISSING = "Index not found. Run 'ast-index rebuild' first."
# Commands share pre-dispatch validation; arguments are small authored Java queries.
# Mutation commands are invoked only with invalid selectors, inside .artifacts.
READS = [
    ['search', 'Probe'], ['class', 'Probe'], ['symbol', 'Probe'], ['file', 'Probe'],
    ['hierarchy', 'Probe'], ['implementations', 'Probe'], ['refs', 'Probe'],
    ['usages', 'Probe'], ['outline', 'Probe.java'], ['imports', 'Probe.java'],
    ['api', ''], ['unused-symbols'], ['explore', 'Probe'], ['map'], ['conventions'],
    ['module', ''], ['deps', 'fixture'], ['dependents', 'fixture'],
    ['unused-deps', 'fixture'], ['module-route', '--from', 'fixture', '--to', 'other'],
    ['stats'], ['query', 'SELECT 1'], ['schema'], ['list-roots'], ['subtree', 'list'],
    ['hotspots'], ['changed'], ['db-path'], ['watch-status'], ['detect-stacks'],
    ['todo'], ['callers', 'ping'], ['call-tree', 'ping'], ['annotations', 'Deprecated'],
    ['deprecated'], ['suppress'], ['inject', 'Object'], ['provides', 'Object'],
    ['deeplinks'], ['xml-usages', 'Probe'], ['resource-usages', 'R.string.label'],
    ['agrep', 'class Probe {}', '--lang', 'java'],
    *[['graph', action, 'ping'] for action in ['dependencies', 'dependents', 'impact', 'metrics']],
    ['graph', 'path', 'ping', 'ping'], ['graph', 'cycles'],
    ['graph', 'top'], ['graph', 'status'],
]
MUTATIONS = [
    ['rebuild'], ['update'], ['restore', 'missing.sqlite'], ['clear'], ['watch'],
    ['graph', 'build'], ['add-root', '../absent'], ['remove-root', '../absent'],
    ['subtree', 'add', 'absent', '../absent'], ['subtree', 'remove', 'absent'],
    ['install-git-hooks', '--dry-run'], ['install-codex-mcp', '--dry-run'],
]
# Lexical scans have no index prerequisite. Graph queries retain their existing
# structured unavailable response; graph build/status use the indexed-read hint.
# File views/usages support source fallbacks, and modules have command-specific
# structured unavailable envelopes. Their established fixtures cover those
# behaviours; an unavailable index must not turn them into new uniform errors.
INDEXED = [args for args in READS if args[0] in {
    'search', 'class', 'symbol', 'file', 'hierarchy', 'implementations', 'refs',
    'unused-symbols', 'explore', 'map', 'conventions', 'stats', 'query', 'schema',
    'list-roots', 'subtree', 'hotspots'}] + [['graph', 'build'], ['graph', 'status']]
GRAPH_QUERIES = [args for args in READS if args[0] == 'graph' and args[1] != 'status']


def plan_errors(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-selector-errors'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java selector error composition')=0",
                      ('; Java selector error composition and indexed availability have separate '
                       'executed internal CLI contracts; other compound command-specific errors remain unresolved',))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('selector error fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='selector-errors-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    sources = {'Probe.java': 'class Probe {\n void ping() {}\n void entry() { ping(); }\n}\n',
               'inventory.txt': 'full inventory sentinel\n',
               'ignored/Inventory.kt': '// inventory only; no non-Java behaviour asserted\n'}
    for path, source in sources.items():
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        files = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if files != {path: Path(path).suffix for path in sources}:
            raise ToolError('selector fixture full inventory incomplete')
    finally:
        state.close()
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    def run(args, fmt, suffix, environment=None):
        flags = ['--format', fmt]
        code, output = runner.command(*(args + flags if suffix else flags + args),
                                      environment=environment, acceptable=(0, 1, 2))
        with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
            stderr = stream.read(runner.output_budget + 1)
        if len(stderr) > runner.output_budget:
            raise ToolError('selector fixture diagnostic exceeded its budget')
        return code, output, stderr.decode('utf-8')

    for args in READS + MUTATIONS:
        label = ':'.join(args)
        for suffix in (False, True):
            for fmt in ('text', 'json'):
                # A conflict is a usage failure, before command-specific guards,
                # root validation, cache discovery, delegates or mutations.
                conflict = (['--local', '--subtree', 'absent', *args] if not suffix else
                            [*args, '--subtree', 'absent', '--local'])
                code, output, stderr = run(conflict, fmt, suffix,
                    {'AST_INDEX_ROOT': str(directory / 'absent')})
                record(SELECTORS, f'conflict:{label}:{fmt}:{suffix}',
                       {'exit': 2, 'stdout': '', 'diagnostic': True},
                       {'exit': code, 'stdout': output, 'diagnostic':
                        '--local' in stderr and '--subtree' in stderr and 'mutually exclusive' in stderr})
                code, output, stderr = run(args, fmt, suffix,
                    {'AST_INDEX_ROOT': str(directory / 'absent')})
                record(SELECTORS, f'root:{label}:{fmt}:{suffix}',
                       {'exit': 1, 'stdout': '', 'diagnostic': True},
                       {'exit': code, 'stdout': output, 'diagnostic': 'AST_INDEX_ROOT' in stderr})
            for fmt in ('mermaid', 'dot'):
                if args[0] == 'module-route':
                    continue  # Valid diagram output has its own route contract.
                code, output, stderr = run(args, fmt, suffix,
                    {'AST_INDEX_ROOT': str(directory / 'absent')})
                record(SELECTORS, f'diagram:{label}:{fmt}:{suffix}',
                       {'exit': 1, 'stdout': '', 'diagnostic': True},
                       {'exit': code, 'stdout': output, 'diagnostic': 'supported only' in stderr})
        record(SELECTORS, f'no-index:{label}', False, database.exists())
    record(SELECTORS, 'no-source-tree-mutation',
           {path: Path(path).suffix for path in sources},
           {row[0]: row[1] for row in mobile_contracts.inventory_rows(runner.root)})

    for availability in ('missing', 'unrecognizable'):
        if availability == 'unrecognizable':
            database.write_bytes(b'authored invalid sqlite fixture')
        before = file_sha256(database) if database.exists() else None
        for args in INDEXED:
            for fmt in ('text', 'json'):
                for suffix in (False, True):
                    code, output, stderr = run(args, fmt, suffix)
                    programmatic = fmt == 'json' or args[0] in {'query', 'schema'}
                    record(AVAILABILITY, f'{availability}:{args}:{fmt}:{suffix}',
                           {'exit': 1 if programmatic else 0,
                            'stdout': '' if programmatic else MISSING + '\n',
                            'diagnostic': programmatic},
                           {'exit': code, 'stdout': output, 'diagnostic': MISSING in stderr})
        for args in GRAPH_QUERIES:
            code, output, stderr = run(args, 'json', True)
            try:
                doc = json.loads(output)
            except ValueError:
                doc = None
            record(AVAILABILITY, f'{availability}:structured:{args}',
                   {'exit': 0, 'document': {'error': "index not found; run 'ast-index rebuild' first",
                                         'graph': {'built': False, 'stale': False}}, 'stderr': ''},
                   {'exit': code, 'document': doc, 'stderr': stderr})
        record(AVAILABILITY, availability + ':no-index-mutation', before,
               file_sha256(database) if database.exists() else None)
    # Positive controls execute the same commands with an actual Java index;
    # unconditional failure or a JSON-shaped empty placeholder cannot pass.
    database.unlink()
    runner.command('rebuild', '--force')
    declarations = runner.json('outline', 'Probe.java')['symbols']
    record(AVAILABILITY, 'indexed-declarations', [('Probe', 'class'), ('ping', 'function'), ('entry', 'function')],
           [(row['name'], row['kind']) for row in declarations])
    status = runner.json('graph', 'status')
    record(AVAILABILITY, 'unbuilt-status', {'built': False, 'stale': False},
           {key: status.get('graph', {}).get(key) for key in ('built', 'stale')})
    built = runner.json('graph', 'build')
    record(AVAILABILITY, 'built-nodes', 2, built.get('nodes'))
    dependencies = runner.json('graph', 'dependencies', 'entry')
    record(AVAILABILITY, 'built-java-edge', [('ping', 2)],
           [(row.get('other', {}).get('name'), row.get('other', {}).get('line'))
            for row in dependencies.get('items', [])])
    status = runner.json('graph', 'status')
    record(AVAILABILITY, 'built-status', {'built': True, 'stale': False},
           {key: status.get('graph', {}).get(key) for key in ('built', 'stale')})
    hotspots = runner.json('hotspots')
    record(AVAILABILITY, 'indexed-hotspots', {'items': [], 'commits_analyzed': 0},
           {key: hotspots.get(key) for key in ('items', 'commits_analyzed')})
    for args, marker in ((['graph', 'build'], 'Symbol graph: 2 nodes'),
                         (['graph', 'status'], 'up to date'),
                         (['hotspots'], 'No git signals collected')):
        code, output, stderr = run(args, 'text', True)
        record(AVAILABILITY, 'indexed-text:' + ':'.join(args),
               {'exit': 0, 'rendered': True, 'stderr': ''},
               {'exit': code, 'rendered': marker.lower() in output.lower(), 'stderr': stderr})
    for feature in FEATURES:
        record(feature, 'source-preservation', sources,
               {path: (runner.root / path).read_text() for path in sources})
        record(feature, 'inventory', {path: Path(path).suffix for path in sources}, files)
    return expected, actual
