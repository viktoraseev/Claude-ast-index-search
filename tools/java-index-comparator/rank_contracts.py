"""Java preset contracts: authored graph/history, plus labelled CLI pool promises.

The expected graph solves stationary equations independently. No native DB is
an oracle, and no MCP equivalence is claimed for this oracle-less CLI feature.
"""
from pathlib import Path
import tempfile
import time
import math

from common import ToolError, stable_id
from root_contracts import Runner
from vcs_contracts import History, percentile
import graph_metrics_contracts as graph

FEATURES = {'search:rank-presets'}
REASON = ('independent source/state: disposable Java preset formulas, referenced-only graph '
          'percentiles, substance/lineage, strongest-file symbols and bounded pool/filter '
          'contracts; internal CLI relevance/blend checks; not MCP equivalence')


def rounded(value, places=3):
    scale = 10 ** places
    return math.floor(value * scale + .5) / scale


def plan_rank(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-presets'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('rank artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='rank-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    history = History(runner)
    history.git('init', '-q', '--template=', '--initial-branch=main')
    expected, actual = {}, {}

    def record(label, want, got):
        expected[label], actual[label] = want, got

    def search(query, preset=None, *flags):
        return runner.json('search', query, *(('--rank', preset) if preset else ()), *flags)

    padding = '\n' * 12
    bodies = {
        'Solid': 'class Solid { int value = 1; int run() { return value; } }',
        'Nested': 'class Nested { class Inner {} }',
        'Initialized': 'class Initialized { static { System.nanoTime(); } }',
        'Empty': 'class Empty {\n // a comment is not a member\n /* nor is this\n comment */\n}',
        'Tiny': 'class Tiny { int value = 1; }',
        'Abstract': 'interface Abstract { int run(); }',
        'Mode': 'enum Mode { FIRST }',
        'Pair': 'record Pair(int value) {}',
        'Marked': '@Deprecated\nclass Marked {\n int value;\n}',
        'Constructed': 'class Constructed { Constructed() {} }',
    }
    history.commit({'Probe.java': graph.SOURCE, 'tests/ProbeTest.java': graph.TEST_SOURCE,
                    **{f'body/{name}.java': ('package fixture.body;\n' + body + '\n' +
                                           ('' if name == 'Tiny' else padding))
                       for name, body in bodies.items()}})
    runner.command('rebuild', '--force')
    runner.command('hotspots', '--collect', '--full')
    runner.command('graph', 'build')
    # Ten source-authored substance cases. Single-line substantive bodies and
    # multiline comment-only bodies must have the same answer as their syntax.
    for name in bodies:
        doc = search(name, 'proven', '--type', {'Abstract': 'interface', 'Mode': 'enum'}.get(name, 'class'), '--limit', '100')
        row = next((r for r in doc['symbols'] if r['name'] == name), {})
        want = 'short_file' if name == 'Tiny' else 'empty_class_body' if name == 'Empty' else None
        record('substance:' + name, (True, want), (bool(row), row.get('rank', {}).get('proven', {}).get('stub')))

    metrics = graph.authored_metrics()
    referenced = [name for name in graph.LINES if metrics[name]['fan_in']]
    ranks = graph.stationary_ranks()
    history_scores = {row['path']: row['score_exact'] / 100 for row in history.expected()}
    query = ','.join(graph.LINES)
    plain = search(query, None, '--type', 'function', '--limit', '100')
    plain_names = [row['name'] for row in plain['symbols']]
    record('graph-source-population', sorted(graph.LINES), sorted(plain_names))
    for preset in ('central', 'risky', 'proven', 'hotspots'):
        doc = search(query, preset, '--type', 'function', '--limit', '100')
        scores, blended = {}, {}
        for name in graph.LINES:
            hot = history_scores[graph.identity(name)[0]]
            if preset == 'central':
                score = rounded(float(percentile([ranks[n] for n in referenced], ranks[name])), 1) / 100 if name in referenced else 0
            elif preset == 'risky':
                score = hot * rounded(percentile([metrics[n]['dependents'] for n in referenced], metrics[name]['dependents']), 1) / 100 if name in referenced else 0
            elif preset == 'proven':
                # Both authored graph files exceed ten lines, all births are
                # mature, methods have no judged class lineage.
                substance = .5 if name.startswith('test') else 1
                score = (1 - hot + 1 + int(name in referenced)) / 3 * substance
            else:
                score = hot
            scores[name] = score
            position = plain_names.index(name)
            blended[name] = .9 * score + .1 / (1 + position / 20)
        rows = {row['name']: row['rank'] for row in doc['symbols']}
        record('formula:' + preset, {n: rounded(s) for n, s in scores.items()},
               {n: row.get('score') for n, row in rows.items()})
        # Query terms are all exact names: no fuzzy/subtier demotion applies.
        record('blend:' + preset, {n: rounded(s) for n, s in blended.items()},
               {n: row.get('blended') for n, row in rows.items()})
        record('order:' + preset, sorted(plain_names, key=lambda n: -blended[n]),
               [r['name'] for r in doc['symbols']])
        for cap in (0, 1, 5):
            page = search(query, preset, '--type', 'function', '--limit', cap)
            record(f'prefix:{preset}:{cap}', ([r['name'] for r in doc['symbols']][:cap], len(graph.LINES)),
                   ([r['name'] for r in page['symbols']], page['pagination']['symbols']['total']))
        # File dossiers use their own path relevance and strongest symbol,
        # rather than copying the score of a class with no incoming edges.
        files = search('Probe', preset, '--limit', '100')['files']
        for path, name in (('Probe.java', 'leaf'), ('tests/ProbeTest.java', 'testLeaf')):
            row = next(r for r in files if r['path'] == path)['rank']
            record(f'file:{preset}:{path}', rounded(scores[name]), row.get('score'))
            if preset != 'hotspots':
                record(f'strongest:{preset}:{path}', name, row.get('graph', {}).get('strongest_symbol', {}).get('name'))

    # Independently authored old/recent families; enough direct subclasses
    # activate lineage. A descendant must inherit its weakest base's penalty.
    old = {'lineage/OldBase.java': 'class OldBase {}\n' + padding,
           'lineage/LiveBase.java': 'class LiveBase {}\n' + padding}
    old.update({f'lineage/OldChild{i}.java': f'class OldChild{i} extends OldBase {{ int value; }}\n' + padding for i in range(6)})
    history.commit(old)
    recent = {f'lineage/LiveChild{i}.java': f'class LiveChild{i} extends LiveBase {{ int value; }}\n' + padding for i in range(6)}
    recent['lineage/Descendant.java'] = 'class Descendant extends OldChild0 { int value; }\n' + padding
    history.commit(recent, timestamp=int(time.time()) - 86400 * 10)
    runner.command('rebuild', '--force')
    runner.command('hotspots', '--collect', '--full')
    runner.command('graph', 'build')
    for name, base_name, recent_count in (('OldChild0', 'OldBase', 0), ('LiveChild0', 'LiveBase', 6), ('Descendant', 'OldBase', 0)):
        row = next(r for r in search(name, 'proven', '--limit', '100')['symbols'] if r['name'] == name)['rank']
        lineage = row.get('proven', {}).get('lineage', {})
        record('lineage:' + name, (base_name, 6, recent_count, float(bool(recent_count))),
               tuple(lineage.get(k) for k in ('base', 'subclasses', 'recent', 'vitality')))
        factor = next((r['value'] for r in row['components'] if r['name'] == 'lineage'), None)
        record('lineage-factor:' + name, 1.0 if recent_count else .5, factor)
    return expected, actual


def budget(binary, base):
    """Check the documented relevance-head pool, not global ranking optimality."""
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('rank artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='rank-budget-', dir=base)).resolve())
    # The fixed 2000-file pool boundary needs larger dossiers than ordinary
    # fixtures. Retain a hard, small memory ceiling and private stdout logs.
    runner.output_budget = 8 * 1024 * 1024
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    # Compact repeated synthetic data stays private; no per-row test/code file.
    (runner.root / 'Crowd.java').write_text('class Crowd {\n' + ''.join(
        f' int probe{i:03d}() {{ return 0; }}\n' for i in range(110)) +
        ' int entry() {\n  return probe109();\n }\n}\n')
    (runner.root / 'tests').mkdir()
    (runner.root / 'tests/Tests.java').write_text('class Tests { int probeTest() { return 0; } }\n')
    for i in range(2005):
        (runner.root / f'probe{i:04d}.java').write_text(f'class File{i:04d} {{}}\n')
    (runner.root / 'probe2004.java').write_text('class File2004 {\n int leaf() { return 0; }\n'
                                               ' int enter() {\n  return leaf();\n }\n}\n')
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.command('graph', 'build')
    expected, actual = {}, {}
    full = runner.json('search', 'probe', '--type', 'function', '--limit', '3000')
    symbols = [r['name'] for r in full['symbols'] if not r['path'].startswith('tests/')]
    files = [f'probe{i:04d}.java' for i in range(2005)]
    expected['source-population'] = (sorted([f'probe{i:03d}' for i in range(110)] + ['probeTest']), files)
    actual['source-population'] = (sorted(r['name'] for r in full['symbols']), full['files'])
    for cap in (0, 1, 99, 100, 110, 2001, 2005):
        doc = runner.json('search', 'probe', '--rank', 'central', '--type', 'function', '--exclude-tests', '--limit', cap)
        symbol_pool = symbols[:max(100, cap + 1)]
        # A higher-scoring tail candidate leads only once it enters the
        # relevance-head pool. Changing --limit is allowed to widen that pool.
        if 'probe109' in symbol_pool:
            symbol_pool = ['probe109'] + [n for n in symbol_pool if n != 'probe109']
        file_pool = files[:max(2000, cap + 1)]
        if 'probe2004.java' in file_pool:
            file_pool = ['probe2004.java'] + [p for p in file_pool if p != 'probe2004.java']
        expected[str(cap)] = {'symbols': symbol_pool[:cap], 'files': file_pool[:cap],
                              'symbol-total': 110, 'file-total': 2005,
                              'symbol-pool': min(110, max(100, cap + 1)),
                              'file-pool': min(2005, max(2000, cap + 1))}
        actual[str(cap)] = {'symbols': [r['name'] for r in doc['symbols']], 'files': [r['path'] for r in doc['files']],
                            'symbol-total': doc['pagination']['symbols']['total'],
                            'file-total': doc['pagination']['files']['total'],
                            'symbol-pool': doc['rank']['pool']['symbols'], 'file-pool': doc['rank']['pool']['files']}
    return expected, actual
