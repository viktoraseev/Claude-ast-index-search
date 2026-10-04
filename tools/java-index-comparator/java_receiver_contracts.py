"""Authored Java receiver bindings through production graph consumers.

These checks establish a bounded source contract, not MCP equivalence or
compiler-wide dispatch. Unresolved receivers must not invent resolved edges.
"""
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'graph:java-parameter-bindings', 'graph:java-receiver-guards',
            'explore:java-resolved-receivers'}
REASON = ('independent source/state: disposable Java explicit parameter types, '
          'nonstatic import/package/nested binding, overload arity, unresolved receiver guards '
          'and graph-backed exploration; not MCP equivalence or compiler-wide dispatch')

SOURCES = {
    'a/Leaf.java': '''package fixture.a;
public class Leaf {
    public int ping() { return 1; }
    public int ping(int value) { return value; }
    public static class Inner {
        public int nested() { return 1; }
    }
}
''',
    'b/Leaf.java': '''package fixture.b;
public class Leaf {
    public int ping() { return 2; }
    public int samePackage(Leaf receiver) { return receiver.ping(); }
}
''',
    'local/Probe.java': '''package fixture.local;
import fixture.a.Leaf;
import java.util.List;
class Probe {
    int ping() { return 3; }
    int size() { return 4; }
    int explicit(Leaf receiver) { return receiver.ping(/* no arguments */); }
    int qualified(fixture.b.Leaf receiver) { return receiver.ping(); }
    int argument(Leaf receiver) { return receiver.ping(/* before */ 1 /* after */); }
    int external(List<?> receiver) { return receiver.size(); }
    <Leaf extends java.util.List<?>> int generic(Leaf receiver) { return receiver.size(); }
    int collision(Leaf a, fixture.b.Leaf b) { return a.ping() + b.ping(); }
    int noArity(Leaf receiver) { return receiver.ping(1, 2); }
    int nested(Leaf.Inner receiver) { return receiver.nested(); }
    int prefix(Leaf[] receiver) { return receiver.ping(); }
    int postfix(Leaf receiver[]) { return receiver.ping(); }
}
''',
    'local/List.java': '''package fixture.local;
class List { int size() { return 5; } }
''',
    'wild/Probe.java': '''package fixture.wild;
import fixture.a.*;
class Probe {
    int wildcard(Leaf receiver) { return receiver.ping(); }
}
''',
    'ambiguous/Probe.java': '''package fixture.ambiguous;
import fixture.a.*;
import fixture.b.*;
class Probe {
    int ping() { return 6; }
    int ambiguous(Leaf receiver) { return receiver.ping(); }
}
''',
    'shadow/Probe.java': '''package fixture.shadow;
import fixture.a.*;
class Leaf { int ping() { return 7; } }
class Probe {
    int packageFirst(Leaf receiver) { return receiver.ping(); }
}
''',
    'overload/Leaf.java': '''package fixture.overload;
class Leaf {
    int ping(String value) { return 8; }
    int ping(Integer value) { return 9; }
    int unresolved(Leaf receiver) { return receiver.ping(null); }
}
''',
    'view/View.java': '''package fixture.view;
import fixture.b.Leaf;
import java.util.List;
class View {
    int ping() { return 10; }
    int size() { return 11; }
    int cross(Leaf receiver) { return receiver.ping(); }
    int foreign(List<?> receiver) { return receiver.size(); }
}
''',
}
# Explicit declaration locations, independent of native index rows.
BINDINGS = {
    'fixture.local.Probe.explicit': ('a/Leaf.java', 3, 'ping'),
    'fixture.local.Probe.qualified': ('b/Leaf.java', 3, 'ping'),
    'fixture.local.Probe.argument': ('a/Leaf.java', 4, 'ping'),
    'fixture.local.Probe.nested': ('a/Leaf.java', 6, 'nested'),
    'fixture.b.Leaf.samePackage': ('b/Leaf.java', 3, 'ping'),
    'fixture.wild.Probe.wildcard': ('a/Leaf.java', 3, 'ping'),
    'fixture.shadow.Probe.packageFirst': ('shadow/Probe.java', 3, 'ping'),
}
GUARDS = ('fixture.local.Probe.external', 'fixture.local.Probe.generic',
          'fixture.local.Probe.collision', 'fixture.local.Probe.noArity',
          'fixture.local.Probe.prefix', 'fixture.local.Probe.postfix',
          'fixture.ambiguous.Probe.ambiguous', 'fixture.overload.Leaf.unresolved')
SOURCE_IDS = {
    'fixture.local.Probe.explicit': ('local/Probe.java', 7, 'explicit'),
    'fixture.local.Probe.qualified': ('local/Probe.java', 8, 'qualified'),
    'fixture.local.Probe.argument': ('local/Probe.java', 9, 'argument'),
    'fixture.local.Probe.nested': ('local/Probe.java', 14, 'nested'),
    'fixture.b.Leaf.samePackage': ('b/Leaf.java', 4, 'samePackage'),
    'fixture.wild.Probe.wildcard': ('wild/Probe.java', 4, 'wildcard'),
    'fixture.shadow.Probe.packageFirst': ('shadow/Probe.java', 5, 'packageFirst'),
    'fixture.local.Probe.external': ('local/Probe.java', 10, 'external'),
    'fixture.local.Probe.generic': ('local/Probe.java', 11, 'generic'),
    'fixture.local.Probe.collision': ('local/Probe.java', 12, 'collision'),
    'fixture.local.Probe.noArity': ('local/Probe.java', 13, 'noArity'),
    'fixture.local.Probe.prefix': ('local/Probe.java', 15, 'prefix'),
    'fixture.local.Probe.postfix': ('local/Probe.java', 16, 'postfix'),
    'fixture.ambiguous.Probe.ambiguous': ('ambiguous/Probe.java', 6, 'ambiguous'),
    'fixture.overload.Leaf.unresolved': ('overload/Leaf.java', 5, 'unresolved'),
}
TARGET_QUALIFIERS = {'a/Leaf.java': 'fixture.a.Leaf', 'b/Leaf.java': 'fixture.b.Leaf',
                     'shadow/Probe.java': 'fixture.shadow.Leaf'}


def plan_receivers(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-receiver-bindings'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def identity(row):
    return row.get('path'), row.get('line'), row.get('name')


def call_edges(doc):
    # Type references are separate graph edges. This contract compares every
    # callable target, keeping duplicates and unexpected callable names.
    if doc.get('error') or not isinstance(doc.get('items'), list):
        raise ToolError('receiver query did not execute a graph contract')
    for row in doc['items']:
        other = row.get('other', {})
        if (not isinstance(other.get('path'), str) or not isinstance(other.get('line'), int)
                or not isinstance(other.get('name'), str) or not isinstance(other.get('kind'), str)
                or row.get('confidence') not in {'local', 'scoped', 'import', 'unique', 'ambiguous'}):
            raise ToolError('malformed receiver graph edge; see private logs')
    return sorted((identity(row['other']), row['confidence']) for row in doc['items']
                  if row['other']['kind'] == 'function')


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('receiver artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-receivers-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for relative, source in SOURCES.items():
        path = runner.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, label, want, got):
        expected[feature][label], actual[feature][label] = want, got

    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for seed, target in BINDINGS.items():
        doc = runner.json('graph', 'dependencies', seed)
        record('graph:java-parameter-bindings', seed, [(target, 'scoped')], call_edges(doc))
        # Reverse traversal and paths consume the same resolved edge, rather
        # than re-identifying every same-name method as a caller.
        source_id = SOURCE_IDS[seed]
        qualifier = TARGET_QUALIFIERS[target[0]] + ('.Inner' if target[2] == 'nested' else '')
        reverse = runner.json('graph', 'dependents', qualifier + '.' + target[2])
        record('graph:java-parameter-bindings', seed + ':reverse', True,
               source_id in [identity(row.get('other', {})) for row in reverse.get('items', [])])
        path = runner.json('graph', 'path', seed, target[2], '--max-depth', '1')
        record('graph:java-parameter-bindings', seed + ':path',
               [(source_id, target)],
               [tuple(identity(hop.get('symbol', {})) for hop in hops) for hops in path.get('items', [])])
    for seed in GUARDS:
        doc = runner.json('graph', 'dependencies', seed)
        record('graph:java-receiver-guards', seed,
               {'matched': [SOURCE_IDS[seed]], 'calls': []},
               {'matched': [identity(row) for row in doc['matched']], 'calls': call_edges(doc)})
        doc = runner.json('graph', 'dependencies', seed, '--include-ambiguous')
        wanted = [(('overload/Leaf.java', line, 'ping'), 'ambiguous') for line in (3, 4)] \
            if seed == 'fixture.overload.Leaf.unresolved' else []
        record('graph:java-receiver-guards', seed + ':ambiguous-page',
               {'matched': [SOURCE_IDS[seed]], 'calls': wanted},
               {'matched': [identity(row) for row in doc['matched']], 'calls': call_edges(doc)})

    # A fresh graph with no edge for this Java seed is authoritative about
    # resolved callers. A name fallback here invents callers from other types.
    # Keep lexical seeds within a separate view directory. Fuzzy class seeds
    # can legitimately contribute type-reference neighbours; those are a
    # different contract from false callers of these uncalled methods.
    for seed in ('size', 'ping'):
        doc = runner.json('explore', seed, '--rwr', cwd=runner.root / 'view')
        record('explore:java-resolved-receivers', seed + ':false-callers', [],
               [(row.get('path'), row.get('name'), row.get('line'))
                for row in doc['neighbours'] if row['link'] == 'caller'])
    return expected, actual
