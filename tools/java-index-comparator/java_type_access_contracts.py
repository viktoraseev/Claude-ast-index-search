"""Java type accessibility and static imports; independent authored source truth.

This bounded contract does not establish inherited member type lookup, local
class binding, module exports or compiler-wide dispatch, nor MCP equivalence.
"""
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from java_type_binding_contracts import edges, identity

FEATURES = {'graph:java-type-access', 'graph:java-static-type-imports',
            'explore:java-type-access'}
REASON = ('independent source/state: disposable Java public/package/private/protected type '
          'accessibility, enclosing type access and direct static nested type imports, '
          'graph traversal and exploration; not MCP equivalence or compiler-wide binding')
SOURCES = {
    'a/Box.java': '''package fixture.a;
public class Box {
    public static class Open { public static class Deep {} }
    static class PackageOnly {}
    private static class Hidden {}
    protected static class Protected {}
    public class Instance {}
    Hidden nestmate(Hidden input) { return input; }
    static class Peer {
        Hidden peer(Hidden input) { return input; }
    }
}
class Secret {}
class Closed { public static class Visible {} }
''',
    'a/Client.java': '''package fixture.a;
class Client {
    Box.PackageOnly packageMember(Box.PackageOnly input) { return input; }
    Secret packageType(Secret input) { return input; }
    Box.Protected samePackage(Box.Protected input) { return input; }
}
''',
    'b/Secret.java': '''package fixture.b;
public class Secret {}
''',
    'client/Client.java': '''package fixture.client;
import fixture.a.*;
import fixture.b.*;
class Client {
    Secret wildcard(Secret input) { return input; }
    fixture.a.Box.Open qualified(fixture.a.Box.Open input) { return input; }
}
''',
    'explicit/Client.java': '''package fixture.explicit;
import static fixture.a.Box.Open;
class Client {
    Open explicit(Open input) { return input; }
}
''',
    'wild/Client.java': '''package fixture.wild;
import static fixture.a.Box.*;
class Client {
    Open onDemand(Open input) { return input; }
}
''',
    'tail/Client.java': '''package fixture.tail;
import static fixture.a.Box.Open;
class Client {
    Open.Deep importedTail(Open.Deep input) { return input; }
}
''',
    'implicit/Api.java': '''package fixture.implicit;
public interface Api { class Member {} }
''',
    'implicit/Client.java': '''package fixture.implicit;
import static fixture.implicit.Api.Member;
class Client {
    Member implicit(Member input) { return input; }
}
''',
    'enums/Holder.java': '''package fixture.enums;
public enum Holder {
    ONLY;
    public static class Member {}
}
''',
    'enums/Client.java': '''package fixture.enums;
import static fixture.enums.Holder.Member;
class Client {
    Member fromEnum(Member input) { return input; }
}
''',
    'subclass/Child.java': '''package fixture.subclass;
public class Child extends fixture.a.Box {
    fixture.a.Box.Protected inheritedAccess(fixture.a.Box.Protected input) { return input; }
    static class Nested {
        fixture.a.Box.Protected enclosingSubclass(fixture.a.Box.Protected input) { return input; }
    }
}
''',
    'guard/Client.java': '''package fixture.guard;
class Client {
    fixture.a.Secret deniedPackage(fixture.a.Secret input) { return input; }
    fixture.a.Box.Hidden deniedPrivate(fixture.a.Box.Hidden input) { return input; }
    fixture.a.Box.PackageOnly deniedMember(fixture.a.Box.PackageOnly input) { return input; }
    fixture.a.Closed.Visible deniedEnclosing(fixture.a.Closed.Visible input) { return input; }
    fixture.a.Box.Protected deniedProtected(fixture.a.Box.Protected input) { return input; }
}
''',
    'nonstatic/Client.java': '''package fixture.nonstatic;
import static fixture.a.Box.Instance;
class Client {
    Instance invalid(Instance input) { return input; }
}
''',
    'staticguard/Client.java': '''package fixture.staticguard;
import static fixture.a.Box.Hidden;
class Client {
    Hidden inaccessibleImport(Hidden input) { return input; }
}
''',
    'staticguard/Hidden.java': 'package fixture.staticguard; class Hidden {}\n',
    'protectedimport/Child.java': '''package fixture.protectedimport;
import static fixture.a.Box.Protected;
public class Child extends fixture.a.Box {
    Protected deniedImport(Protected input) { return input; }
}
''',
    'external/Client.java': '''package fixture.external;
import static java.util.Map.Entry;
class Client {
    Entry external(Entry input) { return input; }
}
''',
    'external/Entry.java': 'package fixture.external; class Entry {}\n',
    'view/Box.java': '''package fixture.view;
public class Box {
    private static class Hidden {}
}
''',
    'view/Client.java': '''package fixture.view;
class Client {
    Box.Hidden guarded(Box.Hidden input) { return input; }
}
''',
}
# Complete expected edges, including the separately referenced enclosing type.
BOX = ('a/Box.java', 2, 'Box')
BINDINGS = {
    'fixture.a.Box.nestmate': [('a/Box.java', 5, 'Hidden')],
    'fixture.a.Box.Peer.peer': [('a/Box.java', 5, 'Hidden')],
    'fixture.a.Client.packageMember': [BOX, ('a/Box.java', 4, 'PackageOnly')],
    'fixture.a.Client.packageType': [('a/Box.java', 13, 'Secret')],
    'fixture.a.Client.samePackage': [BOX, ('a/Box.java', 6, 'Protected')],
    'fixture.client.Client.wildcard': [('b/Secret.java', 2, 'Secret')],
    'fixture.client.Client.qualified': [BOX, ('a/Box.java', 3, 'Open')],
    'fixture.explicit.Client.explicit': [('a/Box.java', 3, 'Open')],
    'fixture.wild.Client.onDemand': [('a/Box.java', 3, 'Open')],
    'fixture.tail.Client.importedTail': [('a/Box.java', 3, 'Open'), ('a/Box.java', 3, 'Deep')],
    'fixture.implicit.Client.implicit': [('implicit/Api.java', 2, 'Member')],
    'fixture.enums.Client.fromEnum': [('enums/Holder.java', 4, 'Member')],
    'fixture.subclass.Child.inheritedAccess': [BOX, ('a/Box.java', 6, 'Protected')],
    'fixture.subclass.Child.Nested.enclosingSubclass': [BOX, ('a/Box.java', 6, 'Protected')],
    'fixture.guard.Client.deniedPackage': [],
    'fixture.guard.Client.deniedPrivate': [BOX],
    'fixture.guard.Client.deniedMember': [BOX],
    'fixture.guard.Client.deniedEnclosing': [],
    'fixture.guard.Client.deniedProtected': [BOX],
    'fixture.nonstatic.Client.invalid': [],
    'fixture.staticguard.Client.inaccessibleImport': [],
    'fixture.protectedimport.Child.deniedImport': [],
    'fixture.external.Client.external': [],
}
NEGATIVE_DIRS = ('guard/', 'nonstatic/', 'staticguard/', 'protectedimport/', 'view/')


def plan_access(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-type-access'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def page_observation(targets, observed, cap, total):
    # Graph pages order by confidence/path/line. Names on the same line are
    # tied; sorting by name before slicing invents an ordering the CLI lacks.
    ordered = sorted(targets, key=lambda target: target[:2])
    cutoff = ordered[min(cap, len(ordered)) - 1][:2] if cap and ordered else None
    required = {target for target in targets if cutoff is not None and target[:2] < cutoff}
    allowed = {target for target in targets if cutoff is not None and target[:2] <= cutoff}
    selected = [target for target, confidence in observed if confidence == 'scoped']
    return {'total': total, 'returned': len(observed),
            'valid': len(selected) == len(observed) == len(set(selected))
                     and required <= set(selected) <= allowed,
            'edges': observed if cap >= len(targets) else None}


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('type access fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-type-access-', dir=base)).resolve())
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for relative, source in SOURCES.items():
        path = runner.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for seed, targets in BINDINGS.items():
        feature = 'graph:java-static-type-imports' if seed.split('.')[1] in {
            'explicit', 'wild', 'tail', 'implicit', 'enums', 'nonstatic', 'staticguard', 'protectedimport', 'external'} else 'graph:java-type-access'
        for cap in (0, 1, 100):
            doc = runner.json('graph', 'dependencies', seed, '--limit', cap, '--include-ambiguous')
            record(feature, f'{seed}:{cap}',
                   {'total': len(targets), 'returned': min(cap, len(targets)), 'valid': True,
                    'edges': sorted((t, 'scoped') for t in targets) if cap >= len(targets) else None},
                   page_observation(targets, edges(doc), cap, doc.get('pagination', {}).get('total')))
        # Use an authored seed identity for reverse traversal, never a native DB expectation.
        name = seed.rsplit('.', 1)[-1]
        source = [(p, i, name) for p, text in SOURCES.items()
                  for i, line in enumerate(text.splitlines(), 1) if f' {name}(' in line]
        if len(source) != 1:
            raise ToolError('type access seed must have one authored declaration')
        record(feature, seed + ':selection', source, [identity(r) for r in doc['matched']])
        for target in targets:
            qualifier = ('fixture.a.Box.Open.Deep' if target[2] == 'Deep' else
                         'fixture.a.Box' + ('.' + target[2] if target != BOX else '')
                         if target[0] == 'a/Box.java' and target[2] != 'Secret' else
                         'fixture.a.Secret' if target[2] == 'Secret' and target[0] == 'a/Box.java' else
                         'fixture.b.Secret' if target[0] == 'b/Secret.java' else
                         'fixture.enums.Holder.Member' if target[0] == 'enums/Holder.java' else 'fixture.implicit.Api.Member')
            reverse = runner.json('graph', 'dependents', qualifier)
            record(feature, seed + ':reverse:' + qualifier, True,
                   source[0] in [identity(row['other']) for row in reverse['items']])
    doc = runner.json('explore', 'Hidden', '--rwr', cwd=runner.root / 'view')
    record('explore:java-type-access', 'seed', [('view/Box.java', 3, 'fixture.view.Box.Hidden')],
           [identity(r) for r in doc['symbols'] if r['name'].endswith('.Hidden') or r['name'] == 'Hidden'])
    record('explore:java-type-access', 'inaccessible-neighbours', [],
           [(r['path'], r['line'], r['name']) for r in doc['neighbours'] if r['link'] == 'caller'])
    return expected, actual
