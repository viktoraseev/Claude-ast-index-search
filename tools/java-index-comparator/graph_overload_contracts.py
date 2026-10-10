"""Execute graph overload edges on authored Java, independently of call-tree output."""
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from java_overload_context_contracts import CASES
from java_callable_site_contracts import SOURCE, BRANCHES, TOP, reverse_declarations

FEATURE = 'graph:java-overload-contexts'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-graph-overload-contexts-v1'
REASON = ('independent source/javac/CLI: Java graph direct/reverse edges, impact and '
          'paths for nominal/generic/callback/reference/constructor overload sites, '
          'same-line declarations in both orders and zero/one/full pages; not MCP equivalence')


def plan_contracts(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('graph overload fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='graph-overloads-', dir=base)))
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    dependencies = runner.directory / 'dependencies'
    dependencies.mkdir()
    for name in ('Alpha', 'Beta'):
        (dependencies / (name + '.java')).write_text(f'package external; public class {name} {{}}')

    def compile_sources(paths, destination, classpath=None):
        with (runner.directory / 'javac.log').open('ab') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(destination),
                                     *(['-cp', str(classpath)] if classpath else []), *map(str, paths)],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored graph overload fixture does not compile; see private log')

    compile_sources([dependencies / (n + '.java') for n in ('Alpha', 'Beta')], dependencies)
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    def identity(row):
        return (row.get('path'), row.get('line'), row.get('name'))

    def setup(source):
        path = runner.root / 'Probe.java'
        path.write_text(source)
        compile_sources([path], runner.directory / 'classes', dependencies)
        runner.command('rebuild', '--force', '--max-files', 0)
        runner.json('graph', 'build')

    def check_seed(key, seed, direct, indirect):
        # Ordering is part of the public edge/impact contract; retain duplicate
        # declarations instead of normalizing them to a set.
        direct = sorted(direct)
        impact = sorted([(1, row) for row in direct] + [(2, row) for row in indirect])
        for cap in (0, 1, 100):
            doc = runner.json('graph', 'dependents', seed, '--kind', 'function', '--limit', cap)
            record(f'{key}:direct:{cap}', {'items': direct[:cap], 'total': len(direct)},
                   {'items': [identity(row.get('other', {})) for row in doc.get('items', [])],
                    'total': doc.get('pagination', {}).get('total')})
            doc = runner.json('graph', 'impact', seed, '--depth', 2, '--limit', cap)
            record(f'{key}:impact:{cap}', {'items': impact[:cap], 'total': len(impact)},
                   {'items': [(row.get('depth'), identity(row.get('symbol', {}))) for row in doc.get('items', [])],
                    'total': doc.get('total_symbols')})

    for name, source, seeds in CASES:
        setup(source)
        for seed, callers in seeds.items():
            direct = [('Probe.java', line, re.search(r'\b(\w+)\s*\([^)]*\)\s*\{', text)[1])
                      for line, text in enumerate(source.splitlines(), 1) if f'return {seed}();' in text]
            indirect = [('Probe.java', line, caller) for caller in callers
                        for line, text in enumerate(source.splitlines(), 1)
                        if re.search(r'\b' + re.escape(caller) + r'\s*\(', text)]
            check_seed(name + ':' + seed, seed, direct, indirect)

    for layout, source in (('forward', SOURCE), ('reverse', reverse_declarations(SOURCE))):
        setup(source)
        for seed, branches in BRANCHES.items():
            direct = [('Probe.java', line, name) for line, name, _ in branches]
            indirect = [('Probe.java', line, name) for owner_line, owner, children in branches
                        for line, name in children if (owner_line, owner) != (line, name)]
            check_seed(layout + ':' + seed, seed, direct, indirect)
            opposite = 'betaSeed' if seed == 'alphaSeed' else 'alphaSeed'
            for member, (top_line, top) in TOP[seed].items():
                for target, distance in ((seed, 3), (opposite, None)):
                    doc = runner.json('graph', 'path', top, target, '--max-depth', 3)
                    # Graph paths include type references and class containment
                    # (advertised by graph_path), unlike call-only trees. A
                    # constructor return type reaches either constructor body.
                    paths = []
                    target_line = 5 if target == 'alphaSeed' else 6
                    if member == 'create':
                        distance = 3
                        paths.append([(top_line, top, 'function'), (22, 'Construct', 'class'),
                                      (23, 'Construct', 'function'), (target_line, target, 'function')])
                    if target == seed:
                        middle_line = {'overloaded': 10, 'qualified': 13, 'varied': 16,
                                       'reference': 19, 'create': 25}[member]
                        bottom = (23, 'Construct', 'function') if member == 'create' else (7, 'route', 'function')
                        paths.append([(top_line, top, 'function'), (middle_line, member, 'function'),
                                      bottom, (target_line, target, 'function')])
                    record(f'{layout}:path:{top}:{target}', {'length': distance, 'paths': sorted(paths)},
                           {'length': doc.get('length'), 'paths': sorted([
                               [(h.get('symbol', {}).get('line'), h.get('symbol', {}).get('name'),
                                 h.get('symbol', {}).get('kind')) for h in path]
                               for path in doc.get('items', [])])})
    # Imported overloaded members must keep their declaring root, even when
    # the primary root has same-name class/method decoys. Scope is induced
    # before reverse traversal, not merely applied to the rendered rows.
    (runner.root / 'Probe.java').unlink()
    attached = runner.directory / 'attached'
    attached.mkdir()
    provider = attached / 'Router.java'
    provider.write_text('''package provider;
public class Router {
 public static class A {} public static class B {}
 public static int alphaSeed() { return 1; }
 public static int betaSeed() { return 2; }
 public static int pick(A a) { return alphaSeed(); } public static int pick(B b) { return betaSeed(); }
}
''')
    client = runner.root / 'Client.java'
    client.write_text('''package app;
import provider.Router;
class Client {
 static int alpha() { return Router.pick(new Router.A()); }
 static int beta(Router.B value) {
  java.util.function.ToIntFunction<Router.B> operation = Router::pick;
  return operation.applyAsInt(value);
 }
}
''')
    (runner.root / 'Router.java').write_text('''package decoy;
class Router {
 static int alphaSeed() { return 3; }
 static int betaSeed() { return 4; }
 static int pick(Object value) { return alphaSeed() + betaSeed(); }
}
''')
    compile_sources([provider], runner.directory / 'provider-classes')
    compile_sources([client], runner.directory / 'client-classes', runner.directory / 'provider-classes')
    runner.json('subtree', 'add', 'library', attached)
    runner.command('rebuild', '--force', '--max-files', 0)
    runner.json('graph', 'build')
    for seed, caller, caller_line in (('alphaSeed', 'alpha', 4), ('betaSeed', 'beta', 5)):
        for flags, indirect in (((), [(2, ('project/Client.java', caller_line, caller))]),
                                (('--subtree', 'library'), [])):
            want = [(1, ('attached/Router.java', 6, 'pick')), *indirect]
            for cap in (0, 1, 100):
                doc = runner.json(*flags, 'graph', 'impact', 'provider.Router#' + seed,
                                  '--depth', 2, '--limit', cap)
                rows = [(r.get('depth'), (runner.path(r.get('symbol', {}).get('path', '')),
                         r.get('symbol', {}).get('line'), r.get('symbol', {}).get('name')))
                        for r in doc.get('items', [])]
                record(f'attached:{seed}:{flags}:{cap}', {'items': want[:cap], 'total': len(want)},
                       {'items': rows, 'total': doc.get('total_symbols')})
        opposite = 'betaSeed' if seed == 'alphaSeed' else 'alphaSeed'
        for target in (seed, opposite):
            # The referenced Router class contains both leaf definitions.
            # Retain that public type path, and require the selected call path
            # only for the matching overload (direct impact above also guards it).
            for depth in (0, 1, 2, 3):
                doc = runner.json('graph', 'path', 'app.Client#' + caller, 'provider.Router#' + target,
                                  '--max-depth', depth)
                start = ('project/Client.java', caller_line, caller, 'function')
                end = ('attached/Router.java', 4 if target == 'alphaSeed' else 5, target, 'function')
                paths = [[start, ('attached/Router.java', 2, 'Router', 'class'), end]]
                if target == seed:
                    paths.append([start, ('attached/Router.java', 6, 'pick', 'function'), end])
                record(f'attached:path:{caller}:{target}:{depth}',
                       {'length': 2 if depth >= 2 else None, 'paths': sorted(paths) if depth >= 2 else []},
                       {'length': doc.get('length'), 'paths': sorted([
                           [(runner.path(h['symbol']['path']), h['symbol']['line'],
                             h['symbol']['name'], h['symbol']['kind']) for h in path]
                           for path in doc.get('items', [])])})
    # Review the complete Java entity mapping of --kind, including non-node
    # annotation/import entries. These are executed selections, not DB truth.
    runner.json('subtree', 'remove', 'library')
    client.unlink()
    (runner.root / 'Router.java').unlink()
    setup('''package fixture;
import java.util.List;
@interface Mark {}
interface Task { void work(); }
enum Mode { READY }
record Value(int component) {}
@Deprecated
class Probe {
 static final int LIMIT = 1;
 int field = LIMIT;
 Probe() {}
 int leaf() { return field; }
}
''')
    # Java field declarations (including static final fields) map to property;
    # enum constants map to constant. Packages supply qualified namespaces,
    # while import/annotation uses are excluded from graph ownership. See the
    # Java query captures and graph is_node_kind, rather than Java/JDK labels.
    kinds = {'package': [], 'interface': [(3, 'Mark'), (4, 'Task')],
             'enum': [(5, 'Mode')], 'class': [(6, 'Value'), (8, 'Probe')],
             'constant': [(5, 'READY')], 'property': [(6, 'component'), (9, 'LIMIT'), (10, 'field')],
             'function': [(4, 'work'), (6, 'component'), (11, 'Probe'), (12, 'leaf')],
             'import': [], 'annotation': [], '__missing_kind__': []}
    names = ['fixture', 'List', 'Mark', 'Task', 'Mode', 'READY', 'Value', 'component',
             '@Deprecated', 'Probe', 'LIMIT', 'field', 'work', 'leaf']
    for kind, entries in kinds.items():
        want = sorted(('Probe.java', line, name, kind) for line, name in entries)
        for command, arguments in (('top', []), ('metrics', names)):
            doc = runner.json('graph', command, *arguments, '--kind', kind, '--limit', 100)
            got = sorted((row.get('symbol', {}).get('path'), row.get('symbol', {}).get('line'),
                          row.get('symbol', {}).get('name'), row.get('symbol', {}).get('kind'))
                         for row in doc.get('items', []))
            # Top ranks stored edge endpoints; isolated definitions have no
            # metric row. Metrics still selects them and returns zero metrics.
            # This source has exactly leaf -> field -> LIMIT, independently of
            # native DB rows or the output of either command.
            top_endpoints = {(9, 'LIMIT'), (10, 'field'), (12, 'leaf')}
            selected = [row for row in want if (row[1], row[2]) in top_endpoints] if command == 'top' else want
            record(f'java-entity-kind:{kind}:{command}', selected, got)
    return expected, actual
