"""Authored Java type bindings through graph consumers, independently of MCP.

A bounded syntax contract: imports, package and nested names, qualified types,
external bindings, generic shadows and line collisions. Visibility, local-class
shadows, static imports and attached roots still need separate contracts.
"""
from pathlib import Path
import tempfile
import re

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'graph:java-type-bindings', 'graph:java-type-binding-guards',
            'explore:java-type-bindings'}
REASON = ('independent source/state: disposable Java syntax type references, explicit/on-demand '
          'imports, package/nested/qualified names, external/generic/collision guards and '
          'graph traversal/exploration; not MCP equivalence or compiler-wide binding')
SOURCES = {
    'a/Leaf.java': '''package fixture.a;
public class Leaf {
    public static class Inner {}
    public int inherited() { return 1; }
}
''',
    'b/Leaf.java': '''package fixture.b;
public class Leaf {}
''',
    'local/Leaf.java': '''package fixture.local;
public class Leaf {}
''',
    'local/List.java': '''package fixture.local;
class List {}
''',
    'local/Probe.java': '''package fixture.local;
import fixture.a.Leaf;
import java.util.List;
class Probe {
    Leaf field;
    Leaf explicit(Leaf input) { return input; }
    fixture.b.Leaf qualified(fixture.b.Leaf input) { return input; }
    Leaf.Inner nested(Leaf.Inner input) { return input; }
    List<?> external(List<?> input) { return input; }
    <Leaf> Leaf generic(Leaf input) { return input; }
    Leaf[] array(Leaf[] input) { return input; }
    List<Leaf> argument(List<Leaf> input) { return input; }
    Object collision(Leaf a, fixture.b.Leaf b) { return null; }
}
''',
    'wild/Probe.java': '''package fixture.wild;
import fixture.a.*;
class Probe {
    Leaf wildcard(Leaf input) { return input; }
}
''',
    'package/Probe.java': '''package fixture.local;
class PackageProbe {
    Leaf packageFirst(Leaf input) { return input; }
}
''',
    'nested/Probe.java': '''package fixture.nested;
import fixture.a.Leaf;
class Probe {
    static class Leaf {}
    Leaf lexical(Leaf input) { return input; }
}
''',
    'ambiguous/Probe.java': '''package fixture.ambiguous;
import fixture.a.*;
import fixture.b.*;
class Probe {
    Leaf ambiguous(Leaf input) { return input; }
}
''',
    'unknown/Probe.java': '''package fixture.unknown;
class Probe {
    Leaf absent(Leaf input) { return input; }
}
''',
    'child/Leaf.java': '''package fixture.child;
class Leaf { int inherited() { return 2; } }
''',
    'child/Child.java': '''package fixture.child;
import fixture.a.Leaf;
class Child extends Leaf {
    int importedParent() { return inherited(); }
}
''',
    'child/Qualified.java': '''package fixture.child;
class Qualified extends fixture.a.Leaf {
    int qualifiedParent() { return inherited(); }
}
''',
    'view/View.java': '''package fixture.view;
import fixture.a.Leaf;
class View {
    Leaf viewed(Leaf input) { return input; }
}
''',
}
A = ('a/Leaf.java', 2, 'Leaf')
B = ('b/Leaf.java', 2, 'Leaf')
BINDINGS = {
    'fixture.local.Probe.field': [A],
    'fixture.local.Probe.explicit': [A],
    'fixture.local.Probe.qualified': [B],
    'fixture.local.Probe.nested': [A, ('a/Leaf.java', 3, 'Inner')],
    'fixture.local.Probe.array': [A],
    'fixture.local.Probe.argument': [A],
    'fixture.wild.Probe.wildcard': [A],
    'fixture.local.PackageProbe.packageFirst': [('local/Leaf.java', 2, 'Leaf')],
    'fixture.nested.Probe.lexical': [('nested/Probe.java', 4, 'Leaf')],
    'fixture.view.View.viewed': [A],
}
GUARDS = {
    'fixture.local.Probe.external': ('local/Probe.java', 9, 'external'),
    'fixture.local.Probe.generic': ('local/Probe.java', 10, 'generic'),
    'fixture.local.Probe.collision': ('local/Probe.java', 13, 'collision'),
    'fixture.unknown.Probe.absent': ('unknown/Probe.java', 3, 'absent'),
    'fixture.ambiguous.Probe.ambiguous': ('ambiguous/Probe.java', 5, 'ambiguous'),
}


def plan_types(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-type-bindings'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def identity(row):
    return row.get('path'), row.get('line'), row.get('name')


def edges(document):
    if document.get('error') or not isinstance(document.get('items'), list):
        raise ToolError('type binding query did not execute a graph contract')
    result = []
    for row in document['items']:
        other = row.get('other', {})
        if (not isinstance(other.get('path'), str) or type(other.get('line')) is not int
                or not isinstance(other.get('name'), str) or not isinstance(other.get('kind'), str)
                or row.get('confidence') not in {'local', 'scoped', 'import', 'unique', 'ambiguous'}):
            raise ToolError('malformed type binding graph edge; see private logs')
        # Preserve every output edge, including unexpected callable edges and duplicates.
        result.append((identity(other), row['confidence']))
    return sorted(result)


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('type binding fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-types-', dir=base)).resolve()
    runner = Runner(binary, directory)
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
        for cap in (0, 1, 100):
            doc = runner.json('graph', 'dependencies', seed, '--limit', cap)
            record('graph:java-type-bindings', f'{seed}:dependencies:{cap}',
                   {'total': len(targets), 'edges': sorted((target, 'scoped') for target in targets)[:cap]},
                   {'total': doc.get('pagination', {}).get('total'), 'edges': edges(doc)})
        # Full-name seeds isolate the declaration under test; no broad-query
        # normalization or DB agreement is used as expected binding truth.
        source = identity(doc['matched'][0]) if len(doc.get('matched', [])) == 1 else None
        # This observed identity is used only for membership checks; the
        # full seed selection is separately checked against authored syntax.
        name = seed.rsplit('.', 1)[-1]
        candidates = [(path, i, name) for path, text in SOURCES.items()
                      for i, line in enumerate(text.splitlines(), 1)
                      if re.search(r'\b' + re.escape(name) + r'\s*[;(]', line) and not line.startswith('package ')]
        if len(candidates) != 1:
            raise ToolError('authored type fixture seed is not unique')
        record('graph:java-type-bindings', seed + ':selection', candidates, [identity(r) for r in doc['matched']])
        for target in targets:
            qualifier = ('fixture.a.Leaf.Inner' if target[2] == 'Inner' else
                         {'a/Leaf.java': 'fixture.a.Leaf', 'b/Leaf.java': 'fixture.b.Leaf',
                          'local/Leaf.java': 'fixture.local.Leaf',
                          'nested/Probe.java': 'fixture.nested.Probe.Leaf'}[target[0]])
            reverse = runner.json('graph', 'dependents', qualifier)
            record('graph:java-type-bindings', seed + ':reverse:' + qualifier, True,
                   source in [identity(row['other']) for row in reverse['items']])
            impact = runner.json('graph', 'impact', qualifier, '--depth', '1')
            record('graph:java-type-bindings', seed + ':impact:' + qualifier, True,
                   source in [identity(row['symbol']) for row in impact['items']])
            path = runner.json('graph', 'path', seed, qualifier, '--max-depth', '1')
            record('graph:java-type-bindings', seed + ':path:' + qualifier,
                   [(candidates[0], end) for end in targets
                    if end == target or (target == A and end[2] == 'Inner')],
                   [tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']])
    for seed, source in (('fixture.child.Child.importedParent', ('child/Child.java', 4, 'importedParent')),
                         ('fixture.child.Qualified.qualifiedParent', ('child/Qualified.java', 3, 'qualifiedParent'))):
        owner = seed.rsplit('.', 1)[0]
        doc = runner.json('graph', 'dependencies', owner)
        record('graph:java-type-bindings', seed + ':parent-type', [(A, 'scoped')], edges(doc))
        doc = runner.json('graph', 'dependencies', seed)
        record('graph:java-type-bindings', seed + ':inherited',
               [(('a/Leaf.java', 4, 'inherited'), 'scoped')], edges(doc))
        doc = runner.json('graph', 'path', seed, 'fixture.a.Leaf.inherited', '--max-depth', '1')
        record('graph:java-type-bindings', seed + ':inherited-path',
               [(source, ('a/Leaf.java', 4, 'inherited'))],
               [tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']])
    for seed, source in GUARDS.items():
        for ambiguous in (False, True):
            doc = runner.json('graph', 'dependencies', seed, *(['--include-ambiguous'] if ambiguous else []))
            wanted = sorted([(A, 'ambiguous'), (B, 'ambiguous')]) \
                if ambiguous and seed.endswith('.ambiguous') else []
            record('graph:java-type-binding-guards', f'{seed}:{ambiguous}',
                   {'matched': [source], 'edges': wanted},
                   {'matched': [identity(row) for row in doc['matched']], 'edges': edges(doc)})
    # Scope containing both a type and its guarded/valid clients isolates
    # exploration from unrelated fuzzy or whole-project lexical candidates.
    # A fresh graph must not revive a name-based neighbour for an external import.
    (runner.root / 'view/List.java').write_text('package fixture.view; class List {}\n')
    (runner.root / 'view/Client.java').write_text('''package fixture.view;
import java.util.List;
class Client {
    List<?> guarded(List<?> input) { return input; }
}
''')
    runner.command('update')
    runner.json('graph', 'build')
    doc = runner.json('explore', 'List', '--rwr', cwd=runner.root / 'view')
    record('explore:java-type-bindings', 'external-import-seed', True,
           any(r['path'] == 'view/List.java' and r['line'] == 1 and r['kind'] == 'class'
               for r in doc['symbols']))
    record('explore:java-type-bindings', 'external-import-no-neighbour', [],
           [(r['path'], r['line'], r['name']) for r in doc['neighbours'] if r['link'] == 'caller'])
    # Add a legal local binding, then check that the same graph consumer does
    # surface its independently authored neighbour after update/refresh.
    (runner.root / 'view/Client.java').write_text('''package fixture.view;
class Client {
    List bound(List input) { return input; }
}
''')
    runner.command('update')
    runner.json('graph', 'build')
    doc = runner.json('explore', 'List', '--rwr', cwd=runner.root / 'view')
    record('explore:java-type-bindings', 'package-binding-neighbour',
           [('view/Client.java', 3, 'fixture.view.Client.bound')],
           sorted((r['path'], r['line'], r['name']) for r in doc['neighbours'] if r['link'] == 'caller'))
    return expected, actual
