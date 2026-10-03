"""Authored Java graph metrics and ranking, without native DB or MCP truth.

PageRank expectations solve the stationary equations with exact rational
arithmetic, independently of the production power iteration. Resolution beyond
local Java calls is deliberately left to the pending graph contract.
"""
from fractions import Fraction
from pathlib import Path
import re
import tempfile

from common import ToolError
from root_contracts import Runner

FEATURES = {'graph:java-metrics', 'graph:java-top', 'graph:metrics-rendering'}
SOURCE = '''package fixture;
class Probe {
    int leaf() { return 1; }
    int left() { return leaf(); }
    int right() { return leaf(); }
    int entry() { return left() + right(); }
    int cycleA() { return cycleB(); }
    int cycleB() { return cycleA(); }
    int isolated() { return 0; }
    int twice() { return leaf() + leaf(); }
    int hop1() { return leaf(); }
    int hop2() { return hop1(); }
    int hop3() { return hop2(); }
    int hop4() { return hop3(); }
}
'''
TEST_SOURCE = '''package fixture;
class ProbeTest {
    int testLeaf() { return 1; }
    int testEntry() { return testLeaf(); }
}
'''
LINES = dict(zip(('leaf', 'left', 'right', 'entry', 'cycleA', 'cycleB',
                  'isolated', 'twice', 'hop1', 'hop2', 'hop3', 'hop4'), range(3, 15)))
LINES.update(testLeaf=3, testEntry=4)
EDGES = {('left', 'leaf'), ('right', 'leaf'), ('entry', 'left'), ('entry', 'right'),
         ('cycleA', 'cycleB'), ('cycleB', 'cycleA'), ('twice', 'leaf'),
         ('hop1', 'leaf'), ('hop2', 'hop1'), ('hop3', 'hop2'), ('hop4', 'hop3'),
         ('testEntry', 'testLeaf')}
METRIC_KEYS = ('fan_in', 'fan_in_files', 'fan_out', 'fan_in_ambiguous',
               'fan_out_ambiguous', 'dependents', 'dependents_depth', 'pagerank', 'pagerank_pct')


def identity(name):
    return ('tests/ProbeTest.java' if name.startswith('test') else 'Probe.java', LINES[name], name)


def stationary_ranks():
    """Solve (I - d P) r = (1-d)/n, including uniform dangling mass."""
    names = sorted({name for edge in EDGES for name in edge})
    count, damping = len(names), Fraction(85, 100)
    matrix = []
    for target in names:
        row = []
        for source in names:
            outgoing = {end for start, end in EDGES if start == source}
            probability = (Fraction(target in outgoing, len(outgoing)) if outgoing else Fraction(1, count))
            row.append(Fraction(source == target) - damping * probability)
        matrix.append(row + [(1 - damping) / count])
    for pivot in range(count):
        scale = matrix[pivot][pivot]
        matrix[pivot] = [value / scale for value in matrix[pivot]]
        for row in range(count):
            if row != pivot:
                scale = matrix[row][pivot]
                matrix[row] = [a - scale * b for a, b in zip(matrix[row], matrix[pivot])]
    return {name: matrix[index][-1] * count for index, name in enumerate(names)}


def authored_metrics():
    ranks = stationary_ranks()
    # Distinct transitive callers within three hops, excluding the seed itself.
    dependents = dict(leaf=7, left=1, right=1, entry=0, cycleA=1, cycleB=1,
                      isolated=0, twice=0, hop1=3, hop2=2, hop3=1, hop4=0,
                      testLeaf=1, testEntry=0)
    result = {}
    for name in LINES:
        incoming = {source for source, target in EDGES if target == name}
        rank = ranks.get(name, 0)
        pct = (100 * (sum(value < rank for value in ranks.values()) +
                      Fraction(sum(value == rank for value in ranks.values()), 2)) / len(ranks)
               if name in ranks else 0)
        result[name] = {'fan_in': len(incoming), 'fan_in_files': len({identity(n)[0] for n in incoming}),
                        'fan_out': sum(source == name for source, _ in EDGES),
                        'fan_in_ambiguous': 0, 'fan_out_ambiguous': 0,
                        'dependents': dependents[name], 'dependents_depth': 3,
                        'pagerank': round(float(rank), 6), 'pagerank_pct': round(float(pct), 2)}
    return result


def metric_rows(output):
    rows = []
    for row in output.get('items', []):
        symbol = row.get('symbol', {})
        if (not isinstance(symbol.get('path'), str) or not isinstance(symbol.get('line'), int)
                or not isinstance(symbol.get('name'), str)):
            raise ToolError('graph metrics lack a declaration identity')
        # Preserve unexpected names and duplicate rows as mismatches, without
        # using the authored oracle to rewrite native identities or metrics.
        rows.append(((symbol['path'], symbol['line'], symbol['name']),
                     {key: row.get(key) for key in METRIC_KEYS}))
    return sorted(rows, key=lambda row: row[0])


def text_rows(output):
    symbols = re.findall(r'^\s+(?:\d+\. )?(\w+) \[function\] (.+\.java):(\d+)$', output, re.MULTILINE)
    numbers = re.findall(r'fan-in (\d+) \((\d+) files, \+(\d+) ambiguous\) · '
                         r'fan-out (\d+) \(\+(\d+) ambiguous\) · dependents≤(\d+) (\d+) · '
                         r'pagerank ([\d.]+) \(p(\d+)\)', output)
    if len(symbols) != len(numbers):
        raise ToolError('graph text metrics do not match rendered identities')
    return sorted(((path, int(line), name), tuple(map(float, values)))
                  for (name, path, line), values in zip(symbols, numbers))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('graph metrics artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='graph-metrics-', dir=base)).resolve())
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    (runner.root / 'Probe.java').write_text(SOURCE)
    (runner.root / 'tests').mkdir()
    (runner.root / 'tests/ProbeTest.java').write_text(TEST_SOURCE)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, label, want, got):
        expected[feature][label], actual[feature][label] = want, got

    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    metrics = authored_metrics()
    # These first ten metric observations are distinct, source-authored cases.
    for name in LINES:
        output = runner.json('graph', 'metrics', name)
        record('graph:java-metrics', name, [(identity(name), metrics[name])], metric_rows(output))
    for specs, names in ((['leaf', 'leaf'], ['leaf']),
                         (['leaf', 'Probe#leaf', 'fixture.Probe.leaf'], ['leaf']),
                         (['left', 'right', 'entry'], ['left', 'right', 'entry']),
                         (['__absent__'], [])):
        for cap in (0, 1, 50):
            output = runner.json('graph', 'metrics', *specs, '--limit', cap)
            candidates = sorted(names, key=lambda n: -metrics[n]['pagerank'])
            want = sorted((identity(n), metrics[n]) for n in candidates[:cap])
            label = ':'.join(specs) + f':{cap}'
            record('graph:java-metrics', label, {'rows': want, 'total': len(names)},
                   {'rows': metric_rows(output), 'total': output.get('pagination', {}).get('total')})
    for flags, names in ((('--in-file', 'tests/'), ['testLeaf']),
                          (('--kind', 'class'), []), (('--in-file', 'absent'), [])):
        output = runner.json('graph', 'metrics', 'testLeaf', *flags)
        record('graph:java-metrics', str(flags), sorted((identity(n), metrics[n]) for n in names), metric_rows(output))

    active = {name for edge in EDGES for name in edge}
    for sort, key in (('pagerank', 'pagerank'), ('fan-in', 'fan_in'),
                      ('fan-out', 'fan_out'), ('dependents', 'dependents')):
        for flags, names in (((), active), (('--path', 'tests/'), {'testLeaf', 'testEntry'}),
                             (('--exclude-tests',), active - {'testLeaf', 'testEntry'}),
                             (('--kind', 'class'), set()), (('--path', 'absent'), set()),
                             (('--path', 'tests/', '--exclude-tests'), set())):
            full = runner.json('graph', 'top', '--sort', sort, *flags, '--limit', 50)
            values = [row.get(key) for row in full.get('items', [])]
            if not all(isinstance(value, (int, float)) for value in values):
                raise ToolError('graph ranking lacks a numeric sort value')
            label = sort + ':' + str(flags)
            record('graph:java-top', label,
                   {'rows': sorted((identity(n), metrics[n]) for n in names), 'ordered': True, 'total': len(names)},
                   {'rows': metric_rows(full), 'ordered': values == sorted(values, reverse=True),
                    'total': full.get('pagination', {}).get('total')})
            for cap in (0, 1, 3):
                output = runner.json('graph', 'top', '--sort', sort, *flags, '--limit', cap)
                record('graph:java-top', label + f':{cap}',
                       {'rows': full.get('items', [])[:cap], 'total': len(names), 'returned': min(cap, len(names))},
                       {'rows': output.get('items', []), 'total': output.get('pagination', {}).get('total'),
                        'returned': output.get('pagination', {}).get('returned')})
    record('graph:java-top', 'bad-sort-rejected', 1,
           runner.command('graph', 'top', '--sort', 'invalid', acceptable=(0, 1))[0])

    for arguments in (('metrics', 'leaf', 'left', 'right'), ('metrics', 'isolated'),
                      ('metrics', '__absent__'), ('top', '--sort', 'fan-in'),
                      ('top', '--path', 'tests/'), ('top', '--path', 'absent')):
        for cap in (0, 1, 50):
            report = runner.json('graph', *arguments, '--limit', cap)
            _, output = runner.command('graph', *arguments, '--limit', cap)
            want = []
            for row in report['items']:
                name = row['symbol']['name']
                if name not in metrics:
                    raise ToolError('graph rendering returned an unauthored declaration')
                m = metrics[name]
                values = tuple(map(float, (m['fan_in'], m['fan_in_files'], m['fan_in_ambiguous'],
                         m['fan_out'], m['fan_out_ambiguous'], 3, m['dependents'],
                         f"{m['pagerank']:.2f}", f"{m['pagerank_pct']:.0f}")))
                want.append((identity(name), values))
            total = report['pagination']['total']
            returned = min(cap, total)
            notice = f'Truncated: showing {returned} of {total} results; use --limit {total} to see all.'
            record('graph:metrics-rendering', str(arguments) + f':{cap}',
                   {'rows': sorted(want), 'truncation': total > cap},
                   {'rows': text_rows(output), 'truncation': notice in output})
    return expected, actual
