"""Colliding Java local type occurrences; authored/javac truth, not MCP truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_receiver_contracts import call_edges, identity

GRAPH = 'graph:java-colliding-type-sites'
EXPLORE = 'explore:java-colliding-type-sites'
FEATURES = {GRAPH, EXPLORE}
REASON = ('independent source/state: javac-validated same-line local type occurrences, '
          'member ownership, receiver sites, block/sibling boundaries, distinct parents, '
          'generic class/method parameter order, return variables and bounds, implicit '
          'record accessor sites, graph pages/reverse/path and RWR callers; not MCP equivalence')
SOURCE = '''package collisions;
class Left { int inheritedLeft() { return 1; } }
class Right { int inheritedRight() { return 2; } }
class Box<T> { T get() { return null; } }
class Leaf { int packageOnly() { return 0; } }
class Probe {
 int direct() { int n = 0; { class Leaf { int one() { return 1; } int shared() { return 0; } } Leaf a = null; n += a.one() + a.shared(); } { class Leaf { int two() { return 2; } int shared() { return 0; } } Leaf b = null; n += b.two() + b.shared(); } return n; }
 int creation() { int n = 0; { class Leaf { int createLeft() { return 1; } } n += new Leaf().createLeft(); } { class Leaf { int createRight() { return 2; } } n += new Leaf().createRight(); } return n; }
 int casts() { int n = 0; { class Leaf { int castLeft() { return 1; } } n += ((Leaf) null).castLeft(); } { class Leaf { int castRight() { return 2; } } n += ((Leaf) null).castRight(); } return n; }
 int generic() { int n = 0; { class Leaf { int genericLeft() { return 1; } } Box<Leaf> a = null; n += a.get().genericLeft(); } { class Leaf { int genericRight() { return 2; } } Box<Leaf> b = null; n += b.get().genericRight(); } return n; }
 int parents() { int n = 0; { class Leaf extends Left {} Leaf a = null; n += a.inheritedLeft(); } { class Leaf extends Right {} Leaf b = null; n += b.inheritedRight(); } return n; }
 int nested() { int n = 0; { class Leaf { class Inner { int innerLeft() { return 1; } } Inner left; } Leaf a = null; n += a.left.innerLeft(); } { class Leaf { class Inner { int innerRight() { return 2; } } Inner right; } Leaf b = null; n += b.right.innerRight(); } return n; }
 int captured() { int n = 0; { class Leaf { int captureLeft() { return 1; } } Leaf a = null; class Worker { int invokeLeft() { return a.captureLeft(); } } } { class Leaf { int captureRight() { return 2; } } Leaf b = null; class Worker { int invokeRight() { return b.captureRight(); } } } return n; }
 int outside() { Leaf a = null; return a.packageOnly(); }
}
'''
BINDINGS = {
    'direct': [('Probe.java', 7, 'one'), ('Probe.java', 7, 'two'),
               ('Probe.java', 7, 'shared'), ('Probe.java', 7, 'shared')],
    'creation': [('Probe.java', 8, 'createLeft'), ('Probe.java', 8, 'createRight')],
    'casts': [('Probe.java', 9, 'castLeft'), ('Probe.java', 9, 'castRight')],
    'generic': [('Probe.java', 10, 'genericLeft'), ('Probe.java', 10, 'genericRight'),
                ('Probe.java', 4, 'get')],
    'parents': [('Probe.java', 2, 'inheritedLeft'), ('Probe.java', 3, 'inheritedRight')],
    'nested': [('Probe.java', 12, 'innerLeft'), ('Probe.java', 12, 'innerRight')],
    'invokeLeft': [('Probe.java', 13, 'captureLeft')],
    'invokeRight': [('Probe.java', 13, 'captureRight')],
    'outside': [('Probe.java', 5, 'packageOnly')],
}
METADATA_SOURCE = '''package metadata;
class Alpha { int alpha() { return 1; } }
class Beta { int beta() { return 2; } }
class Probe {
 int fields() { int n = 0; { class Holder { Alpha slot; } Holder a = null; n += a.slot.alpha(); } { class Holder { Beta slot; } Holder b = null; n += b.slot.beta(); } return n; }
 int returns() { int n = 0; { class Holder { Alpha read() { return null; } } Holder a = null; n += a.read().alpha(); } { class Holder { Beta read() { return null; } } Holder b = null; n += b.read().beta(); } return n; }
 int references() { { class Holder { int refer() { return 1; } } Holder a = null; java.util.function.IntSupplier left = a::refer; } { class Holder { int refer() { return 2; } } Holder b = null; java.util.function.IntSupplier right = b::refer; } return 0; }
}
class Gamma {
 int gammaOnly() { return 1; }
}
class Delta {
 int deltaOnly() { return 2; }
}
class GenericProbe {
 int indices() { int n = 0; { class Holder<T> { T read() { return null; } } Holder<Gamma> a = null; n += a.read().gammaOnly(); } { class Holder<X,T> { T read() { return null; } } Holder<Gamma,Delta> b = null; n += b.read().deltaOnly(); } return n; }
 int parameters() { int n = 0; { class Holder<T,X> { T read() { return null; } } Holder<Gamma,Delta> a = null; n += a.read().gammaOnly(); } { class Holder<T,X> { X read() { return null; } } Holder<Gamma,Delta> b = null; n += b.read().deltaOnly(); } return n; }
 int classBounds() { int n = 0; { class Holder<T extends Gamma> { T read() { return null; } } Holder a = null; n += a.read().gammaOnly(); } { class Holder<T extends Delta> { T read() { return null; } } Holder b = null; n += b.read().deltaOnly(); } return n; }
 int methodBounds() { int n = 0; { class Holder { <T extends Gamma> T read() { return null; } } Holder a = null; n += a.read().gammaOnly(); } { class Holder { <T extends Delta> T read() { return null; } } Holder b = null; n += b.read().deltaOnly(); } return n; }
 int shadow() { class Holder<T extends Gamma> { <T extends Delta> T read() { return null; } } Holder<Gamma> a = null; return a.read().deltaOnly(); }
 int records() { int n = 0; { record Holder<T>(T read) {} Holder<Gamma> a = null; n += a.read().gammaOnly(); } { record Holder<X,T>(T read) {} Holder<Gamma,Delta> b = null; n += b.read().deltaOnly(); } return n; }
 int mixedRecords() { int n = 0; { record Holder<T>(T read) {} Holder<Gamma> a = null; n += a.read().gammaOnly(); } { record Holder<X,T>(T read) { public T read() { return read; } } Holder<Gamma,Delta> b = null; n += b.read().deltaOnly(); } return n; }
}
'''


def plan_types(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-colliding-type-sites'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        note = ('; separate Java occurrence fixture covers colliding same-line local types, '
                'member containers and inheritance through graph/exploration, and nominal '
                'same-name field/return metadata through graph; generic member/signature '
                'metadata and compiler dispatch remain pending; not MCP equivalence')
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (note, parent, note))
        generic_note = ('; separate executed same-line generic declaration metadata checklist '
                        'covers class parameter order, method return variables, class/method '
                        'bounds and shadowing, implicit record accessor ownership and negative '
                        'sibling-bound guards through graph pages/reverse/path and exact RWR '
                        'callers; generic field projections, callback/reference signature '
                        'metadata, generic inference and overload/attached-root dispatch remain '
                        'pending; independent source/state, not MCP equivalence')
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (generic_note, parent, generic_note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('colliding type fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-colliding-types-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    source = runner.root / 'Probe.java'
    source.write_text(SOURCE)
    metadata = runner.root / 'Receiver.java'
    metadata.write_text(METADATA_SOURCE)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': 2, '.kt': 1, '.xml': 1}:
            raise ToolError('colliding type inventory incomplete')
    finally:
        state.close()

    def compile_source(label):
        with (runner.directory / (label + '.stdout.log')).open('wb') as stdout, \
                (runner.directory / (label + '.stderr.log')).open('wb') as stderr:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                   str(source), str(metadata)], stdout=stdout, stderr=stderr, timeout=30).returncode
    if compile_source('positive-javac'):
        raise ToolError('authored colliding types do not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    record(GRAPH, 'inventory', {'.java': 2, '.kt': 1, '.xml': 1}, counts)
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for name, wanted in BINDINGS.items():
        seed = name if name.startswith('invoke') else 'collisions.Probe.' + name
        line = next(i for i, text in enumerate(SOURCE.splitlines(), 1) if 'int ' + name + '(' in text)
        caller = ('Probe.java', line, name)
        for ambiguous in (False, True):
            for limit in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                calls = call_edges(doc)
                members = [identity for identity, _ in calls]
                record(GRAPH, f'{name}:{limit}:{ambiguous}',
                       {'matched': [caller], 'valid': True, 'complete': True, 'page': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'valid': all(member in wanted for member in members),
                        'complete': limit < 100 or sorted(members) == sorted(wanted),
                        'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
        for target in sorted(set(wanted)):
            reverse = runner.json('graph', 'dependents', target[2], '--limit', 100)
            record(GRAPH, name + ':reverse:' + target[2], [caller],
                   sorted(set(identity(row['other']) for row in reverse['items'])))
            path = runner.json('graph', 'path', seed, target[2], '--max-depth', 1)
            wanted_paths = [(caller, target)] * wanted.count(target)
            record(GRAPH, name + ':path:' + target[2], wanted_paths,
                   sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']))
            doc = runner.json('explore', target[2], '--rwr', '--max-files', 100)
            callers = sorted(set((row['path'], row['line'], row['name']) for row in doc['neighbours']
                                 if row['link'] == 'caller'))
            qualified = 'collisions.Probe.' + name if not name.startswith('invoke') else name
            # Exploration searches declaration signatures as well as names.
            # Both captured callables share a physical signature line containing
            # both member names, so the authored broad seed union has two callers.
            # Exact dispatch is independently asserted by graph/reverse/path above.
            wanted_callers = ([('Probe.java', 13, 'invokeLeft'), ('Probe.java', 13, 'invokeRight')]
                              if name.startswith('invoke') else [('Probe.java', line, qualified)])
            record(EXPLORE, name + ':' + target[2], wanted_callers, callers)

    # Colliding members keep their nominal receiver types as well as their
    # containers. Native field/return rows are never the expected oracle.
    for method, line in (('fields', 5), ('returns', 6)):
        wanted = [('Receiver.java', 2, 'alpha'), ('Receiver.java', 3, 'beta')]
        if method == 'returns':
            wanted += [('Receiver.java', 6, 'read')] * 2
        for ambiguous in (False, True):
            doc = runner.json('graph', 'dependencies', 'metadata.Probe.' + method, '--limit', 100,
                              *(['--include-ambiguous'] if ambiguous else []))
            record(GRAPH, 'metadata:' + method + ':' + str(ambiguous), sorted(wanted),
                   sorted(target for target, _ in call_edges(doc)))
        for target in ('alpha', 'beta'):
            doc = runner.json('graph', 'path', 'metadata.Probe.' + method,
                              'metadata.' + ('Alpha' if target == 'alpha' else 'Beta') + '.' + target,
                              '--max-depth', 1)
            record(GRAPH, 'metadata:' + method + ':path:' + target,
                   [(('Receiver.java', line, method), ('Receiver.java', 2 if target == 'alpha' else 3, target))],
                   sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']))
    for target in ('alpha', 'beta'):
        doc = runner.json('graph', 'dependents', target, '--limit', 100)
        record(GRAPH, 'metadata:reverse:' + target,
               [('Receiver.java', 5, 'fields'), ('Receiver.java', 6, 'returns')],
               sorted(identity(row['other']) for row in doc['items']))
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'metadata.Probe.references', '--limit', 100,
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'metadata:references:' + str(ambiguous), [('Receiver.java', 7, 'refer')] * 2,
               sorted(target for target, _ in call_edges(doc)))

    # Same-line generic metadata must belong to the exact class/method site,
    # including parameter order, return variables, erasure bounds and shadows.
    generic_targets = {name: ('Receiver.java', next(i for i, text in enumerate(METADATA_SOURCE.splitlines(), 1)
                                                   if 'int ' + name + '(' in text), name)
                       for name in ('gammaOnly', 'deltaOnly')}
    generic_callers = {}
    for name in ('indices', 'parameters', 'classBounds', 'methodBounds', 'shadow', 'records', 'mixedRecords'):
        line = next(i for i, text in enumerate(METADATA_SOURCE.splitlines(), 1)
                    if 'int ' + name + '(' in text)
        caller = ('Receiver.java', line, name)
        generic_callers[name] = caller
        wanted = [generic_targets['deltaOnly'], ('Receiver.java', line, 'read')]
        if name != 'shadow':
            wanted += [generic_targets['gammaOnly'], ('Receiver.java', line, 'read')]
        seed = 'metadata.GenericProbe.' + name
        for ambiguous in (False, True):
            for limit in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                members = [target for target, _ in call_edges(doc)]
                record(GRAPH, f'generic:{name}:{limit}:{ambiguous}',
                       {'matched': [caller], 'valid': True, 'complete': True, 'page': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'valid': all(target in wanted for target in members),
                        'complete': limit < 100 or sorted(members) == sorted(wanted),
                        'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
        for target in ('deltaOnly',) if name == 'shadow' else ('gammaOnly', 'deltaOnly'):
            path = runner.json('graph', 'path', seed,
                               'metadata.' + ('Gamma' if target == 'gammaOnly' else 'Delta') + '.' + target,
                               '--max-depth', 1)
            record(GRAPH, f'generic:{name}:path:{target}',
                   [(caller, generic_targets[target])],
                   sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']))
    for target in ('gammaOnly', 'deltaOnly'):
        callers = [caller for name, caller in generic_callers.items()
                    if target == 'deltaOnly' or name != 'shadow']
        reverse = runner.json('graph', 'dependents', 'metadata.' + ('Gamma' if target == 'gammaOnly' else 'Delta') + '.' + target,
                              '--limit', 100)
        record(GRAPH, 'generic:reverse:' + target, sorted(callers),
               sorted(identity(row['other']) for row in reverse['items']))
        doc = runner.json('explore', target,
                          '--rwr', '--max-files', 100)
        record(EXPLORE, 'generic:callers:' + target,
               sorted((path, line, 'metadata.' + ('GenericProbe.' if name in generic_callers else 'Probe.') + name)
                      for path, line, name in callers),
               sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))

    # A second applicable local declaration must not lend missing members to
    # the first; a sibling callable must not see either block's local class.
    source.write_text('''package collisions;
class Leaf { int packageOnly() { return 0; } }
class Probe {
 int invalid() { int n = 0; { class Leaf {} Leaf a = null; n += a.onlyOther(); } { class Leaf { int onlyOther() { return 1; } } } return n; }
 int sibling() { Leaf a = null; return a.onlyOther(); }
}
''')
    if not compile_source('negative-javac'):
        raise ToolError('javac accepted an invalid local type member leak')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for name in ('invalid', 'sibling'):
        for ambiguous in (False, True):
            record(GRAPH, name + ':guard:' + str(ambiguous), [],
                   call_edges(runner.json('graph', 'dependencies', 'collisions.Probe.' + name,
                                         *(['--include-ambiguous'] if ambiguous else []))))
    source.write_text(SOURCE)
    for kind, declaration in (
            ('class', 'class Holder<T extends {bound}> {{ T read() {{ return null; }} }}'),
            ('method', 'class Holder {{ <T extends {bound}> T read() {{ return null; }} }}')):
        guard = ('package metadata;\n'
                 'class Gamma {}\n'
                 'class Delta { int delta() { return 1; } }\n'
                 'class GenericProbe {\n'
                 ' int invalid() { { ' + declaration.format(bound='Gamma') +
                 ' Holder a = null; a.read().delta(); } { ' + declaration.format(bound='Delta') +
                 ' } return 0; }\n}\n')
        metadata.write_text(guard)
        if not compile_source('negative-generic-' + kind + '-javac'):
            raise ToolError('javac accepted a bound borrowed from a sibling declaration')
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')
        for ambiguous in (False, True):
            record(GRAPH, f'generic:bound-guard:{kind}:{ambiguous}', [('Receiver.java', 5, 'read')],
                   sorted(target for target, _ in call_edges(runner.json(
                       'graph', 'dependencies', 'metadata.GenericProbe.invalid', '--limit', 100,
                       *(['--include-ambiguous'] if ambiguous else [])))))
    return expected, actual
