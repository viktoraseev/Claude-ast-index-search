"""Java catch/resource bindings through graph and explore; independent source truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_receiver_contracts import call_edges, identity

GRAPH = 'graph:java-exception-resource-scopes'
EXPLORE = 'explore:java-exception-resource-scopes'
FEATURES = {GRAPH, EXPLORE}
REASON = ('independent source/state: disposable javac-validated Java catch parameters, '
          'typed/inferred resources, shadowing, catch/finally boundaries, graph pages/reverse '
          'edges and explore callers; multi-catch unresolved-type guards; not MCP equivalence '
          'or compiler-wide dispatch')
SOURCES = {
    'Failure.java': '''package fixture;
class Failure extends Exception {
 int marker() { return 1; }
}
''',
    'Resource.java': '''package fixture;
class Resource implements AutoCloseable {
 int marker() { return 2; }
 Resource() {}
 Resource(int value) {}
 public void close() {}
}
''',
    'Decoy.java': '''package fixture;
class Decoy {
 int marker() { return 3; }
 public String toString() { return "decoy"; }
}
''',
    'CatchProbe.java': '''package fixture;
class CatchProbe {
 Decoy slot = new Decoy();
 int single() {
  try { throw new Failure(); } catch (final Failure slot) { return slot.marker(); }
 }
 int qualified() {
  try { throw new Failure(); } catch (fixture.Failure slot) { return slot.marker(); }
 }
 void capture() {
  try { throw new Failure(); } catch (Failure slot) { Runnable task = () -> slot.marker(); task.run(); }
 }
 void boundary() {
  try { throw new Failure(); } catch (Failure slot) { slot.marker(); }
  finally { slot.marker(); }
  slot.marker();
 }
 String multiple() {
  try { throw new IllegalArgumentException(); }
  catch (IllegalArgumentException | IllegalStateException slot) { return slot.toString(); }
 }
 String external() {
  try { throw new IllegalArgumentException(); }
  catch (IllegalArgumentException slot) { return slot.toString(); }
 }
 int outside() { return slot.marker(); }
}
''',
    'ResourceProbe.java': '''package fixture;
class ResourceProbe {
 Decoy slot = new Decoy();
 void typed() {
  try (Resource slot = new Resource()) { slot.marker(); }
  finally { slot.marker(); }
 }
 void inferred() {
  try (var slot = new Resource()) { slot.marker(); }
 }
 void sequential() {
  try (Resource first = new Resource(); var slot = first) { slot.marker(); }
 }
 void initializers() {
  try (Resource first = new Resource();
       Resource slot = new Resource(first.marker())) {}
 }
 void caught() {
  try (Resource slot = new Resource()) { slot.marker(); throw new Failure(); }
  catch (Failure failure) { slot.marker(); }
 }
 void existing(Resource slot) {
  try (slot) { slot.marker(); }
 }
 void external() {
  try (java.io.StringReader slot = new java.io.StringReader("")) { slot.toString(); }
 }
 int outside() { return slot.marker(); }
}
''',
}
TARGETS = {'failure': ('Failure.java', 3, 'marker'),
           'resource': ('Resource.java', 3, 'marker'),
           'decoy': ('Decoy.java', 3, 'marker')}
CALLS = {
    'CatchProbe.single': ['failure'], 'CatchProbe.qualified': ['failure'],
    'CatchProbe.capture': ['failure'], 'CatchProbe.boundary': ['failure', 'decoy'],
    'CatchProbe.multiple': [], 'CatchProbe.external': [], 'CatchProbe.outside': ['decoy'],
    'ResourceProbe.typed': ['resource', 'decoy'], 'ResourceProbe.inferred': ['resource'],
    'ResourceProbe.sequential': ['resource'], 'ResourceProbe.initializers': ['resource'],
    'ResourceProbe.caught': ['resource', 'decoy'], 'ResourceProbe.external': [],
    'ResourceProbe.existing': ['resource'], 'ResourceProbe.outside': ['decoy'],
}
CREATIONS = {f'ResourceProbe.{name}': [('Resource.java', 4, 'Resource')]
             for name in ('typed', 'inferred', 'sequential', 'caught')}
CREATIONS['ResourceProbe.initializers'] = [('Resource.java', 4, 'Resource'), ('Resource.java', 5, 'Resource')]


def plan_scopes(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-exception-resource-scopes'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def source_identity(seed):
    owner, method = seed.split('.')
    path = owner + '.java'
    lines = [i for i, line in enumerate(SOURCES[path].splitlines(), 1) if f' {method}(' in line]
    if len(lines) != 1:
        raise ToolError('authored Java scope seed is not unique')
    return path, lines[0], method


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('exception scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-exception-scope-', dir=base)).resolve())
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for path, source in SOURCES.items():
        (runner.root / path).write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        if inventory != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('exception scope fixture inventory incomplete')
    finally:
        state.close()
    # Compilation checks every positive and conservative guard source. Diagnostics stay private.
    with (runner.directory / 'javac.stdout.log').open('wb') as stdout, \
            (runner.directory / 'javac.stderr.log').open('wb') as stderr:
        result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                 *[str(runner.root / path) for path in SOURCES]],
                                stdout=stdout, stderr=stderr, timeout=30)
    if result.returncode:
        raise ToolError('authored Java exception/resource scope fixture does not compile; see private logs')
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for feature in FEATURES:
        record(feature, 'full-inventory', {'.java': len(SOURCES), '.kt': 1, '.xml': 1}, inventory)
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for seed, bindings in CALLS.items():
        targets = sorted((target, 'scoped') for target in
                         [*[TARGETS[b] for b in bindings], *CREATIONS.get(seed, [])])
        for limit in (0, 1, 100):
            doc = runner.json('graph', 'dependencies', 'fixture.' + seed, '--limit', limit,
                              '--include-ambiguous')
            # Filter only edge *kinds*, keeping every callable name, identity and duplicate.
            observed = call_edges(doc)
            # Type references can share a page with calls; check complete calls at a full page.
            record(GRAPH, seed + f':{limit}:selection', [source_identity(seed)],
                   [identity(row) for row in doc.get('matched', [])])
            if limit == 100:
                record(GRAPH, seed + ':calls', targets, observed)
            else:
                record(GRAPH, seed + f':{limit}:bounded',
                       {'bounded': True, 'valid': True},
                       {'bounded': len(doc['items']) <= limit,
                        'valid': all(edge in targets for edge in observed)})
    for kind, target in TARGETS.items():
        owner = target[0].removesuffix('.java')
        wanted = sorted(source_identity(seed) for seed, bindings in CALLS.items() if kind in bindings)
        doc = runner.json('graph', 'dependents', f'fixture.{owner}.marker', '--limit', 100)
        call_edges(doc)
        record(GRAPH, kind + ':reverse', wanted, sorted(identity(row['other']) for row in doc['items']))
    # Each family fits the CLI's fixed ten-neighbour cap. A larger union would
    # require a ranking contract to decide which otherwise-valid callers fit.
    for owner in ('CatchProbe', 'ResourceProbe'):
        directory = runner.directory / ('explore-' + owner)
        directory.mkdir()
        isolated = Runner(binary, directory)
        isolated.root.mkdir()
        (isolated.root / '.git').mkdir()
        isolated.environment['AST_INDEX_ROOT'] = str(isolated.root)
        for path, source in SOURCES.items():
            if not path.endswith('Probe.java') or path == owner + '.java':
                (isolated.root / path).write_text(source)
        isolated.command('rebuild', '--force')
        isolated.json('graph', 'build')
        doc = isolated.json('explore', 'marker', '--rwr', '--max-files', 100)
        callers = [(row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller']
        want = sorted((p, line, 'fixture.' + seed) for seed, bindings in CALLS.items()
                      if bindings and seed.startswith(owner + '.')
                      for p, line, name in [source_identity(seed)])
        record(EXPLORE, owner + ':callers', want, sorted(callers))
    return expected, actual
