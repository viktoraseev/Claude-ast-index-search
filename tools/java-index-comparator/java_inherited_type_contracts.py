"""Inherited Java member types on authored sources, not MCP equivalence.

Public declaring/qualifying owners, inheritance paths, hiding, diamond identity,
and import access guards are bounded contracts, not compiler-wide resolution.
"""
from pathlib import Path
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_type_binding_contracts import edges, identity
from java_type_access_contracts import page_observation

FEATURES = {'graph:java-inherited-types', 'graph:java-inherited-static-type-imports',
            'explore:java-inherited-types'}
REASON = ('independent source/state: disposable Java inherited member types, qualified/lexical '
          'names, explicit/on-demand static imports, hiding, diamond identity and access guards; '
          'graph pages/reverse traversal and exploration; not MCP equivalence or compiler-wide binding')
SOURCES = {
    'base/Base.java': '''package fixture.base;
public class Base {
    public static class Member { public static class Deep {} }
    private static class Hidden {}
    protected static class Protected {}
    static class PackageOnly {}
    public class Instance {}
    public static class MAX_VALUE {}
}
''',
    'child/Child.java': '''package fixture.child;
public class Child extends fixture.base.Base {
    Member lexical(Member input) { return input; }
    Protected protectedLexical(Protected input) { return input; }
}
''',
    'child/Grand.java': '''package fixture.child;
public class Grand extends Child {}
''',
    'hiding/Hiding.java': '''package fixture.hiding;
public class Hiding extends fixture.child.Grand {
    public static class Member {}
    Member hiddenParent(Member input) { return input; }
}
''',
    'diamond/Left.java': '''package fixture.diamond;
public interface Left { class Shared {} }
''',
    'diamond/Right.java': '''package fixture.diamond;
public interface Right extends Left {}
''',
    'diamond/Both.java': '''package fixture.diamond;
public class Both implements Left, Right {
    Shared diamond(Shared input) { return input; }
}
''',
    'client/Client.java': '''package fixture.client;
import static fixture.child.Grand.Member;
class Client {
    Member explicit(Member input) { return input; }
    Member.Deep tail(Member.Deep input) { return input; }
    fixture.child.Child.Member qualified(fixture.child.Child.Member input) { return input; }
    fixture.hiding.Hiding.Member hiding(fixture.hiding.Hiding.Member input) { return input; }
}
''',
    'wild/Client.java': '''package fixture.wild;
import static fixture.child.Grand.*;
class Client {
    Member wildcard(Member input) { return input; }
}
''',
    'namespace/Client.java': '''package fixture.namespace;
import static java.lang.Integer.MAX_VALUE;
class Client extends fixture.child.Grand {
    MAX_VALUE fieldNamespace(MAX_VALUE input) { return input; }
}
''',
    'guard/Client.java': '''package fixture.guard;
import static fixture.child.Grand.Hidden;
class Hidden {}
class Client {
    Hidden privateImport(Hidden input) { return input; }
    fixture.child.Grand.PackageOnly packageGuard(fixture.child.Grand.PackageOnly input) { return input; }
    fixture.child.Grand.Protected protectedGuard(fixture.child.Grand.Protected input) { return input; }
}
''',
    'nonstatic/Client.java': '''package fixture.nonstatic;
import static fixture.child.Grand.Instance;
class Instance {}
class Client { Instance invalid(Instance input) { return input; } }
''',
    'ambiguous/Other.java': '''package fixture.ambiguous;
public interface Other { class Shared {} }
''',
    'ambiguous/Mixed.java': '''package fixture.ambiguous;
class Mixed implements fixture.diamond.Left, Other {
    Shared ambiguous(Shared input) { return input; }
}
''',
}
NEGATIVE_DIRS = ('guard/', 'nonstatic/', 'ambiguous/')
MEMBER = ('base/Base.java', 3, 'Member')
BINDINGS = {
    'fixture.child.Child.lexical': [MEMBER],
    'fixture.child.Child.protectedLexical': [('base/Base.java', 5, 'Protected')],
    'fixture.hiding.Hiding.hiddenParent': [('hiding/Hiding.java', 3, 'Member')],
    'fixture.diamond.Both.diamond': [('diamond/Left.java', 2, 'Shared')],
    'fixture.client.Client.explicit': [MEMBER],
    'fixture.client.Client.tail': [MEMBER, ('base/Base.java', 3, 'Deep')],
    'fixture.client.Client.qualified': [('child/Child.java', 2, 'Child'), MEMBER],
    'fixture.client.Client.hiding': [('hiding/Hiding.java', 2, 'Hiding'), ('hiding/Hiding.java', 3, 'Member')],
    'fixture.wild.Client.wildcard': [MEMBER],
    'fixture.namespace.Client.fieldNamespace': [('base/Base.java', 8, 'MAX_VALUE')],
    'fixture.guard.Client.privateImport': [],
    'fixture.guard.Client.packageGuard': [('child/Grand.java', 2, 'Grand')],
    'fixture.guard.Client.protectedGuard': [('child/Grand.java', 2, 'Grand')],
    'fixture.nonstatic.Client.invalid': [],
}
QUALIFIERS = {MEMBER: 'fixture.base.Base.Member',
              ('base/Base.java', 3, 'Deep'): 'fixture.base.Base.Member.Deep',
              ('base/Base.java', 5, 'Protected'): 'fixture.base.Base.Protected',
              ('hiding/Hiding.java', 3, 'Member'): 'fixture.hiding.Hiding.Member',
              ('diamond/Left.java', 2, 'Shared'): 'fixture.diamond.Left.Shared',
              ('child/Child.java', 2, 'Child'): 'fixture.child.Child',
              ('child/Grand.java', 2, 'Grand'): 'fixture.child.Grand',
              ('hiding/Hiding.java', 2, 'Hiding'): 'fixture.hiding.Hiding'}
QUALIFIERS[('base/Base.java', 8, 'MAX_VALUE')] = 'fixture.base.Base.MAX_VALUE'


def plan_types(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-inherited-types'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('inherited type fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-inherited-types-', dir=base)).resolve())
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for relative, source in SOURCES.items():
        path = runner.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        for feature in FEATURES:
            record(feature, 'inventory', {'.java': len(SOURCES), '.kt': 1}, inventory)
        if inventory != {'.java': len(SOURCES), '.kt': 1}:
            raise ToolError('inherited type fixture inventory incomplete')
    finally:
        state.close()
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for seed, targets in BINDINGS.items():
        feature = ('graph:java-inherited-static-type-imports' if seed.split('.')[1] in
                   {'client', 'wild', 'namespace', 'guard', 'nonstatic'} else 'graph:java-inherited-types')
        for cap in (0, 1, 100):
            doc = runner.json('graph', 'dependencies', seed, '--limit', cap, '--include-ambiguous')
            record(feature, f'{seed}:{cap}',
                   {'total': len(targets), 'returned': min(cap, len(targets)), 'valid': True,
                    'edges': sorted((t, 'scoped') for t in targets) if cap >= len(targets) else None},
                   page_observation(targets, edges(doc), cap, doc.get('pagination', {}).get('total')))
        name = seed.rsplit('.', 1)[-1]
        source = [(p, i, name) for p, text in SOURCES.items()
                  for i, line in enumerate(text.splitlines(), 1) if f' {name}(' in line]
        if len(source) != 1:
            raise ToolError('inherited type seed must have one authored declaration')
        record(feature, seed + ':selection', source, [identity(r) for r in doc['matched']])
        for target in targets:
            reverse = runner.json('graph', 'dependents', QUALIFIERS[target])
            record(feature, seed + ':reverse:' + QUALIFIERS[target], True,
                   source[0] in [identity(row['other']) for row in reverse['items']])
    ambiguous = runner.json('graph', 'dependencies', 'fixture.ambiguous.Mixed.ambiguous',
                            '--include-ambiguous', '--limit', 100)
    record('graph:java-inherited-types', 'distinct-inherited-ambiguity',
           [(('ambiguous/Other.java', 2, 'Shared'), 'ambiguous'),
            (('diamond/Left.java', 2, 'Shared'), 'ambiguous')], edges(ambiguous))
    # RWR also expands enclosing types and lexical seeds. Check this inherited
    # caller and invalid-source guards; do not claim an exhaustive ranking oracle.
    doc = runner.json('explore', 'Deep', '--rwr', '--max-files', 100)
    callers = [(r['path'], r['line'], r['name']) for r in doc['neighbours'] if r['link'] == 'caller']
    wanted = [(p, i, seed) for seed, targets in BINDINGS.items() if ('base/Base.java', 3, 'Deep') in targets
              for p, text in SOURCES.items() for i, line in enumerate(text.splitlines(), 1)
              if f" {seed.rsplit('.', 1)[-1]}(" in line]
    guards = {seed for seed in BINDINGS if seed.startswith(('fixture.guard.', 'fixture.nonstatic.'))}
    record('explore:java-inherited-types', 'callers',
           {'required': sorted(wanted), 'guards': [], 'duplicates': False},
           {'required': sorted(row for row in callers if row in wanted),
            'guards': sorted(row for row in callers if row[2] in guards),
            'duplicates': len(callers) != len(set(callers))})
    return expected, actual
