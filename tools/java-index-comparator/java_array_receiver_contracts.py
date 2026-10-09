"""Java array element identities: authored source/javac/CLI, not MCP evidence."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
from java_receiver_contracts import call_edges
import mobile_contracts

GRAPH = 'graph:java-array-receivers'
EXPLORE = 'explore:java-array-receivers'
FEATURES = {GRAPH, EXPLORE}
REASON = ('independent source/javac/CLI: array rank and explicit generic element '
          'projections across fields, inherited owners, method/record results, '
          'declaration sites, captures and attached roots; not MCP equivalence')
DECLARATIONS = '''package arrays;
class Leaf {
 int arrayMarker(){return 1;}
}
class Box<T> { T[] values; T[][] matrix; T trailing[]; T[] get(){return null;} T getTrailing()[]{return null;} }
class Relay<U> extends Box<U> {}
class Fixed extends Relay<Leaf> {}
class Bound<T extends Leaf> { T[] values; }
record Packet<T>(T[] values) {}
class Nest<T> { java.util.List<T>[] values; }
class Hidden extends Box<Leaf> { int[] values; }
class MethodShadow<T> { <T> T[] get(){return null;} }
'''
# Each phase is bounded below explore's ten-neighbour result cap. Expectations
# name authored endpoint declarations, never native DB rows or native output.
PHASES = (
    {
        'field': 'int field(Box<Leaf> b){return b.values[0].arrayMarker();}',
        'matrix': 'int matrix(Box<Leaf> b){return b.matrix[0][0].arrayMarker();}',
        'result': 'int result(Box<Leaf> b){return b.get()[0].arrayMarker();}',
        'record': 'int record(Packet<Leaf> b){return b.values()[0].arrayMarker();}',
        'nested': 'int nested(Nest<Leaf> b){return b.values[0].get(0).arrayMarker();}',
        'argument': 'int argument(Box<Leaf[]> b){return b.values[0][0].arrayMarker();}',
        'postfixField': 'int postfixField(Box<Leaf> b){return b.trailing[0].arrayMarker();}',
        'postfixResult': 'int postfixResult(Box<Leaf> b){return b.getTrailing()[0].arrayMarker();}',
    },
    {
        'inherited': 'int inherited(Relay<Leaf> b){return b.values[0].arrayMarker();}',
        'fixed': 'int fixed(Fixed b){return b.values[0].arrayMarker();}',
        'inheritedResult': 'int inheritedResult(Relay<Leaf> b){return b.get()[0].arrayMarker();}',
        'rawBound': 'int rawBound(Bound b){return b.values[0].arrayMarker();}',
        'wildcardBound': 'int wildcardBound(Bound<?> b){return b.values[0].arrayMarker();}',
        'bare': 'int bare(){return values[0].arrayMarker();}',
        'self': 'int self(){return this.values[0].arrayMarker();}',
        'parent': 'int parent(){return super.values[0].arrayMarker();}',
    },
    {
        'parameter': 'int parameter(Leaf[] a){class Leaf {} return a[0].arrayMarker();}',
        'local': 'int local(Leaf[] a){Leaf[] v=a; class Leaf {} return v[0].arrayMarker();}',
        'variable': 'int variable(Box<Leaf> b){var a=b.values; class Leaf {} return a[0].arrayMarker();}',
        'capture': 'int capture(Box<Leaf> b){return ((java.util.function.IntSupplier)()->b.values[0].arrayMarker()).getAsInt();}',
        'reference': 'int reference(Box<Leaf> b){return ((java.util.function.IntSupplier)b.values[0]::arrayMarker).getAsInt();}',
        'foreach': 'int foreach(Box<Leaf> b){for(var v:b.values){return v.arrayMarker();} return 0;}',
        'cast': 'int cast(Object a){return ((Leaf[])a)[0].arrayMarker();}',
        'creation': 'int creation(){return (new Leaf[1][1])[0][0].arrayMarker();}',
        'postfixParameter': 'int postfixParameter(Leaf a[]){class Leaf {} return a[0].arrayMarker();}',
        'postfixLocal': 'int postfixLocal(Leaf[] a){Leaf v[]=a; class Leaf {} return v[0].arrayMarker();}',
    },
)
GUARDS = {
    'rank': ('Box<Leaf> b', 'b.matrix[0].arrayMarker()'),
    'unindexed': ('Box<Leaf> b', 'b.values.arrayMarker()'),
    'raw': ('Box b', 'b.values[0].arrayMarker()'),
    'wildcard': ('Box<?> b', 'b.values[0].arrayMarker()'),
    'primitive': ('int[] a', 'a[0].arrayMarker()'),
    'shadow': ('Object a', '((int[])a)[0].arrayMarker()'),
    'missing': ('Box<Leaf> b', 'b.values[0].missing()'),
    'localShadow': ('', 'class Leaf {} Leaf[] a=null; return a[0].arrayMarker()'),
    'block': ('Box<Leaf> b', '{var a=b.values;} return a[0].arrayMarker()'),
    'static': ('', 'values[0].arrayMarker()'),
    'hidden': ('Hidden b', 'b.values[0].arrayMarker()'),
    'methodShadow': ('MethodShadow<Leaf> b', 'b.get()[0].arrayMarker()'),
    'overIndex': ('Box<Leaf> b', 'b.values[0][0].arrayMarker()'),
}


def plan_arrays(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-array-receivers-v1'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        note = ('; separate executable Java array endpoint checklist covers array rank, '
                'explicit generic fields/inheritance, method/record results, declaration '
                'sites/captures and attached roots; retained overload/inference/access '
                'and other parent obligations remain pending; not MCP equivalence')
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' AND instr(reason,?)=0",
                          (note, parent, note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('array receiver artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-array-receivers-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    source = runner.root / 'Probe.java'
    (runner.root / 'Sentinel.kt').write_text('// inventory sentinel\n')
    (runner.root / 'sentinel.xml').write_text('<fixture/>\n')
    source.write_text(DECLARATIONS)
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': 1, '.kt': 1, '.xml': 1}:
            raise ToolError('applicable array receiver inventory incomplete')
    finally:
        inventory.close()
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    def compile_source(label):
        with (runner.directory / (label + '.javac.log')).open('wb') as log:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'), str(source)],
                                  stdout=log, stderr=log, timeout=30).returncode

    def identity(row):
        path = runner.path(row['path'])
        return path, row['line'], row['name']

    def endpoints(doc):
        return sorted((runner.path(path), line, name) for (path, line, name), _ in call_edges(doc)
                      if name in ('arrayMarker', 'missing'))

    record(GRAPH, 'inventory', {'.java': 1, '.kt': 1, '.xml': 1}, counts)
    attached = runner.directory / 'attached'
    attached.mkdir()
    for phase, methods in enumerate(PHASES):
        body = DECLARATIONS + 'class Probe extends Fixed {\n' + '\n'.join(methods.values()) + '\n}\n'
        source.write_text(body)
        (attached / 'Probe.java').write_text(body)
        if phase == 0:
            runner.command('rebuild', '--force')
            runner.command('subtree', 'add', 'extra', attached)
        if compile_source('positive-' + str(phase)):
            raise ToolError('authored array receiver sources failed javac; see private logs')
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')
        for prefix, flags in (('project/', ('--local',)), ('attached/', ('--subtree', 'extra'))):
            marker = (prefix + 'Probe.java', 3, 'arrayMarker')
            callers = {name: (prefix + 'Probe.java', next(i for i, line in enumerate(body.splitlines(), 1)
                                                        if 'int ' + name + '(' in line), name) for name in methods}
            for name, caller in callers.items():
                key = f'{phase}/{prefix}{name}'
                seed = 'arrays.Probe.' + name
                for ambiguous in (False, True):
                    for limit in (0, 1, 100):
                        doc = runner.json('graph', 'dependencies', seed, '--limit', limit, *flags,
                                          *(['--include-ambiguous'] if ambiguous else []))
                        hits = endpoints(doc)
                        record(GRAPH, key + f':{limit}:{ambiguous}',
                               {'matched': [caller], 'valid': True, 'complete': True, 'page': True},
                               {'matched': [identity(row) for row in doc['matched']],
                                'valid': all(hit == marker for hit in hits) and len(hits) <= 1,
                                'complete': limit < 100 or hits == [marker],
                                'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
                doc = runner.json('graph', 'path', seed, 'arrays.Leaf.arrayMarker', '--max-depth', 1, *flags)
                record(GRAPH, key + ':path', [(caller, marker)],
                       sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']))
            doc = runner.json('graph', 'dependents', 'arrays.Leaf.arrayMarker', '--limit', 100, *flags)
            record(GRAPH, f'{phase}/{prefix}reverse', sorted(callers.values()),
                   sorted(identity(row['other']) for row in doc['items']))
            doc = runner.json('explore', 'arrayMarker', '--rwr', '--max-files', 100, *flags)
            record(EXPLORE, f'{phase}/{prefix}callers',
                   sorted((p, line, 'arrays.Probe.' + name) for p, line, name in callers.values()),
                   sorted((runner.path(row['path']), row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    for name, (parameters, expression) in GUARDS.items():
        expression = expression if 'return ' in expression else 'return ' + expression
        source.write_text(DECLARATIONS + 'class Probe extends Fixed { ' + ('static ' if name == 'static' else '') +
                          'int invalid(' + parameters + '){' + expression + ';} }\n')
        if not compile_source('negative-' + name):
            raise ToolError('javac accepted an array receiver negative guard')
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')
        for ambiguous in (False, True):
            doc = runner.json('graph', 'dependencies', 'arrays.Probe.invalid', '--local',
                              *(['--include-ambiguous'] if ambiguous else []))
            record(GRAPH, name + ':guard:' + str(ambiguous), [], endpoints(doc))
    for stage, element in (('initial', 'Leaf'), ('changed', 'Object'), ('restored', 'Leaf')):
        source.write_text(DECLARATIONS + 'class Probe { int refresh(' + element +
                          '[] a){return a[0].arrayMarker();} }\n')
        runner.command('update')
        doc = runner.json('graph', 'dependencies', 'arrays.Probe.refresh', '--local', '--refresh')
        record(GRAPH, 'refresh:' + stage,
               [('project/Probe.java', 3, 'arrayMarker')] if element == 'Leaf' else [], endpoints(doc))
    return expected, actual
