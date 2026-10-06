"""Java receiver byte sites: javac/authored identities, not MCP equivalence."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect
import mobile_contracts
from root_contracts import Runner
from java_receiver_contracts import call_edges, identity

GRAPH = 'graph:java-same-line-receiver-sites'
EXPLORE = 'explore:java-same-line-receiver-sites'
FEATURES = {GRAPH, EXPLORE}
SOURCES = {
    'Leaf.java': 'package bytesites;\nclass Leaf { int marker() { return 1; }\n int isolated() { return 1; } }\n',
    'Holder.java': 'package bytesites;\nclass Holder { Leaf leaf; Leaf get() { return leaf; } }\n',
    'Box.java': 'package bytesites;\nclass Box<T> { T get() { return null; } }\n',
    'Probe.java': '''package bytesites;
import java.util.List;
class Probe {
 Leaf field;
 int parameter(Leaf input) { class Leaf {} Leaf local = null; return input.marker(); }
 int variable() { Leaf input = null; class Leaf {} Leaf local = null; return input.marker(); }
 int inferred() { var input = new Leaf(); class Leaf {} Leaf local = null; return input.marker(); }
 int fieldCall() { class Leaf {} Leaf local = null; return this.field.marker(); }
 int list() { List<Leaf> input = null; class Leaf {} Leaf local = null; return input.get(0).marker(); }
 int box() { Box<Leaf> input = null; class Leaf {} Leaf local = null; return input.get().marker(); }
 int chain() { Holder input = null; class Leaf {} Leaf local = null; return input.get().marker(); }
 int reference(Leaf input) { class Leaf {} Leaf local = null; java.util.function.IntSupplier task = input::marker; return 0; }
 int captured(Leaf input) { class Worker { int invoke() { class Leaf {} Leaf local = null; return input.marker(); } } return 0; }
 int block() { Leaf input = null; { class Leaf {} Leaf local = null; } return input.marker(); }
 int local() { Leaf before = null; class Leaf { int localOnly() { return 2; } } Leaf input = null; return input.localOnly(); }
 int localList() { Leaf before = null; class Leaf { int listOnly() { return 3; } } List<Leaf> input = null; return input.get(0).listOnly(); }
 int creation() { Leaf before = null; class Leaf { int creationOnly() { return 4; } } return new Leaf().creationOnly(); }
 int cast() { Leaf before = null; class Leaf { int castOnly() { return 5; } } return ((Leaf) null).castOnly(); }
 int sibling(Leaf input) { return input.isolated(); }
}
''',
}
PACKAGE_CALLERS = ('parameter', 'variable', 'inferred', 'fieldCall', 'list', 'box',
                   'chain', 'reference', 'invoke', 'block', 'sibling')
LOCAL_CALLERS = {'local': 'localOnly', 'localList': 'listOnly',
                 'creation': 'creationOnly', 'cast': 'castOnly'}


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('same-line site artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-byte-sites-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for name, source in SOURCES.items():
        (runner.root / name).write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('same-line site inventory incomplete')
    finally:
        inventory.close()

    def compile_sources(label):
        with (runner.directory / (label + '.stdout.log')).open('wb') as stdout, \
                (runner.directory / (label + '.stderr.log')).open('wb') as stderr:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                   *[str(runner.root / name) for name in SOURCES]],
                                  stdout=stdout, stderr=stderr, timeout=30).returncode
    if compile_sources('positive-javac'):
        raise ToolError('authored same-line receiver sites do not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    record(GRAPH, 'complete-inventory', {'.java': len(SOURCES), '.kt': 1, '.xml': 1}, counts)
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    marker = ('Leaf.java', 2, 'marker')
    callers = {}
    for name in (*PACKAGE_CALLERS, *LOCAL_CALLERS):
        line = next(i for i, text in enumerate(SOURCES['Probe.java'].splitlines(), 1)
                    if 'int ' + name + '(' in text)
        callers[name] = ('Probe.java', line, name)
        seed = name if name == 'invoke' else 'bytesites.Probe.' + name
        target = (('Leaf.java', 3, 'isolated') if name == 'sibling' else marker) if name in PACKAGE_CALLERS else ('Probe.java', line, LOCAL_CALLERS[name])
        # Direct declaration-site receivers have scoped confidence. The generic
        # projection uses the existing local hierarchy confidence.
        wanted = [(target, 'local' if name == 'localList' else 'scoped')]
        if name == 'box':
            wanted.append((('Box.java', 2, 'get'), 'scoped'))
        if name == 'chain':
            wanted.append((('Holder.java', 2, 'get'), 'scoped'))
        wanted.sort()
        for ambiguous in (False, True):
            for limit in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                calls = call_edges(doc)
                record(GRAPH, f'{name}:{limit}:{ambiguous}',
                       {'matched': [callers[name]], 'valid': True, 'complete': True, 'page': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'valid': all(edge in wanted for edge in calls) and len(set(calls)) == len(calls),
                        'complete': limit < 100 or calls == wanted,
                        'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
        path = runner.json('graph', 'path', seed,
                           'bytesites.Leaf.' + target[2] if name in PACKAGE_CALLERS else LOCAL_CALLERS[name],
                           '--max-depth', 1)
        record(GRAPH, name + ':path', [(callers[name], target)],
               sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']))
    # Explore exposes at most ten neighbours. Use two authored package methods
    # so every caller is checked, including the nested capture, without assuming
    # a larger native page than the documented CLI renderer supplies.
    for method in ('marker', 'isolated', *LOCAL_CALLERS.values()):
        names = ([n for n in PACKAGE_CALLERS if n != 'sibling'] if method == 'marker' else
                 ['sibling'] if method == 'isolated' else [n for n, m in LOCAL_CALLERS.items() if m == method])
        reverse = runner.json('graph', 'dependents',
                              'bytesites.Leaf.' + method if method in ('marker', 'isolated') else method, '--limit', 100)
        record(GRAPH, method + ':reverse', sorted(callers[n] for n in names),
               sorted(identity(row['other']) for row in reverse['items']))
        doc = runner.json('explore', method, '--rwr', '--max-files', 100)
        found = sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller')
        record(EXPLORE, method,
               sorted(('Probe.java', callers[n][1], n if n == 'invoke' else 'bytesites.Probe.' + n) for n in names),
               found)

    # A missing member on an applicable local type cannot regain a package call.
    (runner.root / 'Probe.java').write_text('''package bytesites;
class Probe {
 int invalid() { Leaf before = null; class Leaf {} Leaf input = null; return input.marker(); }
}
''')
    if not compile_sources('negative-javac'):
        raise ToolError('javac accepted a same-line local shadow member leak')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        record(GRAPH, 'missing-member:' + str(ambiguous), [],
               call_edges(runner.json('graph', 'dependencies', 'bytesites.Probe.invalid',
                                      *(['--include-ambiguous'] if ambiguous else []))))
    return expected, actual
