"""Authored Java traversal pages and text, independent of native DB/MCP truth.

Uses the local diamond/cycle source in graph_contracts. Expected identities,
edges and counts come from that source; a second native query never supplies
the expected text or pagination. Dispatch and attached roots remain pending.
"""
import re

FEATURES = {'graph:java-path-pagination', 'graph:traversal-rendering'}


def pagination(total, cap):
    return dict(total=total, returned=min(total, cap), limit=cap, truncated=total > cap)


def notice(total, cap, flag='--limit'):
    return (f'  Truncated: showing {cap} of {total} results; use {flag} {total} to see all.\n'
            if total > cap else '')


def path_text(output):
    """Ignore only path enumeration order, retaining every hop and duplicate."""
    chunks = re.split(r'^  path \d+:\n', output, flags=re.MULTILINE)
    header = chunks.pop(0)
    tail = ''
    if chunks:
        last = chunks[-1]
        match = re.search(r'^  Truncated:.*\n', last, re.MULTILINE)
        if match:
            tail = match.group()
            chunks[-1] = last[:match.start()] + last[match.end():]
    else:
        match = re.search(r'^  Truncated:.*\n', header, re.MULTILINE)
        if match:
            tail = match.group()
            header = header[:match.start()] + header[match.end():]
    return {'header': header, 'paths': sorted(chunks), 'notice': tail}


def exercise(runner, authored):
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, label, want, got):
        expected[feature][label], actual[feature][label] = want, got

    def symbol(name):
        path, line, _ = authored(name)
        return f'{name} [function] {path}:{line}'

    # One source-authored diamond has exactly two distinct shortest paths,
    # independent of output limits and of the query's forward/reverse spelling.
    paths = {tuple(authored(n) for n in ('entry', branch, 'leaf'))
             for branch in ('left', 'right')}
    for start, end in (('entry', 'leaf'), ('leaf', 'entry')):
        a, b = f'fixture.a.Probe#{start}', f'fixture.a.Probe#{end}'
        for depth in (2, 1):
            for cap in (0, 1, 3):
                report = runner.json('graph', 'path', a, b, '--max-depth', depth, '--max-paths', cap)
                rows = [tuple((h.get('symbol', {}).get('path'), h.get('symbol', {}).get('line'),
                               h.get('symbol', {}).get('name')) for h in row)
                        for row in report.get('items', [])]
                found = depth == 2
                total = 2 if found else 0
                label = f'path:{start}:{depth}:{cap}'
                record('graph:java-path-pagination', label,
                       {'pagination': pagination(total, cap), 'shortest': total,
                        'rows': min(total, cap), 'valid': True, 'confidence': True},
                       {'pagination': report.get('pagination'), 'shortest': report.get('shortest_paths'),
                        'rows': len(rows), 'valid': len(rows) == len(set(rows)) and set(rows) <= paths,
                        'confidence': all([h.get('edge') for h in row] == ['local', 'local', None]
                                          for row in report.get('items', []))})
                _, output = runner.command('graph', 'path', a, b, '--max-depth', depth, '--max-paths', cap)
                if found:
                    header = (f"'fixture.a.Probe#entry' reaches 'fixture.a.Probe#leaf' in 2 hop(s); "
                              f'2 shortest path(s), showing {min(cap, 2)}:\n')
                    if start == 'leaf':
                        header += f"  No path from '{a}' to '{b}'; this is the reverse direction.\n"
                    blocks = [''.join('    ' + symbol(n) + (' -> [local]' if n != 'leaf' else '') + '\n'
                                      for n in ('entry', branch, 'leaf')) for branch in ('left', 'right')]
                    got = path_text(output)
                    # A capped page may select either equal-length branch. Its
                    # identities must still be authored and unique, never inferred
                    # from the JSON query above or collapsed with a set.
                    wanted = {'header': header, 'count': min(cap, 2), 'valid': True,
                              'notice': notice(2, cap, '--max-paths')}
                    observed = {'header': got['header'], 'count': len(got['paths']),
                                'valid': len(set(got['paths'])) == len(got['paths']) and
                                         all(p in blocks for p in got['paths']), 'notice': got['notice']}
                else:
                    wanted = f"No dependency path between '{a}' and '{b}' within 1 hop(s) over resolved edges (try --include-ambiguous).\n"
                    observed = output
                record('graph:traversal-rendering', label, wanted, observed)

    for command, seed, neighbours in (
            ('dependencies', 'entry', ['left', 'right']),
            ('dependents', 'leaf', ['left', 'right']),
            ('dependencies', 'isolated', [])):
        spec = f'fixture.a.Probe#{seed}'
        title = 'Dependencies of' if command == 'dependencies' else 'Dependents of'
        for cap in (0, 1, 20):
            _, output = runner.command('graph', command, spec, '--limit', cap)
            want = f"{title} '{spec}':\n  {symbol(seed)}\n  {len(neighbours)} resolved edge(s), 0 ambiguous.\n"
            for n in neighbours[:cap]:
                want += f'  [local] {symbol(n)} (1 ref)\n'
            if not neighbours[:cap]:
                want += '  No edges.\n'
            want += notice(len(neighbours), cap)
            record('graph:traversal-rendering', f'{command}:{seed}:{cap}', want, output)

    for seed, depth, candidates in (
            ('leaf', 1, [('left', 'leaf'), ('right', 'leaf')]),
            ('left', 2, [('entry', 'left')]), ('isolated', 2, [])):
        spec = f'fixture.a.Probe#{seed}'
        for cap in (0, 1, 20):
            _, output = runner.command('graph', 'impact', spec, '--depth', depth, '--limit', cap)
            want = f"Impact of '{spec}' (transitive dependents, depth {depth}, resolved edges):\n  {symbol(seed)}\n"
            if candidates:
                want += f'  depth 1: {len(candidates)} symbol(s) in 1 file(s)\n'
            want += f'  total: {len(candidates)} symbol(s) in {int(bool(candidates))} file(s)\n'
            for name, via in candidates[:cap]:
                want += f'  d1 {symbol(name)} -> {via} (local)\n'
            want += notice(len(candidates), cap)
            record('graph:traversal-rendering', f'impact:{seed}:{cap}', want, output)

    for size in (2, 3):
        for cap in (0, 1, 20):
            _, output = runner.command('graph', 'cycles', '--path', 'a/', '--min-size', size, '--limit', cap)
            total = int(size == 2)
            want = f'Dependency cycles over resolved edges: {total} component(s) with {size}+ symbols.\n'
            if total and cap:
                want += ('  2 symbols in 1 file(s):\n    cycle: cycleA -> cycleB -> cycleA\n'
                         f'    {symbol("cycleA")}\n    {symbol("cycleB")}\n')
            else:
                want += '  No cycles.\n'
            want += notice(total, cap)
            record('graph:traversal-rendering', f'cycles:{size}:{cap}', want, output)

    for command in ('dependencies', 'dependents', 'impact'):
        spec = 'fixture.absent.Probe#leaf'
        _, output = runner.command('graph', command, spec)
        record('graph:traversal-rendering', command + ':absent', f"No symbol matches '{spec}'.\n", output)
    return expected, actual
