"""Java CLI selector/availability failures on disposable fixtures, not MCP truth."""
from pathlib import Path
import json
import tempfile
import tomllib

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

SELECTORS = 'global:format:java-selector-errors'
AVAILABILITY = 'global:format:java-index-availability'
FEATURES = {SELECTORS, AVAILABILITY}
REASON = ('internal CLI: disposable Java global selector conflicts, unsupported formats, '
          'all Java-applicable CLI command coverage, command-specific scope guard precedence, version root exception, '
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
    ['hotspots'], ['changed'], ['db-path'], ['watch-status'], ['detect-stacks'], ['version'],
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
    ['install-claude-plugin'],
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
# These are the complete command-specific predicates in main's shared scope
# validation. Collection variants can mutate state, so rejection precedes VCS.
SCOPE_GUARDS = [
    ('changed:subtree', ['changed'], ['--subtree', 'absent'], 'not supported'),
    ('hotspots:subtree', ['hotspots'], ['--subtree', 'absent'], 'not supported'),
    ('hotspots-collect:subtree', ['hotspots', '--collect'], ['--subtree', 'absent'], 'not supported'),
    ('hotspots-full:subtree', ['hotspots', '--full'], ['--subtree', 'absent'], 'not supported'),
    *[(f'graph-{action}:{selector}', ['graph', action],
       ['--local'] if selector == 'local' else ['--subtree', 'absent'], 'do not apply')
      for action in ('build', 'status') for selector in ('local', 'subtree')],
]


def acceptance_keys(feature):
    """Finite execution obligations independent of returned fixture samples."""
    keys = {'source-preservation', 'inventory'}
    if feature == SELECTORS:
        keys.update({'no-source-tree-mutation', 'scoped-graph-positive'})
        for args in READS + MUTATIONS:
            label = ':'.join(args)
            keys.add(f'no-index:{label}')
            for suffix in (False, True):
                for fmt in ('text', 'json', 'mermaid', 'dot'):
                    keys.add(f'conflict:{label}:{fmt}:{suffix}')
                    if fmt in ('text', 'json'):
                        keys.add(f'root:{label}:{fmt}:{suffix}')
                    elif args[0] != 'module-route':
                        keys.add(f'diagram:{label}:{fmt}:{suffix}')
                keys.update(f'format-value:{label}:{fmt}:{suffix}' for fmt in ('yaml', 'JSON'))
        for phase in ('missing', 'populated'):
            keys.update(f'guards:{phase}:{case}' for case in
                        ('no-index-mutation', 'no-tree-mutation', 'no-cache-mutation'))
            for label, _, _, _ in SCOPE_GUARDS:
                for fmt in ('text', 'json', 'mermaid', 'dot'):
                    for suffix in (False, True):
                        keys.update({f'guard:{phase}:{label}:{fmt}:{suffix}',
                                     f'guard:{phase}:{label}:{fmt}:{suffix}:valid-root',
                                     f'guard-conflict:{phase}:{label}:{fmt}:{suffix}'})
    elif feature == AVAILABILITY:
        for phase in ('missing', 'unrecognizable'):
            keys.add(phase + ':no-index-mutation')
            for args in INDEXED:
                keys.update(f'{phase}:{args}:{fmt}:{suffix}'
                            for fmt in ('text', 'json') for suffix in (False, True))
            keys.update(f'{phase}:structured:{args}' for args in GRAPH_QUERIES)
        keys.update({'indexed-declarations', 'unbuilt-status', 'built-nodes',
                     'built-java-edge', 'built-status', 'indexed-hotspots',
                     'indexed-text:graph:build', 'indexed-text:graph:status', 'indexed-text:hotspots'})
    else:
        raise ToolError('unknown selector acceptance family')
    return keys


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
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'complete shared selector guard matrix')=0",
                      ('; complete shared selector guard matrix (every Java CLI command, all scope '
                       'guards, format/conflict/root precedence and version exception) executes separately; '
                       'operation-specific source I/O, recognizable damaged DB/query failures, '
                       'restore/publication and delegate failure composition remain pending',))


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
    version = tomllib.loads((Path(__file__).resolve().parents[2] / 'Cargo.toml').read_text())['package']['version']

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
            for fmt in ('text', 'json', 'mermaid', 'dot'):
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
                if fmt not in ('text', 'json'):
                    continue
                code, output, stderr = run(args, fmt, suffix,
                    {'AST_INDEX_ROOT': str(directory / 'absent')})
                # Version explicitly bypasses project-root discovery. It must
                # still reject scope conflicts and unsupported formats.
                if args[0] == 'version':
                    want = {'name': 'ast-index', 'version': version} if fmt == 'json' else f'ast-index v{version}\n'
                    try:
                        got = json.loads(output) if fmt == 'json' else output
                    except ValueError:
                        got = '<invalid-json>'
                    record(SELECTORS, f'root:{label}:{fmt}:{suffix}',
                           {'exit': 0, 'version': want, 'stderr': ''},
                           {'exit': code, 'version': got, 'stderr': stderr})
                    continue
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
            for fmt in ('yaml', 'JSON'):
                code, output, stderr = run(args, fmt, suffix,
                    {'AST_INDEX_ROOT': str(directory / 'absent')})
                record(SELECTORS, f'format-value:{label}:{fmt}:{suffix}',
                       {'exit': 2, 'stdout': '', 'diagnostic': True},
                       {'exit': code, 'stdout': output, 'diagnostic':
                        'invalid value' in stderr and '--format' in stderr})
        record(SELECTORS, f'no-index:{label}', False, database.exists())
    record(SELECTORS, 'no-source-tree-mutation',
           {path: Path(path).suffix for path in sources},
           {row[0]: row[1] for row in mobile_contracts.inventory_rows(runner.root)})

    def scope_guards(phase):
        # Trap root/cache discovery after each guard. Even a missing index must
        # not hide an invalid scope; an existing index must not be changed.
        trap = directory / 'cache-trap'
        trap.write_text('not a directory\n')
        before = file_sha256(database) if database.exists() else None
        sources_before = list(mobile_contracts.inventory_rows(runner.root))
        for label, args, selectors, diagnostic in SCOPE_GUARDS:
            for fmt in ('text', 'json', 'mermaid', 'dot'):
                for suffix in (False, True):
                    scoped = ([*args, *selectors, '--walk-up'] if suffix else
                              ['--walk-up', *selectors, *args])
                    code, output, stderr = run(scoped, fmt, suffix, {
                        'AST_INDEX_ROOT': str(directory / 'absent'),
                        'AST_INDEX_CACHE_DIR': str(trap),
                        'AST_INDEX_DB_PATH': str(trap / 'index.sqlite'),
                    })
                    marker = 'supported only' if fmt in ('mermaid', 'dot') else diagnostic
                    record(SELECTORS, f'guard:{phase}:{label}:{fmt}:{suffix}',
                           {'exit': 1, 'stdout': '', 'diagnostic': True, 'root_discovery': False},
                           {'exit': code, 'stdout': output, 'diagnostic': marker in stderr,
                            'root_discovery': 'AST_INDEX_ROOT' in stderr})
                    code, output, stderr = run(scoped, fmt, suffix)
                    record(SELECTORS, f'guard:{phase}:{label}:{fmt}:{suffix}:valid-root',
                           {'exit': 1, 'stdout': '', 'diagnostic': True},
                           {'exit': code, 'stdout': output, 'diagnostic': marker in stderr})
                    # The scope conflict wins even over a valid diagram value
                    # unsupported by this command, or the command-specific guard.
                    conflict = [*scoped, '--subtree', 'absent'] if '--local' in selectors else [*scoped, '--local']
                    code, output, stderr = run(conflict, fmt, suffix)
                    record(SELECTORS, f'guard-conflict:{phase}:{label}:{fmt}:{suffix}',
                           {'exit': 2, 'stdout': '', 'conflict': True},
                           {'exit': code, 'stdout': output, 'conflict': 'mutually exclusive' in stderr})
        record(SELECTORS, f'guards:{phase}:no-index-mutation', before,
               file_sha256(database) if database.exists() else None)
        record(SELECTORS, f'guards:{phase}:no-tree-mutation', sources_before,
               list(mobile_contracts.inventory_rows(runner.root)))
        record(SELECTORS, f'guards:{phase}:no-cache-mutation', 'not a directory\n', trap.read_text())

    scope_guards('missing')

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
    scope_guards('populated')
    scoped = runner.json('--local', 'graph', 'dependencies', 'entry')
    record(SELECTORS, 'scoped-graph-positive', [('ping', 2)],
           [(row.get('other', {}).get('name'), row.get('other', {}).get('line'))
            for row in scoped.get('items', [])])
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
