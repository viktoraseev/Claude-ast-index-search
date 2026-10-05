"""Bounded module navigation on disposable Maven/Java source, not MCP truth.

These contracts supplement the target's source graph checks. A target with no
reactor edges still needs real production budget and rendering checks; it is
never declared inapplicable based on its Java-only navigation population.
"""
import json
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
import mobile_contracts
import module_contracts


FEATURES = {'module-route:budgets', 'module-route:rendering'}
REASON = ('independent source/state: disposable Maven/Java graph, path/depth/kind caps, '
          'timeouts and rendered route identities; not MCP equivalence')
# Equal-length branches keep cap selection unambiguous. Include a cycle,
# a real self-edge, and a disconnected Java module.
EDGES = {('a', 'b', 'compile'), ('a', 'c', 'compile'), ('a', 'z', 'compile'),
         ('b', 'z', 'compile'), ('c', 'z', 'compile'), ('z', 'a', 'compile'),
         ('s', 's', 'compile')}
NAMES = {'a', 'b', 'c', 'z', 's', 'isolated'}


def plan_routes(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-graph'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def expected_route(start, end, all_mode, cap, depth, timeout, kind):
    """Use independently enumerated source edges, with explicit cap semantics."""
    result = {'from': start, 'to': end, 'paths': [], 'count': 0, 'truncated': False,
              'truncation_reason': None, 'empty_reason': None}
    if kind not in {'all', 'api', 'implementation'}:
        result['empty_reason'] = 'invalid_args'
    elif start not in NAMES or end not in NAMES:
        result['empty_reason'] = 'missing_module_from' if start not in NAMES else 'missing_module_to'
    elif start == end and not module_contracts.paths(EDGES, start, end, 1, kind):
        result['empty_reason'] = 'self'
    elif all_mode and cap == 0:
        result.update(truncated=True, truncation_reason='max_paths', empty_reason='truncated_max_paths')
    elif timeout == 0:
        reason = 'prune_timeout' if all_mode and start != end else 'timeout'
        result.update(truncated=True, truncation_reason=reason, empty_reason='truncated_' + reason)
    else:
        paths = module_contracts.paths(EDGES, start, end, depth, kind)
        selected = paths[:cap] if all_mode else paths[:1]
        result['paths'] = [{'length': len(p), 'hops': [{'from': a, 'to': b, 'kind': k} for a, b, k in p]}
                           for p in selected]
        result['count'] = len(selected)
        if all_mode and len(paths) > cap:
            result.update(truncated=True, truncation_reason='max_paths')
        if not selected:
            result['empty_reason'] = ('kind_filter' if kind != 'all' and
                                      module_contracts.paths(EDGES, start, end, depth, 'all') else 'unreachable')
    return result


def envelope(output):
    # Missing booleans/counts do not silently become valid defaults. Optional
    # reasons alone may be omitted, as the production JSON contract specifies.
    return {key: output.get(key) for key in ('from', 'to', 'paths', 'count', 'truncated',
                                           'truncation_reason', 'empty_reason')}


def diagram(result, format):
    """Exact public fixture identities, including partial-result disclosure."""
    mermaid = format == 'mermaid'
    lines = ['```mermaid', 'flowchart LR'] if mermaid else ['digraph module_route {', '  rankdir=LR;']
    comment = '%%' if mermaid else '//'
    if result['truncated']:
        lines.append(f"  {comment} Truncated: {result['truncation_reason']}")
    if not result['paths']:
        lines.append(f"  {comment} No path: {result['empty_reason']}")
    else:
        edges = list(dict.fromkeys((hop['from'], hop['to'], hop['kind'])
                                  for path in result['paths'] for hop in path['hops']))
        nodes = list(dict.fromkeys(n for a, b, _ in edges for n in (a, b)))
        ids = {name: f'n{i}' for i, name in enumerate(nodes)}
        lines += ([f'  {ids[n]}[{n}]' for n in nodes] if mermaid else
                  [f'  "{n}";' for n in sorted(nodes)])
        lines += ([f'  {ids[a]} --> {ids[b]}' if k == 'implementation' else
                   f'  {ids[a]} -->|{k}| {ids[b]}' for a, b, k in edges] if mermaid else
                  [f'  "{a}" -> "{b}" [label="{k}"];' for a, b, k in edges])
    return '\n'.join([*lines, '```' if mermaid else '}']) + '\n'


def text_observation(text, result):
    # Text stats contain wall time, so compare semantic identities and the
    # required disclosure, rather than machine-dependent timing strings.
    import re
    reason = result['empty_reason']
    if result['paths']:
        disclosure = f"truncated: {result['truncation_reason']}" in text if result['truncated'] else 'truncated:' not in text
    elif reason == 'truncated_max_paths':
        disclosure = 'Hit max-paths limit' in text
    elif result['truncated']:
        disclosure = 'timed out before finding any paths' in text
    elif reason == 'unreachable':
        disclosure = text.strip() == f"No dependency path from '{result['from']}' to '{result['to']}'."
    elif reason == 'self':
        disclosure = text.strip() == f"'{result['from']}' depends on itself (trivial path)."
    elif reason in {'missing_module_from', 'missing_module_to'}:
        name = result['from'] if reason == 'missing_module_from' else result['to']
        disclosure = text.strip() == f"Module '{name}' not found in index."
    else:
        disclosure = text.strip() == f"No path found (reason: {reason})."
    return {'hops': re.findall(r'^    (.+?) → (.+?) \[([^]]+)\]$', text, re.MULTILINE),
            'lengths': [int(n) for n in re.findall(r'^  Path \d+ \((\d+) hops?\):$', text, re.MULTILINE)],
            'reason': disclosure,
            'count': int(m[1]) if (m := re.search(r'\((\d+) paths?, shortest = ', text)) else 0,
            'ansi': '\x1b' in text}


def exercise(binary, base, feature):
    if feature not in FEATURES:
        raise ToolError('unknown route contract')
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('route artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='routes-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    subprocess.run(['git', 'init', '--template=', str(runner.root)], capture_output=True, check=True,
                   env=runner.environment, timeout=15)
    # Maven module names come from directory ownership; no native DB rows are
    # inserted or read to establish expected identities.
    for name in sorted(NAMES):
        module = runner.root / name
        module.mkdir()
        dependencies = ''.join(f'<dependency><groupId>fixture</groupId><artifactId>{b}</artifactId></dependency>'
                               for a, b, _ in sorted(EDGES) if a == name)
        (module / 'pom.xml').write_text(f'<project><groupId>fixture</groupId><artifactId>{name}</artifactId>'
                                      f'<dependencies>{dependencies}</dependencies></project>')
        (module / 'Probe.java').write_text(f'package fixture.{name}; public class Probe {{}}\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        modules, edges, _ = module_contracts.graph(state, runner.root)
        if set(modules) != NAMES or edges != EDGES:
            raise ToolError('disposable source graph differs from fixture declarations')
    finally:
        state.close()
    runner.command('rebuild', '--force')
    scenarios = []
    if feature == 'module-route:budgets':
        for start, end in [('a', 'z'), ('s', 's')]:
            for all_mode in (False, True):
                for cap in (0, 1, 2, 3, 4):
                    scenarios.append((start, end, all_mode, cap, 20, 5000, 'all'))
                for depth in (0, 1, 2):
                    scenarios.append((start, end, all_mode, 4, depth, 5000, 'all'))
                scenarios.append((start, end, all_mode, 4, 20, 0, 'all'))
        for start, end in [('a', 'isolated'), ('isolated', 'isolated'), ('missing', 'z'), ('a', 'missing')]:
            for all_mode in (False, True):
                scenarios.append((start, end, all_mode, 4, 20, 5000, 'all'))
        for kind in ('api', 'implementation', 'invalid'):
            for all_mode in (False, True):
                scenarios.append(('a', 'z', all_mode, 4, 20, 5000, kind))
    else:
        scenarios = [('a', 'z', True, cap, 20, 5000, 'all') for cap in (0, 1, 3, 4)]
        scenarios += [('a', 'z', mode, 4, 20, 0, 'all') for mode in (False, True)]
        scenarios += [('s', 's', True, 0, 20, 5000, 'all'),
                      ('s', 's', True, 1, 20, 5000, 'all'),
                      ('s', 's', False, 4, 0, 5000, 'all'),
                      ('isolated', 'isolated', True, 4, 20, 5000, 'all'),
                      ('a', 'isolated', True, 4, 20, 5000, 'all'),
                      ('a', 'missing', True, 4, 20, 5000, 'all'),
                      ('missing', 'z', False, 4, 20, 5000, 'all'),
                      ('a', 'z', True, 4, 20, 5000, 'api')]
    expected, actual = {}, {}
    for scenario in scenarios:
        start, end, all_mode, cap, depth, timeout, kind = scenario
        key = json.dumps(scenario)
        result = expected_route(*scenario)
        args = ['module-route', '--from', start, '--to', end, '--max-paths', cap,
                '--max-depth', depth, '--timeout-ms', timeout, '--via-kind', kind]
        if all_mode:
            args.append('--all')
        if feature == 'module-route:budgets':
            output = runner.json(*args)
            expected[key], actual[key] = result, envelope(output)
            # Verify meaningful progress constraints without requiring a
            # particular machine's elapsed time or positive-time scheduling.
            stats = output.get('search_stats')
            required = result['truncated'] or (all_mode and start != end and result['empty_reason'] not in
                                               {'missing_module_from', 'missing_module_to', 'invalid_args'})
            expected[key + ':stats'] = True
            actual[key + ':stats'] = (isinstance(stats, dict) and
                all(isinstance(stats.get(k), int) and not isinstance(stats[k], bool) and stats[k] >= 0
                    for k in ('nodes_visited', 'edges_explored', 'elapsed_ms', 'max_depth_reached', 'timeout_ms')) and
                stats['timeout_ms'] == timeout and stats['max_depth_reached'] <= depth and
                (isinstance(stats.get('suggested_timeout_ms'), int) and stats['suggested_timeout_ms'] > timeout
                 if timeout == 0 and result['truncated'] else stats.get('suggested_timeout_ms') is None)
                if required else stats is None or isinstance(stats, dict))
        else:
            for format in ('mermaid', 'dot', 'text'):
                _, output = runner.command('--format', format, *args)
                if format != 'text':
                    expected[key + ':' + format], actual[key + ':' + format] = diagram(result, format), output
                else:
                    expected[key + ':text'] = {'hops': [(h['from'], h['to'], h['kind']) for p in result['paths'] for h in p['hops']],
                                              'lengths': [p['length'] for p in result['paths']], 'count': result['count'],
                                              'reason': True, 'ansi': False}
                    actual[key + ':text'] = text_observation(output, result)
    return expected, actual
