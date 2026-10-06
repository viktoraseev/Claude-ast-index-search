"""Local Java interface visibility; compiler/source truth, never MCP equivalence."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_type_binding_contracts import edges, identity

SCOPES = 'graph:java-local-interface-scopes'
MEMBERS = 'graph:java-local-interface-members'
EXPLORE = 'explore:java-local-interface-scopes'
FEATURES = {SCOPES, MEMBERS, EXPLORE}
REASON = ('independent source/state: disposable javac-validated Java local interfaces, '
          'declaration/block/method boundaries, sibling identities, nested types and static '
          'members, graph pages/reverse/path and exploration; not MCP equivalence or '
          'compiler-wide receiver dispatch')
SOURCES = {
    'Leaf.java': '''package fixture;
interface Leaf {
 class Nested {}
 static int marker() { return 1; }
}
''',
    'Probe.java': '''package fixture;
class Probe {
 Object local() {
  interface Leaf {
   class Nested {}
   static int marker() { return 2; }
  }
  Leaf item = null;
  return new Leaf.Nested();
 }
 Leaf outside(Leaf item) { return item; }
 Object outsideNested() { return new Leaf.Nested(); }
 int outsideCall() { return Leaf.marker(); }
 Object sibling() {
  interface Leaf { class Nested {} }
  return new Leaf.Nested();
 }
 Object block() {
  new Leaf.Nested(); Leaf.marker();
  {
   interface Leaf { class Nested {} static int marker() { return 4; } }
   new Leaf.Nested(); Leaf.marker();
  }
  Leaf.marker(); return new Leaf.Nested();
 }
 int localCall() {
  interface Leaf { static int marker() { return 3; } }
  return Leaf.marker();
 }
}
''',
    'Guards.java': '''package fixture;
class Guards {
 Object parameter(Object Leaf) { return Leaf.toString(); }
 Object variable() { Object Leaf = new Object(); return Leaf.toString(); }
 Object captured() {
  Object Leaf = new Object();
  java.util.function.Supplier<String> task = () -> Leaf.toString();
  return task.get();
 }
 Object lambda() {
  java.util.function.Function<Object, String> task = Leaf -> Leaf.toString();
  return task;
 }
}
''',
    'FieldGuard.java': '''package fixture;
class FieldGuard {
 Object slot = new Object();
 Object field() { return slot.toString(); }
}
''',
}
PACKAGE = ('Leaf.java', 2, 'Leaf')
NESTED = ('Leaf.java', 3, 'Nested')
BINDINGS = {
    'local': [('Probe.java', 4, 'Leaf'), ('Probe.java', 5, 'Nested')],
    'outside': [PACKAGE],
    'outsideNested': [PACKAGE, NESTED],
    'outsideCall': [PACKAGE, ('Leaf.java', 4, 'marker')],
    'sibling': [('Probe.java', 15, 'Leaf'), ('Probe.java', 15, 'Nested')],
    'block': [PACKAGE, NESTED, ('Probe.java', 21, 'Leaf'), ('Probe.java', 21, 'Nested'),
              ('Leaf.java', 4, 'marker'), ('Probe.java', 21, 'marker')],
    'localCall': [('Probe.java', 27, 'Leaf'), ('Probe.java', 27, 'marker')],
}


def source_identity(name):
    lines = [line for line, source in enumerate(SOURCES['Probe.java'].splitlines(), 1)
             if f' {name}(' in source]
    if len(lines) != 1:
        raise ToolError('authored local interface seed is not unique')
    return 'Probe.java', lines[0], name


def plan_interfaces(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-local-interface-scopes'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('local interface fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-local-interfaces-', dir=base)).resolve())
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for name, source in SOURCES.items():
        (runner.root / name).write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        if inventory != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('local interface fixture inventory incomplete')
    finally:
        state.close()
    with (runner.directory / 'javac.stdout.log').open('wb') as stdout, \
            (runner.directory / 'javac.stderr.log').open('wb') as stderr:
        result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                 *[str(runner.root / name) for name in SOURCES]],
                                stdout=stdout, stderr=stderr, timeout=30)
    if result.returncode:
        raise ToolError('authored Java local interface fixture does not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    doc = runner.json('graph', 'dependencies', 'fixture.FieldGuard.field', '--include-ambiguous', '--limit', 100)
    record(SCOPES, 'value-field-dependency', [(('FieldGuard.java', 3, 'slot'), 'local')], edges(doc))
    for name in ('parameter', 'variable', 'captured', 'lambda'):
        line = next(i for i, source in enumerate(SOURCES['Guards.java'].splitlines(), 1)
                    if f' {name}(' in source)
        for ambiguous in (False, True):
            doc = runner.json('graph', 'dependencies', 'fixture.Guards.' + name, '--limit', 100,
                              *(['--include-ambiguous'] if ambiguous else []))
            record(SCOPES, name + f':value-shadow:{ambiguous}',
                   {'matched': [('Guards.java', line, name)], 'edges': []},
                   {'matched': [identity(row) for row in doc['matched']], 'edges': edges(doc)})
    for name, targets in BINDINGS.items():
        feature = SCOPES if name == 'outside' else MEMBERS
        for limit in (0, 1, 100):
            for ambiguous in (False, True):
                doc = runner.json('graph', 'dependencies', 'fixture.Probe.' + name, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                observed = edges(doc)
                wanted = sorted((target, 'local' if target[0] == 'Probe.java' and target[2] == 'marker'
                                 else 'scoped') for target in targets)
                record(feature, f'{name}:{limit}:{ambiguous}',
                       {'matched': [source_identity(name)], 'total': len(wanted),
                        'count': min(limit, len(wanted)), 'valid': True, 'complete': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'total': doc.get('pagination', {}).get('total'), 'count': len(observed),
                        'valid': len(set(observed)) == len(observed) and all(edge in wanted for edge in observed),
                        'complete': limit < len(wanted) or observed == wanted})
    # Exact package identities expose leakage even when broad local-name seeds
    # intentionally select a union of same-named local declarations.
    for qualifier, target in [('fixture.Leaf', PACKAGE), ('fixture.Leaf.Nested', NESTED),
                              ('fixture.Leaf.marker', ('Leaf.java', 4, 'marker'))]:
        wanted = sorted(source_identity(name) for name, targets in BINDINGS.items() if target in targets)
        doc = runner.json('graph', 'dependents', qualifier, '--limit', 100)
        record(SCOPES, qualifier + ':reverse', wanted, sorted(identity(row['other']) for row in doc['items']))
        for name in BINDINGS:
            doc = runner.json('graph', 'path', 'fixture.Probe.' + name, qualifier, '--max-depth', 1)
            # Graph seeds include qualified descendants. Build that union
            # from authored declarations, never from native seed/output rows.
            selected = {PACKAGE, NESTED, ('Leaf.java', 4, 'marker')} if qualifier == 'fixture.Leaf' else {target}
            record(SCOPES, name + ':path:' + qualifier,
                   sorted((source_identity(name), end) for end in BINDINGS[name] if end in selected),
                   sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']))
    doc = runner.json('explore', 'Leaf', '--rwr', '--max-files', 100)
    record(EXPLORE, 'callers', sorted(('Probe.java', source_identity(name)[1], 'fixture.Probe.' + name)
                                   for name in BINDINGS),
           sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    return expected, actual
