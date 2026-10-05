"""Order-independent inherited Java parent aliases; independent source/state truth."""
from pathlib import Path
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_type_binding_contracts import edges, identity
from java_type_access_contracts import page_observation

FEATURES = {'graph:java-inherited-parent-bindings', 'explore:java-inherited-parent-bindings'}
REASON = ('independent source/state: disposable javac-validated Java inherited parent aliases, '
          'hidden enclosing owners, qualified/static/lexical bindings, precedence and access guards, '
          'opposite file orders, graph pages/reverse edges and explore callers; not MCP equivalence '
          'or compiler-wide dispatch')
SOURCES = {
    'base/Nest.java': '''package fixture.base;
class Nest {
    static class HiddenOwner {
        public static class Member {
            public static class Deep {}
            public int marker() { return 1; }
        }
        private static class Hidden {}
    }
}
''',
    'base/Exported.java': '''package fixture.base;
public class Exported extends Nest.HiddenOwner {}
''',
    'bridge/Bridge.java': '''package fixture.bridge;
public class Bridge extends fixture.base.Exported {}
''',
    'client/Child.java': '''package fixture.client;
import static fixture.bridge.Bridge.Member;
class Child extends Member {
    Deep use(Deep value) { return value; }
    int call() { return marker(); }
}
''',
    'client/Qualified.java': '''package fixture.client;
class Qualified extends fixture.bridge.Bridge.Member {
    int qualifiedCall() { return marker(); }
}
''',
    'nested/Outer.java': '''package fixture.nested;
class Outer extends fixture.bridge.Bridge {
    class Local extends Member {
        int nestedCall() { return marker(); }
    }
}
''',
    'shadow/Outer.java': '''package fixture.shadow;
class Outer extends fixture.bridge.Bridge {
    class Local extends Member {
        int shadowCall() { return marker(); }
    }
}
''',
    'shadow/Member.java': '''package fixture.shadow;
class Member { int wrong() { return 0; } }
''',
    'chain/Mid.java': '''package fixture.chain;
public class Mid extends fixture.bridge.Bridge.Member {}
''',
    'chain/Leaf.java': '''package fixture.chain;
class Leaf extends Mid.Deep {}
''',
    'guard/Bad.java': '''package fixture.guard;
import fixture.bridge.Bridge.Member;
class Bad extends Member { int badCall() { return marker(); } }
''',
    'privateguard/Bad.java': '''package fixture.privateguard;
import static fixture.bridge.Bridge.Hidden;
class Bad extends Hidden {}
''',
    'directguard/Bad.java': '''package fixture.directguard;
class Bad extends fixture.base.Nest.HiddenOwner.Member {}
''',
}
NEGATIVE_DIRS = ('guard/', 'privateguard/', 'directguard/')
# Authored declaration coordinates, never learned from native DB output.
MEMBER = ('base/Nest.java', 4, 'Member')
DEEP = ('base/Nest.java', 5, 'Deep')
MARKER = ('base/Nest.java', 6, 'marker')
BRIDGE = ('bridge/Bridge.java', 2, 'Bridge')
MID = ('chain/Mid.java', 2, 'Mid')
PARENTS = {
    'fixture.client.Child': [MEMBER],
    'fixture.client.Qualified': [BRIDGE, MEMBER],
    'fixture.nested.Outer.Local': [MEMBER],
    'fixture.shadow.Outer.Local': [MEMBER],
    'fixture.chain.Mid': [BRIDGE, MEMBER],
    'fixture.chain.Leaf': [MID, DEEP],
}
CALLS = {f'fixture.{name}': MARKER for name in (
    'client.Child.call', 'client.Qualified.qualifiedCall',
    'nested.Outer.Local.nestedCall', 'shadow.Outer.Local.shadowCall')}
GUARDS = ('fixture.guard.Bad', 'fixture.guard.Bad.badCall',
          'fixture.privateguard.Bad', 'fixture.directguard.Bad')

SEEDS = {
    'fixture.client.Child': ('client/Child.java', 3, 'Child'),
    'fixture.client.Qualified': ('client/Qualified.java', 2, 'Qualified'),
    'fixture.nested.Outer.Local': ('nested/Outer.java', 3, 'Local'),
    'fixture.shadow.Outer.Local': ('shadow/Outer.java', 3, 'Local'),
    'fixture.chain.Mid': ('chain/Mid.java', 2, 'Mid'),
    'fixture.chain.Leaf': ('chain/Leaf.java', 2, 'Leaf'),
    'fixture.client.Child.call': ('client/Child.java', 5, 'call'),
    'fixture.client.Qualified.qualifiedCall': ('client/Qualified.java', 3, 'qualifiedCall'),
    'fixture.nested.Outer.Local.nestedCall': ('nested/Outer.java', 4, 'nestedCall'),
    'fixture.shadow.Outer.Local.shadowCall': ('shadow/Outer.java', 4, 'shadowCall'),
    'fixture.client.Child.use': ('client/Child.java', 4, 'use'),
    'fixture.guard.Bad': ('guard/Bad.java', 3, 'Bad'),
    'fixture.guard.Bad.badCall': ('guard/Bad.java', 3, 'badCall'),
    'fixture.privateguard.Bad': ('privateguard/Bad.java', 3, 'Bad'),
    'fixture.directguard.Bad': ('directguard/Bad.java', 2, 'Bad'),
}


def plan_parents(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-inherited-parent-bindings'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def parent_page(targets, observed, limit, total, paths):
    """Check pagination in physical path order before removing layout prefixes."""
    physical = [(paths[p], line, name) for p, line, name in targets]
    result = page_observation(physical, observed, limit, total)
    if result['edges'] is not None:
        result['edges'] = [((p.split('/', 1)[1], line, name), confidence)
                           for (p, line, name), confidence in result['edges']]
    return result


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('inherited parent fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))
    graph, explore = 'graph:java-inherited-parent-bindings', 'explore:java-inherited-parent-bindings'

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for layout in ('forward', 'reverse'):
        runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-parents-', dir=base)).resolve())
        runner.root.mkdir()
        (runner.root / '.git').mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        paths = {key: f'{index:02d}/' + key for index, key in enumerate(
            sorted(SOURCES, reverse=layout == 'reverse'))}
        for key, source in SOURCES.items():
            path = runner.root / paths[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
        (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
        state = connect(runner.directory / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, runner.root)
            observed = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            want = {'.java': len(SOURCES), '.kt': 1, '.xml': 1}
            for feature in FEATURES:
                record(feature, layout + ':inventory', want, observed)
            if want != observed:
                raise ToolError('inherited parent fixture inventory incomplete')
        finally:
            state.close()
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')

        def normalize(row):
            path, line, name = identity(row)
            if path not in paths.values():
                raise ToolError('inherited parent result escaped authored Java source scope')
            return path.split('/', 1)[1], line, name

        bindings = {**PARENTS, **{k: [v] for k, v in CALLS.items()},
                    'fixture.client.Child.use': [DEEP], **{k: [] for k in GUARDS}}
        for seed, targets in bindings.items():
            for limit in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit, '--include-ambiguous')
                # Validate the production graph document before adapting paths.
                observed = edges(doc)
                for p, line, name in (target for target, _ in observed):
                    normalize({'path': p, 'line': line, 'name': name})
                record(graph, f'{layout}:{seed}:{limit}',
                       {'total': len(targets), 'returned': min(limit, len(targets)), 'valid': True,
                        'edges': [(t, 'scoped') for t in sorted(targets, key=lambda t: (paths[t[0]], t[1]))]
                                 if limit >= len(targets) else None},
                       parent_page(targets, observed, limit, doc.get('pagination', {}).get('total'), paths))
                matches = [normalize(row) for row in doc.get('matched', [])]
                # All seeds identify one authored declaration; missing seeds cannot pass empty guards.
                record(graph, f'{layout}:{seed}:{limit}:selection',
                       [SEEDS[seed]], matches)
        for qualified, target in (('fixture.base.Nest.HiddenOwner.Member', MEMBER),
                                  ('fixture.base.Nest.HiddenOwner.Member.Deep', DEEP),
                                  ('fixture.base.Nest.HiddenOwner.Member.marker', MARKER)):
            doc = runner.json('graph', 'dependents', qualified, '--limit', 100)
            edges(doc)
            required = {seed for seed, targets in bindings.items() if target in targets}
            observed = [normalize(row['other']) for row in doc['items']]
            wanted = sorted(SEEDS[seed] for seed in required)
            record(graph, layout + ':reverse:' + qualified, wanted,
                   sorted(observed))
        doc = runner.json('explore', 'marker', '--rwr', '--max-files', 100)
        callers = [row['name'] for row in doc['neighbours'] if row['link'] == 'caller']
        record(explore, layout + ':callers', {'required': sorted(CALLS), 'guards': [], 'duplicates': False},
               {'required': sorted(name for name in callers if name in CALLS),
                'guards': sorted(name for name in callers if name in GUARDS),
                'duplicates': len(callers) != len(set(callers))})
    return expected, actual
