"""Java graph selection and local traversal on authored source, not MCP truth.

The expected edges below are explicit invocations in tiny synthetic sources.
Native DB rows never supply expected identities. Cross-file Java dispatch and
attached-root traversal remain separate contracts.
"""
from collections import deque
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
import graph_metrics_contracts
import graph_traversal_contracts

FEATURES = ({'graph:java-selection', 'graph:java-traversal', 'graph:lifecycle'} |
            graph_metrics_contracts.FEATURES | graph_traversal_contracts.FEATURES)
REASON = ('independent source/state: disposable Java qualified graph seeds, local invocation '
          'edges, traversal caps, graph freshness, rational stationary metrics, ranked '
          'pages, shortest-path pagination and traversal/metrics/top text rendering; not MCP equivalence')
PENDING_REASON = ('Java compiler-wide type accessibility, same-line local-type sites, declaration-site local-class receiver binding, local/field/generic receiver and overload type resolution, attached-root traversal, '
                  'ambiguity budgets, ambiguous/attached-root rendering and overlapping path seeds remain unresolved; '
                  'separate source contracts cover syntax type binding/guards, public/package/private/protected and enclosing type access, direct and inherited static type imports with public owners, lexical inherited member types/hiding/diamond identity and access guards, order-independent inherited parent aliases through hidden enclosing owners, local invocations and explicit parameter binding/arity/guards, '
                  'block-local class/member/package/import shadows, nested type and static qualifier scope, reverse/path/exploration, traversal pages/rendering and metrics/top; not full MCP equivalence')
SOURCE = '''package fixture.{package};
class Probe {{
    int leaf() {{ return 1; }}
    int left() {{ return leaf(); }}
    int right() {{ return leaf(); }}
    int entry() {{ return left() + right(); }}
    int cycleA() {{ return cycleB(); }}
    int cycleB() {{ return cycleA(); }}
    int isolated() {{ return 0; }}
}}
'''
EDGES = {('left', 'leaf'), ('right', 'leaf'), ('entry', 'left'), ('entry', 'right'),
         ('cycleA', 'cycleB'), ('cycleB', 'cycleA')}
LINES = {'Probe': 2, 'leaf': 3, 'left': 4, 'right': 5, 'entry': 6,
         'cycleA': 7, 'cycleB': 8, 'isolated': 9}


def plan_graph(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      ('graph', 'pending', PENDING_REASON))
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-graph'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def identity(row):
    return row.get('path'), row.get('line'), row.get('name')


def authored(name, package='a'):
    return f'{package}/Probe.java', LINES[name], name


def reach(seed, depth):
    visited, queue = {}, deque([(seed, 0)])
    while queue:
        target, level = queue.popleft()
        if level >= max(1, depth):
            continue
        for source, end in sorted(EDGES):
            if end == target and source != seed and source not in visited:
                visited[source] = level + 1
                queue.append((source, level + 1))
    return sorted((d, authored(n)) for n, d in visited.items())


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('graph artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    # Preserve private command logs for interrupted or failed rounds.
    directory = Path(tempfile.mkdtemp(prefix='graph-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for package in ('a', 'b'):
        (runner.root / package).mkdir()
        (runner.root / package / 'Probe.java').write_text(SOURCE.format(package=package))
    (runner.root / 'Nested.java').write_text('''package fixture.a;
class Outer {
    class Inner {
        int innerLeaf() { return 1; }
        int innerEntry() { return innerLeaf(); }
    }
}
''')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, label, want, got):
        expected[feature][label], actual[feature][label] = want, got

    def lifecycle(label, want, output):
        graph = output.get('graph', {})
        record('graph:lifecycle', label, want, (graph.get('built'), graph.get('stale')))

    runner.command('rebuild', '--force')
    lifecycle('unbuilt-status', (False, False), runner.json('graph', 'status'))
    output = runner.json('graph', 'dependencies', 'leaf')
    record('graph:lifecycle', 'unbuilt-query', True, bool(output.get('error')))
    runner.json('graph', 'build')
    lifecycle('built', (True, False), runner.json('graph', 'status'))

    # The first ten observations exercise one shared seed resolver through
    # four commands. Fully qualified absent names must never broaden to leaf.
    for spec, matches in (
            ('fixture.a.Probe.leaf', [authored('leaf')]),
            ('fixture.a.Probe#leaf', [authored('leaf')]),
            ('fixture.b.Probe.leaf', [authored('leaf', 'b')]),
            ('fixture.b.Probe#leaf', [authored('leaf', 'b')]),
            ('Probe#leaf', [authored('leaf'), authored('leaf', 'b')]),
            ('fixture.a.Outer.Inner#innerLeaf', [('Nested.java', 4, 'innerLeaf')]),
            ('Outer.Inner#innerLeaf', [('Nested.java', 4, 'innerLeaf')]),
            ('fixture.absent.Probe.leaf', []),
            ('fixture.a.Probe', [authored('Probe')]),
            ('fixture.absent.Probe', [])):
        for command in ('dependencies', 'dependents', 'impact', 'metrics'):
            output = runner.json('graph', command, spec)
            rows = output.get('items', []) if command == 'metrics' else output.get('matched', [])
            rows = [row.get('symbol', {}) for row in rows] if command == 'metrics' else rows
            record('graph:java-selection', f'{spec}:{command}', sorted(matches), sorted(identity(r) for r in rows))
    for flags, wanted in ((('--in-file', 'a/'), [authored('leaf')]),
                          (('--in-file', 'b/', '--kind', 'function'), [authored('leaf', 'b')]),
                          (('--kind', 'class'), []), (('--in-file', 'absent'), [])):
        output = runner.json('graph', 'dependencies', 'leaf', *flags)
        record('graph:java-selection', str(flags), wanted, [identity(r) for r in output.get('matched', [])])

    for command in ('dependencies', 'dependents'):
        for seed in ('leaf', 'entry', 'cycleA', 'isolated'):
            names = sorted(b if command == 'dependencies' else a for a, b in EDGES
                           if (a if command == 'dependencies' else b) == seed)
            candidates = sorted(authored(n) for n in names)
            for cap in (0, 1, 20):
                output = runner.json('graph', command, f'fixture.a.Probe#{seed}', '--limit', cap)
                items = output.get('items', [])
                record('graph:java-traversal', f'{command}:{seed}:{cap}',
                       {'items': candidates[:cap], 'total': len(candidates), 'resolved': len(candidates),
                        'ambiguous': 0, 'confidence': True, 'references': True},
                       {'items': sorted(identity(r.get('other', {})) for r in items),
                        'total': output.get('pagination', {}).get('total'),
                        'resolved': output.get('resolved_edges'), 'ambiguous': output.get('ambiguous_edges'),
                        'confidence': all(r.get('confidence') == 'local' for r in items),
                        'references': all(r.get('references') == 1 for r in items)})
    for seed in ('leaf', 'cycleA', 'isolated'):
        for depth in (0, 1, 2, 4):
            candidates = reach(seed, depth)
            for cap in (0, 1, 20):
                output = runner.json('graph', 'impact', f'fixture.a.Probe#{seed}', '--depth', depth, '--limit', cap)
                record('graph:java-traversal', f'impact:{seed}:{depth}:{cap}',
                       {'items': candidates[:cap], 'total': len(candidates), 'files': int(bool(candidates))},
                       {'items': [(r.get('depth'), identity(r.get('symbol', {}))) for r in output.get('items', [])],
                        'total': output.get('total_symbols'), 'files': output.get('total_files')})
    paths = {(authored('entry'), authored(branch), authored('leaf')) for branch in ('left', 'right')}
    for start, end, direction in (('entry', 'leaf', 'forward'), ('leaf', 'entry', 'reverse')):
        for depth in (0, 1, 2, 8):
            for cap in (0, 1, 3):
                output = runner.json('graph', 'path', f'fixture.a.Probe#{start}', f'fixture.a.Probe#{end}',
                                     '--max-depth', depth, '--max-paths', cap)
                found = depth >= 2
                got = [tuple(identity(h.get('symbol', {})) for h in path) for path in output.get('items', [])]
                record('graph:java-traversal', f'path:{start}:{depth}:{cap}',
                       {'direction': direction if found else None, 'length': 2 if found else None,
                        'count': 2 if found else 0, 'returned': min(cap, 2) if found else 0, 'valid': True},
                       {'direction': output.get('direction'), 'length': output.get('length'),
                        'count': output.get('shortest_paths'), 'returned': len(got),
                        'valid': len(set(got)) == len(got) and set(got) <= paths})
    for prefix in ('a/', 'b/', 'absent'):
        for size in (2, 3):
            for cap in (0, 1, 20):
                output = runner.json('graph', 'cycles', '--path', prefix, '--min-size', size, '--limit', cap)
                want = [sorted([authored('cycleA', prefix[0]), authored('cycleB', prefix[0])])] if prefix != 'absent' and size == 2 else []
                items = output.get('items', [])
                record('graph:java-traversal', f'cycles:{prefix}:{size}:{cap}',
                       {'members': want[:cap], 'total': len(want), 'example': True},
                       {'members': [sorted(identity(r) for r in c.get('members', [])) for c in items],
                        'total': output.get('components'),
                        'example': all(len(c.get('example', [])) == 3 and
                                       identity(c['example'][0]) == identity(c['example'][-1]) and
                                       {identity(r) for r in c['example']} == set(want[0]) for c in items)})
    output = runner.json('graph', 'dependencies', 'fixture.a.Probe', '--members')
    record('graph:java-traversal', 'class-members-internal-edges', [], output.get('items'))
    traversal_expected, traversal_actual = graph_traversal_contracts.exercise(runner, authored)
    expected.update(traversal_expected)
    actual.update(traversal_actual)
    source = runner.root / 'a/Probe.java'
    source.write_text(source.read_text().replace('return 1;', 'return 2;'))
    runner.command('update')
    lifecycle('stale-status', (True, True), runner.json('graph', 'status'))
    lifecycle('stale-query', (True, True), runner.json('graph', 'dependents', 'leaf'))
    lifecycle('refresh', (True, False), runner.json('graph', 'dependents', 'leaf', '--refresh'))
    lifecycle('fresh-status', (True, False), runner.json('graph', 'status'))
    metrics_expected, metrics_actual = graph_metrics_contracts.exercise(binary, base)
    expected.update(metrics_expected)
    actual.update(metrics_actual)
    return expected, actual
