"""Java ambiguity budgets and overlapping path seeds, not MCP equivalence.

Javac accepts the fixture; unresolved expression types deliberately remain
ambiguous in the native graph. This verifies conservative representation and
traversal, without pretending to establish compiler-wide overload dispatch.
"""
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from graph_traversal_contracts import notice, pagination, path_text

FEATURES = {'graph:java-ambiguity-budgets', 'global:format:java-graph-ambiguity',
            'graph:java-overlapping-path-seeds'}
REASON = ('independent source/state and internal CLI: disposable javac-validated Java '
          'unresolved overload candidate budgets, default exclusion, direct/reverse/impact '
          'traversal, metrics counters, JSON/text pages and overlapping zero-hop path seeds; '
          'not MCP equivalence or compiler-wide overload dispatch')
SOURCES = {'Probe.java': '''package fixture;
class Probe {
 int leaf() { return 1; }
 int choose(Object x) { return leaf(); }
 int choose(String x) { return leaf(); }
 int use(java.util.function.Supplier<Object> supplier) {
  return choose(supplier.get());
 }
 int wrap(java.util.function.Supplier<Object> supplier) { return use(supplier); }
}
''', 'Other.java': '''package fixture;
class Other {
 int leaf() { return 2; }
}
'''}
TYPES = ('Object', 'String', 'Integer', 'Long', 'Double', 'Float', 'Byte', 'Short', 'Boolean')
SOURCES['Budget.java'] = ('package fixture;\nclass Budget {\n' + ''.join(
    ''.join(f' int {name}({kind} x) {{ return 1; }}\n' for kind in TYPES[:count]) +
    f' int use{count}(java.util.function.Supplier<Object> supplier) {{ return {name}(supplier.get()); }}\n'
    for name, count in (('bounded', 8), ('overflow', 9))) + '}\n')


def plan_contracts(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-graph-ambiguity'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        # The aggregate contracts retain their semantic/selector gaps.
        state.execute("UPDATE coverage SET reason=REPLACE(REPLACE(reason,"
                      "'ambiguity budgets', 'compiler-wide overload dispatch'),"
                      "'ambiguous rendering and overlapping path seeds', 'receiver dispatch') || ? "
                      "WHERE feature='graph' AND status='pending'",
                      ('; separate Java ambiguity fixture checks bounded candidate representation, '
                       'ambiguous traversal/rendering and overlapping path endpoints',))
        state.execute("UPDATE coverage SET reason=REPLACE(reason,"
                      "'ambiguous Java graph rendering and ', '') || ? "
                      "WHERE feature='global:format' AND status='pending'",
                      ('; separate Java graph ambiguity fixture executes JSON/text pages; '
                       'global-selector failure composition remains unresolved',))


def identity(row):
    return row.get('path'), row.get('line'), row.get('name')


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('graph ambiguity fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='graph-ambiguity-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for path, source in SOURCES.items():
        (runner.root / path).write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': 3, '.kt': 1}:
            raise ToolError('graph ambiguity full inventory incomplete')
    finally:
        state.close()
    with (directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'),
                                 *[str(runner.root / p) for p in SOURCES]],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('graph ambiguity Java fixture rejected; see private javac log')
    runner.command('rebuild', '--force')
    build = runner.json('graph', 'build')
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))
    budgets, formats, overlaps = ('graph:java-ambiguity-budgets',
                                  'global:format:java-graph-ambiguity',
                                  'graph:java-overlapping-path-seeds')

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    def declarations(path, name):
        return [(path, line, name) for line, text in enumerate(SOURCES[path].splitlines(), 1)
                if f'int {name}(' in text]

    def symbol(row):
        path, line, name = row
        return f'{name} [function] {path}:{line}'

    def page(rows, candidates, cap):
        # Retain duplicates and unexpected identities. Never use native rows
        # to supply expected declarations or collapse a defective page.
        return {'rows': min(cap, len(candidates)),
                'valid': len(rows) == len(set(rows)) and set(rows) <= set(candidates),
                'complete': cap < len(candidates) or sorted(rows) == sorted(candidates)}

    record(budgets, 'build-cap', 8, build.get('ambiguity_cap'))
    record(budgets, 'overflow-drop', 1,
           sum(r.get('references', 0) for r in build.get('dropped', []) if r.get('reason') == 'too_ambiguous'))
    for path, seed, target, count in (('Probe.java', 'use', 'choose', 2),
                                     ('Budget.java', 'use8', 'bounded', 8),
                                     ('Budget.java', 'use9', 'overflow', 0)):
        spec = f'fixture.{Path(path).stem}#{seed}'
        source = declarations(path, seed)[0]
        candidates = declarations(path, target) if count else []
        for include in (False, True):
            flags = ['--include-ambiguous'] if include else []
            for cap in (0, 1, 20):
                doc = runner.json('graph', 'dependencies', spec, *flags, '--limit', cap)
                rows = [identity(r.get('other', {})) for r in doc.get('items', [])]
                visible = candidates if include else []
                key = f'dependencies:{seed}:{include}:{cap}'
                record(budgets, key,
                       {'counts': [0, count], 'pagination': pagination(len(visible), cap),
                        'page': page(visible[:cap], visible, cap), 'edges': True},
                       {'counts': [doc.get('resolved_edges'), doc.get('ambiguous_edges')],
                        'pagination': doc.get('pagination'), 'page': {**page(rows, visible, cap), 'rows': len(rows)},
                        'edges': all(identity(r.get('subject', {})) == source and
                                     r.get('confidence') == 'ambiguous' and r.get('candidates') == count and
                                     r.get('references') == 1 for r in doc.get('items', []))})
                _, text = runner.command('graph', 'dependencies', spec, *flags, '--limit', cap)
                want = f"Dependencies of '{spec}':\n  {symbol(source)}\n  0 resolved edge(s), {count} ambiguous"
                want += ' (hidden; --include-ambiguous lists them)' if count and not include else ''
                want += '.\n'
                want += ''.join(f'  [ambiguous 1/{count}] {symbol(row)} (1 ref)\n' for row in visible[:cap])
                if not visible[:cap]:
                    want += '  No edges.\n'
                want += notice(len(visible), cap)
                record(formats, key, want, text)
        for cap in (0, 1, 20):
            doc = runner.json('graph', 'metrics', spec, '--limit', cap)
            fan_in = int(seed == 'use')
            wanted = [(source, fan_in, 0, 0, count, fan_in)][:cap]
            record(budgets, f'{seed}:metrics:{cap}',
                   {'pagination': pagination(1, cap), 'rows': wanted},
                   {'pagination': doc.get('pagination'),
                    'rows': [(identity(r.get('symbol', {})), r.get('fan_in'), r.get('fan_in_ambiguous'),
                              r.get('fan_out'), r.get('fan_out_ambiguous'), r.get('dependents'))
                             for r in doc.get('items', [])]})
            _, text = runner.command('graph', 'metrics', spec, '--limit', cap)
            want = (f'  {symbol(source)}\n'
                    f'    fan-in {fan_in} ({fan_in} files, +0 ambiguous) · '
                    f'fan-out 0 (+{count} ambiguous) · dependents≤3 {fan_in} · '
                    'pagerank <rank> (<percentile>)\n') if cap else ''
            # Stationary rank is a separate fixture. Keep the exact text and
            # authored ambiguity counters, normalizing only those two numbers.
            got = re.sub(r'pagerank \d+\.\d{2} \(p\d+\)', 'pagerank <rank> (<percentile>)', text)
            record(formats, f'{seed}:metrics:{cap}', want + notice(1, cap), got)

    choose = declarations('Probe.java', 'choose')
    use, wrap, leaf = (declarations('Probe.java', n)[0] for n in ('use', 'wrap', 'leaf'))
    for include in (False, True):
        flags = ['--include-ambiguous'] if include else []
        for cap in (0, 1, 20):
            doc = runner.json('graph', 'dependents', 'fixture.Probe#choose', *flags, '--limit', cap)
            wanted = [(target, use, 'ambiguous', 2, 1, 7) for target in choose] if include else []
            rows = [(identity(r.get('subject', {})), identity(r.get('other', {})),
                     r.get('confidence'), r.get('candidates'), r.get('references'), r.get('line'))
                    for r in doc.get('items', [])]
            record(budgets, f'reverse:{include}:{cap}',
                   {'counts': [0, 2], 'pagination': pagination(len(wanted), cap),
                    'page': page(wanted[:cap], wanted, cap)},
                   {'counts': [doc.get('resolved_edges'), doc.get('ambiguous_edges')],
                    'pagination': doc.get('pagination'), 'page': {**page(rows, wanted, cap), 'rows': len(rows)}})
            _, text = runner.command('graph', 'dependents', 'fixture.Probe#choose', *flags, '--limit', cap)
            want = ("Dependents of 'fixture.Probe#choose':\n"
                    "  2 definitions match 'fixture.Probe#choose' and their edges are merged; narrow with "
                    "'Outer::choose' or 'Class#choose', --in-file or --kind.\n")
            want += ''.join('  ' + symbol(row) + '\n' for row in choose[:max(1, cap)])
            if cap < 2:
                want += '  … and 1 more definition(s) (--limit lists more).\n'
            want += '  0 resolved edge(s), 2 ambiguous' + ('' if include else ' (hidden; --include-ambiguous lists them)') + '.\n'
            want += ''.join('  [ambiguous 1/2] use [function] Probe.java:7 (1 ref) <- choose\n'
                            for _ in wanted[:cap])
            if not wanted[:cap]:
                want += '  No edges.\n'
            record(formats, f'reverse:{include}:{cap}', want + notice(len(wanted), cap), text)
            doc = runner.json('graph', 'impact', 'fixture.Probe#leaf', *flags, '--depth', 3, '--limit', cap)
            wanted = [(1, r, 'local') for r in choose] + ([(2, use, 'ambiguous'), (3, wrap, 'local')] if include else [])
            rows = [(r.get('depth'), identity(r.get('symbol', {})), r.get('confidence')) for r in doc.get('items', [])]
            record(budgets, f'impact:{include}:{cap}',
                   {'pagination': pagination(len(wanted), cap), 'page': page(wanted[:cap], wanted, cap),
                    'totals': [len(wanted), 1], 'resolved': 2 if include else None},
                   {'pagination': doc.get('pagination'), 'page': {**page(rows, wanted, cap), 'rows': len(rows)},
                    'totals': [doc.get('total_symbols'), doc.get('total_files')],
                    'resolved': doc.get('resolved_only_symbols')})
            _, text = runner.command('graph', 'impact', 'fixture.Probe#leaf', *flags, '--depth', 3, '--limit', cap)
            want = ("Impact of 'fixture.Probe#leaf' (transitive dependents, depth 3, " +
                    ('resolved + ambiguous' if include else 'resolved') + ' edges):\n' +
                    '  ' + symbol(leaf) + '\n  depth 1: 2 symbol(s) in 1 file(s)\n')
            if include:
                want += '  depth 2: 1 symbol(s) in 1 file(s)\n  depth 3: 1 symbol(s) in 1 file(s)\n'
            want += f'  total: {len(wanted)} symbol(s) in 1 file(s)\n'
            if include:
                want += '  resolved edges only: 2 symbol(s) in 1 file(s); the rest is an upper bound through ambiguous names\n'
            for level, row, confidence in wanted[:cap]:
                via = {1: 'leaf', 2: 'choose', 3: 'use'}[level]
                want += f'  d{level} {symbol(row)} -> {via} ({confidence})\n'
            record(formats, f'impact:{include}:{cap}', want + notice(len(wanted), cap), text)

    def paths(key, a, b, paths, edges, depth, flags=(), direction='forward', feature=budgets):
        for cap in (0, 1, 20):
            doc = runner.json('graph', 'path', a, b, '--max-depth', depth, '--max-paths', cap, *flags)
            rows = [tuple(identity(h.get('symbol', {})) for h in row) for row in doc.get('items', [])]
            record(feature, f'{key}:{cap}',
                   {'direction': direction if paths else None, 'length': len(paths[0]) - 1 if paths else None,
                    'total': len(paths), 'pagination': pagination(len(paths), cap),
                    'page': page(paths[:cap], paths, cap), 'edges': True},
                   {'direction': doc.get('direction'), 'length': doc.get('length'), 'total': doc.get('shortest_paths'),
                    'pagination': doc.get('pagination'), 'page': {**page(rows, paths, cap), 'rows': len(rows)},
                    'edges': all([h.get('edge') for h in row] == edges for row in doc.get('items', []))})
            _, text = runner.command('graph', 'path', a, b, '--max-depth', depth, '--max-paths', cap, *flags)
            if not paths:
                suffix = '' if '--include-ambiguous' in flags else ' over resolved edges (try --include-ambiguous)'
                record(formats, f'{key}:{cap}',
                       f"No dependency path between '{a}' and '{b}' within {depth} hop(s){suffix}.\n", text)
                continue
            start, end = (a, b) if direction == 'forward' else (b, a)
            header = f"'{start}' reaches '{end}' in {len(paths[0])-1} hop(s); {len(paths)} shortest path(s), showing {min(cap, len(paths))}:\n"
            if direction == 'reverse':
                header += f"  No path from '{a}' to '{b}'; this is the reverse direction.\n"
            blocks = [''.join('    ' + symbol(h) + (f' -> [{edge}]' if edge else '') + '\n'
                              for h, edge in zip(row, edges)) for row in paths]
            got = path_text(text)
            record(formats, f'{key}:{cap}',
                   {'header': header, 'page': page(blocks[:cap], blocks, cap), 'notice': notice(len(paths), cap, '--max-paths')},
                   {'header': got['header'], 'page': {**page(got['paths'], blocks, cap), 'rows': len(got['paths'])},
                    'notice': got['notice']})

    for a, b, direction in [('fixture.Probe#use', 'fixture.Probe#leaf', 'forward'),
                             ('fixture.Probe#leaf', 'fixture.Probe#use', 'reverse')]:
        paths(direction + ':hidden', a, b, [], [], 3)
        paths(direction + ':ambiguous', a, b, [(use, r, leaf) for r in choose],
              ['ambiguous', 'local', None], 3, ['--include-ambiguous'], direction)
    other = declarations('Other.java', 'leaf')[0]
    for key, a, b, rows, flags in [
            ('self', 'fixture.Probe#leaf', 'fixture.Probe#leaf', [leaf], []),
            ('overloads', 'choose', 'fixture.Probe#choose', choose, []),
            ('broad', 'leaf', 'leaf', [leaf, other], []),
            ('intersection', 'leaf', 'fixture.Probe#leaf', [leaf], []),
            ('class-member', 'fixture.Probe', 'fixture.Probe#choose', choose, []),
            ('disjoint', 'leaf', 'leaf', [], ['--from-file', 'Probe.java', '--to-file', 'Other.java'])]:
        for depth in (0, 3):
            paths(f'{key}:{depth}', a, b, [(row,) for row in rows], [None], depth, flags, feature=overlaps)
    return expected, actual
