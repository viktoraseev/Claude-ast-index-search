"""Java graph root selection, from authored sources, never native DB/MCP truth.

Root selection induces traversal before counting or paging. Per-node metrics
remain those of the full stored graph; directory scope and semantic dispatch
are deliberately separate, unresolved contracts.
"""
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'global:scope:java-graph-roots'}
REASON = ('independent source/state: disposable javac-validated Java graph seeds, '
          'root-induced direct/reverse/path/cycle traversal, metrics/top selection, '
          'counts, pages and text root identities; not MCP equivalence or directory scope')

SOURCES = {
    'project/src/Probe.java': '''package primary;
public class Probe {
    public static void leaf() {}
    public static void entry() { leaf(); }
    public static void detour() { attached.Bridge.go(); }
    public static void cross() { attached.Probe.cross(); }
    public static void first() { second(); }
    public static void second() { first(); }
}
''',
    'attached/src/Probe.java': '''package attached;
public class Probe {
    public static void leaf() {}
    public static void entry() { leaf(); }
    public static void cross() { primary.Probe.cross(); }
    public static void first() { second(); }
    public static void second() { first(); }
}
''',
    'attached/src/Bridge.java': '''package attached;
public class Bridge {
    public static void go() { primary.Probe.leaf(); }
}
''',
}


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-graph-root-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=REPLACE(reason,'analysis/graph/explore scope','analysis/explore scope and graph directory selector intersections') || ? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      ('; separate Java graph root fixture covers seed selection, induced traversal and pages; '
                       'graph directory selector intersections remain pending',))
        state.execute("UPDATE coverage SET reason=REPLACE(REPLACE(reason,'attached-root traversal','compiler-wide attached-root resolution'),'ambiguous/attached-root rendering','ambiguous rendering') || ? WHERE feature='graph' AND status='pending'",
                      ('; separate Java graph root fixture covers induced traversal and root rendering; '
                       'compiler-wide attached-root resolution, directory scope and ambiguity budgets remain pending',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('graph root fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='graph-roots-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for path, source in SOURCES.items():
        file = directory / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    inventory_state = connect(directory / 'inventory.sqlite')
    try:
        inventory_state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory_state, directory)
        inventory = dict(inventory_state.execute('SELECT extension,count(*) FROM file_inventory WHERE extension IN (\'.java\',\'.kt\') GROUP BY extension'))
        if inventory != {'.java': 3, '.kt': 1}:
            raise ToolError('graph root full inventory incomplete')
    finally:
        inventory_state.close()
    # Compiler acceptance validates cross-root calls independently of indexing.
    (directory / 'classes').mkdir()
    with (directory / 'javac.log').open('wb') as log:
        compiled = subprocess.run(['javac', '-d', str(directory / 'classes'),
                                   *[str(directory / p) for p in SOURCES]],
                                  stdout=log, stderr=log, timeout=30)
    if compiled.returncode:
        raise ToolError('graph root Java fixture rejected; see private javac log')
    runner.command('rebuild', '--force')
    runner.command('subtree', 'add', 'attached', directory / 'attached')
    runner.command('rebuild', '--force')
    runner.command('graph', 'build')
    expected, actual = {}, {}

    def record(label, want, got):
        expected[label], actual[label] = want, got

    def identity(row):
        return runner.path(row['path']), row['name'], row['line']

    def declaration(path, name):
        lines = SOURCES[path].splitlines()
        line = next(i for i, text in enumerate(lines, 1)
                    if ('void ' + name + '(' if name != 'Probe' and name != 'Bridge' else 'class ' + name) in text)
        return path, name, line

    def page(report, rows, cap, field='symbol'):
        observed = [identity(row[field]) for row in report.get('items', [])]
        return ({'total': len(rows), 'returned': min(cap, len(rows)), 'limit': cap,
                 'truncated': len(rows) > cap, 'valid': True, 'complete': True},
                {**report.get('pagination', {}),
                 'valid': len(set(observed)) == len(observed) and set(observed) <= set(rows),
                 'complete': cap < len(rows) or sorted(observed) == sorted(rows)})

    for label, flags, paths in (
            ('all', [], list(SOURCES)),
            ('local', ['--local'], ['project/src/Probe.java']),
            ('attached', ['--subtree', 'attached'], ['attached/src/Probe.java', 'attached/src/Bridge.java'])):
        selected = set(paths)
        seed_rows = [declaration(p, 'leaf') for p in paths if p.endswith('/Probe.java')]
        for command in ('dependencies', 'dependents'):
            seed = 'entry' if command == 'dependencies' else 'leaf'
            rows = [declaration(p, 'leaf' if command == 'dependencies' else 'entry')
                    for p in paths if p.endswith('/Probe.java')]
            if command == 'dependents' and label == 'all':
                rows.append(declaration('attached/src/Bridge.java', 'go'))
            for cap in (0, 1, 100):
                report = runner.json(*flags, 'graph', command, seed, '--limit', cap)
                want, got = page(report, rows, cap, 'other')
                record(f'{label}:{command}:{cap}',
                       {'page': want, 'resolved': len(rows), 'ambiguous': 0,
                        'matched': sorted(declaration(p, seed) for p in paths if p.endswith('/Probe.java'))},
                       {'page': got, 'resolved': report.get('resolved_edges'), 'ambiguous': report.get('ambiguous_edges'),
                        'matched': sorted(identity(r) for r in report.get('matched', []))})
        rows = [declaration(p, 'entry') for p in paths if p.endswith('/Probe.java')]
        if label == 'all':
            rows += [declaration('attached/src/Bridge.java', 'go'), declaration('project/src/Probe.java', 'detour')]
        for cap in (0, 1, 100):
            report = runner.json(*flags, 'graph', 'impact', 'leaf', '--depth', 3, '--limit', cap)
            want, got = page(report, rows, cap)
            record(f'{label}:impact:{cap}',
                   {'page': want, 'total_symbols': len(rows), 'total_files': 3 if label == 'all' else 1,
                    'levels': [{'depth': 1, 'symbols': 3 if label == 'all' else 1, 'files': 3 if label == 'all' else 1}] +
                              ([{'depth': 2, 'symbols': 1, 'files': 1}] if label == 'all' else [])},
                   {'page': got, **{k: report.get(k) for k in ('total_symbols', 'total_files', 'levels')}})
        # A scoped traversal must never leave and re-enter the selected root.
        if label != 'attached':
            report = runner.json(*flags, 'graph', 'path', 'primary.Probe#detour', 'primary.Probe#leaf')
            hops = [declaration('project/src/Probe.java', 'detour'), declaration('attached/src/Bridge.java', 'go'),
                    declaration('project/src/Probe.java', 'leaf')]
            record(f'{label}:detour-path',
                   {'length': 2 if label == 'all' else None, 'shortest_paths': int(label == 'all'),
                    'paths': [hops] if label == 'all' else []},
                   {**{k: report.get(k) for k in ('length', 'shortest_paths')},
                    'paths': [[identity(h['symbol']) for h in path] for path in report.get('items', [])]})
        for cap in (0, 1, 100):
            report = runner.json(*flags, 'graph', 'cycles', '--limit', cap)
            cycles = [{declaration('project/src/Probe.java', n) for n in ('first', 'second')},
                      {declaration('attached/src/Probe.java', n) for n in ('first', 'second')}]
            if label == 'all':
                cycles.append({declaration('project/src/Probe.java', 'cross'), declaration('attached/src/Probe.java', 'cross')})
            elif label == 'local':
                cycles = cycles[:1]
            else:
                cycles = cycles[1:]
            items = report.get('items', [])
            record(f'{label}:cycles:{cap}',
                   {'total': len(cycles), 'returned': min(cap, len(cycles)), 'valid': True, 'complete': True},
                   {'total': report.get('components'), 'returned': len(items),
                    'valid': all({identity(r) for r in item['members']} in cycles and item['size'] == 2
                                 and item['files'] == (2 if len({identity(r)[0] for r in item['members']}) == 2 else 1)
                                 for item in items),
                    'complete': cap < len(cycles) or len({frozenset(identity(r) for r in item['members']) for item in items}) == len(cycles)})
            report = runner.json(*flags, 'graph', 'metrics', 'leaf', '--limit', cap)
            record(f'{label}:metrics:{cap}', *page(report, seed_rows, cap))
            record(f'{label}:metrics:values:{cap}', True,
                   all(r.get('fan_in') == (2 if identity(r['symbol'])[0].startswith('project/') else 1)
                       and r.get('fan_out') == 0 for r in report.get('items', [])))
            functions = [declaration(p, n) for p, source in SOURCES.items() if p in selected
                         for n in re.findall(r'void (\w+)\(', source)]
            report = runner.json(*flags, 'graph', 'top', '--kind', 'function', '--limit', cap)
            record(f'{label}:top-functions:{cap}', *page(report, functions, cap))
            declarations = functions + [declaration(p, 'Bridge' if p.endswith('Bridge.java') else 'Probe') for p in paths]
            report = runner.json(*flags, 'graph', 'top', '--limit', cap)
            record(f'{label}:top:{cap}', *page(report, declarations, cap))
        outside = 'attached' if label == 'local' else 'primary' if label == 'attached' else None
        if outside:
            for command in ('dependencies', 'dependents', 'impact', 'metrics'):
                report = runner.json(*flags, 'graph', command, outside + '.Probe#leaf')
                record(f'{label}:{command}:outside', True,
                       report.get('error', '').startswith('no symbol matches') if command != 'metrics' else
                       report.get('pagination', {}).get('total') == 0)
            for left, right in ((outside + '.Probe#entry', 'leaf'), ('entry', outside + '.Probe#leaf')):
                report = runner.json(*flags, 'graph', 'path', left, right)
                record(f'{label}:path:outside:{left}:{right}', True,
                       report.get('error', '').startswith('no symbol matches'))
        for path in paths:
            if not path.endswith('/Probe.java'):
                continue
            package = 'primary' if path.startswith('project/') else 'attached'
            for reverse in (False, True):
                a, b = (('leaf', 'entry') if reverse else ('entry', 'leaf'))
                for cap in (0, 1, 100):
                    report = runner.json(*flags, 'graph', 'path', package + '.Probe#' + a,
                                         package + '.Probe#' + b, '--max-paths', cap)
                    rows = [[declaration(path, n) for n in ('entry', 'leaf')]] if cap else []
                    record(f'{label}:path:{package}:{reverse}:{cap}',
                           {'paths': rows, 'length': 1, 'shortest_paths': 1,
                            'direction': 'reverse' if reverse else 'forward',
                            'pagination': {'total': 1, 'returned': min(1, cap), 'limit': cap, 'truncated': cap == 0}},
                           {'paths': [[identity(h['symbol']) for h in p] for p in report.get('items', [])],
                            **{k: report.get(k) for k in ('length', 'shortest_paths', 'direction', 'pagination')}})
        # Text must render only selected source identities, including decorations.
        for command, args in (('dependencies', ['entry']), ('dependents', ['leaf']),
                              ('impact', ['leaf']), ('metrics', ['leaf']), ('top', []), ('cycles', [])):
            _, text = runner.command(*flags, 'graph', command, *args, '--limit', 100)
            forbidden = [str(directory / p) for p in SOURCES if p not in selected]
            record(f'{label}:{command}:text', {'scope': True, 'decoration': True},
                   {'scope': not any(p in text for p in forbidden),
                    'decoration': '[attached]' in text if label == 'attached' else True})
    return ({f: expected for f in FEATURES}, {f: actual for f in FEATURES})
