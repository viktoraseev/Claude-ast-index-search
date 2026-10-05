"""Executed Java graph directory intersections; independent source, not MCP truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'global:scope:java-graph-directories'}
REASON = ('independent source/state: disposable javac-validated Java directory/root/file/kind '
          'intersections, induced graph traversal, counts, pages and rendering; '
          'not MCP equivalence or compiler-wide dispatch')
SOURCE = '''package fixture.{package};
public class Probe {{
 public static void leaf() {{}}
 public static void entry() {{ leaf(); }}
 public static void detour() {{ fixture.out.Bridge.go(); }}
 public static void first() {{ second(); }}
 public static void second() {{ first(); }}
 public static void cross() {{ fixture.out.Bridge.cross(); }}
}}
'''
SOURCES = {f'project/{path}/Probe.java': SOURCE.format(package=package)
           for path, package in [('scope_', 'local'), ('scopeX', 'prefix'),
                                 ('scope%', 'percent'), ('ScopeCase', 'upper')]}
SOURCES['attached/scope_/Probe.java'] = SOURCE.format(package='attached')
SOURCES['project/outside/Bridge.java'] = '''package fixture.out;
public class Bridge {
 public static void go() { fixture.local.Probe.leaf(); }
 public static void cross() { fixture.local.Probe.cross(); }
}
'''


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-graph-directory-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=REPLACE(REPLACE(reason, "
                      "'analysis/explore scope and graph directory selector intersections', 'analysis/explore scope'), "
                      "'graph directory selector intersections remain pending', "
                      "'separate Java directory fixture covers selector intersections and induced traversal') "
                      "WHERE feature='global:scope-command-matrix' AND status='pending'")
        state.execute("UPDATE coverage SET reason=REPLACE(REPLACE(reason, "
                      "'resolution, directory scope and ambiguity budgets remain pending', "
                      "'resolution and ambiguity budgets remain pending'), "
                      "'resolution, directory scope and ambiguity budgets remain unresolved', "
                      "'resolution and ambiguity budgets remain unresolved') || ? "
                      "WHERE feature='graph' AND status='pending'",
                      ('; separate Java directory fixture covers selector intersections and induced traversal',))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('graph directory fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='graph-directories-', dir=base) as temporary:
        directory = Path(temporary).resolve()
        runner = Runner(binary, directory)
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        for path, source in SOURCES.items():
            file = directory / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
        (runner.root / 'empty').mkdir()
        with (directory / 'javac.log').open('wb') as log:
            compilation = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'),
                                         *[str(directory / p) for p in SOURCES]],
                                        stdout=log, stderr=log, timeout=30)
        if compilation.returncode:
            raise ToolError('graph directory Java sources rejected; see private javac log')
        runner.command('rebuild', '--force')
        runner.command('subtree', 'add', 'attached', directory / 'attached')
        runner.command('rebuild', '--force')
        runner.command('graph', 'build')
        expected, actual = {}, {}

        def record(key, want, got):
            expected[key], actual[key] = want, got

        def declaration(path, name):
            line = next(i for i, text in enumerate(SOURCES[path].splitlines(), 1)
                        if ('void ' + name + '(' if name not in ('Probe', 'Bridge') else
                            'class ' + name) in text)
            return path, name, line

        def identity(row):
            return runner.path(row['path']), row['name'], row['line']

        def page(doc, rows, cap, field='symbol'):
            observed = [identity(row[field]) for row in doc.get('items', [])]
            return ({'total': len(rows), 'returned': min(cap, len(rows)), 'limit': cap,
                     'truncated': cap < len(rows), 'valid': True, 'complete': True},
                    {**doc.get('pagination', {}),
                     'valid': len(set(observed)) == len(observed) and set(observed) <= set(rows),
                     'complete': cap < len(rows) or sorted(observed) == sorted(rows)})

        local = 'project/scope_/Probe.java'
        attached = 'attached/scope_/Probe.java'
        cases = [('cwd', 'scope_', [], [local, attached]),
                 ('local', 'scope_', ['--local'], [local]),
                 ('attached', 'scope_', ['--subtree', 'attached'], [attached]),
                 ('missing-root', 'scope_', ['--subtree', 'missing'], []),
                 ('literal-percent', 'scope%', [], ['project/scope%/Probe.java']),
                 ('case', 'ScopeCase', [], ['project/ScopeCase/Probe.java']),
                 ('prefix', 'scopeX', [], ['project/scopeX/Probe.java']),
                 ('empty', 'empty', [], [])]
        for label, cwd, flags, selected in cases:
            def query(command, *args):
                return runner.json(*flags, 'graph', command, *args, cwd=runner.root / cwd)

            for cap in (0, 1, 100):
                for command, seed, other in [('dependencies', 'entry', 'leaf'),
                                             ('dependents', 'leaf', 'entry')]:
                    doc = query(command, seed, '--limit', cap)
                    rows = [declaration(p, other) for p in selected]
                    if not selected:
                        record(f'{label}:{command}:{cap}:empty', True,
                               doc.get('error', '').startswith('no symbol matches'))
                        continue
                    record(f'{label}:{command}:{cap}:page', *page(doc, rows, cap, 'other'))
                    record(f'{label}:{command}:{cap}:seeds',
                           sorted(declaration(p, seed) for p in selected),
                           sorted(identity(row) for row in doc.get('matched', [])))
                    record(f'{label}:{command}:{cap}:counts', [len(rows), 0],
                           [doc.get('resolved_edges'), doc.get('ambiguous_edges')])
                doc = query('impact', 'leaf', '--depth', 3, '--limit', cap)
                if selected:
                    record(f'{label}:impact:{cap}',
                           {'page': page(doc, [declaration(p, 'entry') for p in selected], cap)[0],
                            'symbols': len(selected), 'files': len(selected)},
                           {'page': page(doc, [declaration(p, 'entry') for p in selected], cap)[1],
                            'symbols': doc.get('total_symbols'), 'files': doc.get('total_files')})
                else:
                    record(f'{label}:impact:{cap}:empty', True,
                           doc.get('error', '').startswith('no symbol matches'))
                doc = query('metrics', 'leaf', '--limit', cap)
                record(f'{label}:metrics:{cap}', *page(doc, [declaration(p, 'leaf') for p in selected], cap))
                doc = query('top', '--kind', 'function', '--limit', cap)
                rows = [declaration(p, n) for p in selected
                        for n in ('leaf', 'entry', 'detour', 'first', 'second', 'cross')]
                record(f'{label}:top:{cap}', *page(doc, rows, cap))
                doc = query('cycles', '--limit', cap)
                cycles = {frozenset(declaration(p, n) for n in ('first', 'second')) for p in selected}
                observed = [frozenset(identity(r) for r in item['members']) for item in doc.get('items', [])]
                record(f'{label}:cycles:{cap}',
                       {'total': len(cycles), 'returned': min(cap, len(cycles)), 'valid': True, 'complete': True},
                       {'total': doc.get('components'), 'returned': len(observed),
                        'valid': len(set(observed)) == len(observed) and set(observed) <= cycles,
                        'complete': cap < len(cycles) or set(observed) == cycles})
            if selected:
                for command in ('dependencies', 'dependents', 'impact', 'metrics'):
                    for selector in ('outside/', 'probe.java'):
                        doc = query(command, 'leaf', '--in-file', selector)
                        record(f'{label}:{command}:file-intersection:{selector}', True,
                               doc.get('pagination', {}).get('total') == 0 if command == 'metrics' else
                               doc.get('error', '').startswith('no symbol matches'))
                doc = query('dependencies', 'entry', '--kind', 'class')
                record(f'{label}:kind-intersection', True, doc.get('error', '').startswith('no symbol matches'))
                doc = query('dependencies', 'Probe', '--members')
                record(f'{label}:members-induced', [0, 0, 0],
                       [doc.get('pagination', {}).get('total'), doc.get('resolved_edges'), doc.get('ambiguous_edges')])
                for command in ('top', 'cycles'):
                    doc = query(command, '--path', 'outside/')
                    record(f'{label}:{command}:path-intersection', 0, doc.get('pagination', {}).get('total'))
                for p in selected:
                    package = SOURCES[p].split(';')[0].split()[-1]
                    for cap in (0, 1, 100):
                        doc = query('path', package + '.Probe#entry', package + '.Probe#leaf', '--max-paths', cap)
                        record(f'{label}:{p}:path:{cap}',
                               {'length': 1, 'count': 1, 'paths': [[declaration(p, n) for n in ('entry', 'leaf')]] if cap else []},
                               {'length': doc.get('length'), 'count': doc.get('shortest_paths'),
                                'paths': [[identity(h['symbol']) for h in path] for path in doc.get('items', [])]})
                    # Both endpoints are selected; an out-of-directory hop must
                    # not leave and re-enter the directory to connect them.
                    doc = query('path', package + '.Probe#detour', 'fixture.local.Probe#leaf')
                    record(f'{label}:{p}:detour', True,
                           doc.get('error', '').startswith('no symbol matches') if local not in selected else
                           doc.get('length') is None and doc.get('items') == [])
            for command, args in [('dependencies', ['entry']), ('dependents', ['leaf']),
                                  ('impact', ['leaf']), ('metrics', ['leaf']), ('top', []), ('cycles', [])]:
                _, text = runner.command(*flags, 'graph', command, *args, cwd=runner.root / cwd)
                record(f'{label}:{command}:text', True,
                       all(str(directory / p) not in text for p in SOURCES if p not in selected))
        return ({f: expected for f in FEATURES}, {f: actual for f in FEATURES})
