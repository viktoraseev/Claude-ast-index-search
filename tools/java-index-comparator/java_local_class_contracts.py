"""Local Java class shadows across graph consumers; source truth, not MCP."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from java_type_binding_contracts import edges, identity
import java_receiver_site_contracts

SCOPES = 'graph:java-local-class-shadows'
MEMBERS = 'graph:java-local-class-members'
EXPLORE = 'explore:java-local-class-shadows'
RECEIVERS = 'graph:java-local-class-receiver-sites'
RECEIVER_EXPLORE = 'explore:java-local-class-receiver-sites'
FEATURES = {SCOPES, MEMBERS, EXPLORE, RECEIVERS, RECEIVER_EXPLORE} | java_receiver_site_contracts.FEATURES
REASON = ('independent source/state: disposable javac-validated Java local classes, '
          'member/package/import shadows, declaration/block/sibling boundaries, nested '
          'types and static qualifiers, graph pages/reverse/path and exploration; '
          'distinct-line direct/chained nominal and generic receiver declaration sites across later local shadows; '
          'not MCP equivalence or compiler-wide receiver dispatch')
SOURCES = {
    'Leaf.java': '''package fixture;
class Leaf {
 static class Nested {}
}
''',
    'Probe.java': '''package fixture;
public class Probe {
 public static class Leaf { // member
  public static class Nested {}
  static int marker() { return 1; }
 }
 Object local() {
  new Leaf.Nested();
  class Leaf { // local
   static class Nested {}
   static int marker() { return 2; }
  }
  Leaf value = null;
  Leaf.marker();
  return new Leaf.Nested();
 }
 Object outside() { Leaf.marker(); return new Leaf.Nested(); }
 Object sibling() {
  class Leaf { // sibling
   static class Nested {}
   static int marker() { return 3; }
  }
  Leaf.marker(); return new Leaf.Nested();
 }
 Object block() {
  {
   class Leaf { // block
    static class Nested {}
   }
   new Leaf.Nested();
  }
  return new Leaf.Nested();
 }
 Object self() {
  class Leaf { // self
   Leaf same;
  }
  return new Leaf();
 }
 Object emptyShadow() {
  class Leaf {} // emptyShadow
  return new Leaf();
 }
}
''',
    'PackageProbe.java': '''package fixture;
class PackageProbe {
 Object local() {
  class Leaf { // local
   static class Nested {}
  }
  return new Leaf.Nested();
 }
 Object outside() { return new Leaf.Nested(); }
}
''',
    'ImportedProbe.java': '''package imported;
import fixture.Probe.Leaf;
class ImportedProbe {
 Object local() {
  class Leaf { // local
   static class Nested {}
  }
  return new Leaf.Nested();
 }
 Object outside() { return new Leaf.Nested(); }
}
''',
    'ReceiverProbe.java': '''package fixture;
class ReceiverProbe {
 Leaf field;
 int parameter(Leaf input) {
  class Leaf { int marker() { return 2; } }
  return input.marker();
 }
 int variable() {
  Leaf input = null;
  class Leaf { int marker() { return 3; } }
  return input.marker();
 }
 int inferred() {
  var input = new Leaf();
  class Leaf { int marker() { return 4; } }
  return input.marker();
 }
 int fieldCall() {
  class Leaf { int marker() { return 5; } }
  return this.field.marker();
 }
 int local() {
  class Leaf { int marker() { return 6; } }
  Leaf input = null;
  return input.marker();
 }
 int captured(Leaf input) {
  class Worker {
   int invoke() {
    class Leaf { int marker() { return 7; } }
    return input.marker();
   }
  }
  return 0;
 }
 int reference(Leaf input) {
  class Leaf { int marker() { return 8; } }
  java.util.function.IntSupplier task = input::marker;
  return 0;
 }
 int sibling() { Leaf input = null; return input.marker(); }
}
''',
}
SOURCES['Leaf.java'] = SOURCES['Leaf.java'].replace(' static class Nested {}',
    ' static class Nested {}\n int marker() { return 1; }')
RECEIVER_BINDINGS = {name: ('Leaf.java', 4, 'marker') for name in
                     ('parameter', 'variable', 'inferred', 'fieldCall', 'invoke', 'reference', 'sibling')}
RECEIVER_BINDINGS['local'] = ('ReceiverProbe.java', 23, 'marker')
for name in ('Leaf.java', 'ReceiverProbe.java'):
    SOURCES['receiver-view/' + name] = SOURCES[name].replace('package fixture;', 'package fixture.receiverview;')
SOURCES['receiver-view/ReceiverProbe.java'] = SOURCES['receiver-view/ReceiverProbe.java'].replace(
    'int invoke()', 'int viewInvoke()').replace('int marker()', 'int decoy()').replace(
    'class Leaf { int decoy() { return 6; } }\n  Leaf input = null;\n  return input.marker();',
    'class Leaf { int decoy() { return 6; } }\n  Leaf input = null;\n  return input.decoy();')


def authored(file, name):
    lines = [line for line, source in enumerate(SOURCES[file].splitlines(), 1)
             if f' {name}(' in source]
    if len(lines) != 1:
        raise ToolError('authored local class seed is not unique')
    return file, lines[0], name


def targets(file, line, *names):
    return [(file, line, name) for name in names]


def local_targets(file, label, members=('Leaf', 'Nested')):
    line = next(i for i, source in enumerate(SOURCES[file].splitlines(), 1)
                if '// ' + label in source)
    return [(file, line + offset, name) for offset, name in enumerate(members)]


MEMBER = local_targets('Probe.java', 'member', ('Leaf', 'Nested', 'marker'))
BINDINGS = {
    ('Probe.java', 'local'): MEMBER[:2] + local_targets('Probe.java', 'local', ('Leaf', 'Nested', 'marker')),
    ('Probe.java', 'outside'): MEMBER,
    ('Probe.java', 'sibling'): local_targets('Probe.java', 'sibling', ('Leaf', 'Nested', 'marker')),
    ('Probe.java', 'block'): MEMBER[:2] + local_targets('Probe.java', 'block'),
    ('Probe.java', 'self'): local_targets('Probe.java', 'self', ('Leaf',)),
    ('Probe.java', 'emptyShadow'): local_targets('Probe.java', 'emptyShadow', ('Leaf',)),
    ('PackageProbe.java', 'local'): local_targets('PackageProbe.java', 'local'),
    ('PackageProbe.java', 'outside'): [('Leaf.java', 2, 'Leaf'), ('Leaf.java', 3, 'Nested')],
    ('ImportedProbe.java', 'local'): local_targets('ImportedProbe.java', 'local'),
    ('ImportedProbe.java', 'outside'): MEMBER[:2],
}


def qualified(file, name):
    return ('imported.' if file == 'ImportedProbe.java' else 'fixture.') + file[:-5] + '.' + name


def plan_classes(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-local-class-shadows'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute("UPDATE coverage SET reason=replace(reason,?,?) WHERE feature=? AND status='pending'",
                          ('declaration-site local-class receiver binding',
                           'chained/generic declaration-site local-class receiver binding', parent))
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending'",
                          ('; separate Java fixture checks direct nominal receiver declaration sites across '
                           'later local shadows, including captures and method references; chained/generic '
                           'receivers, same-line type sites and compiler-wide dispatch remain unresolved', parent))
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending'",
                          ('; separate executed javac/source contract covers nominal field/return chains, '
                           'List/Optional/nested Map/List and explicit generic Box projections, captures, '
                           'method references and missing-member guards at receiver declaration sites; '
                           'same-line local declaration collisions, generic inference beyond explicit type '
                           'arguments, overload/accessibility dispatch and attached-root receiver semantics '
                           'remain pending; not MCP equivalence', parent))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('local class fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-local-classes-', dir=base)).resolve())
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for name, source in SOURCES.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        if inventory != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('local class fixture inventory incomplete')
    finally:
        state.close()
    def compile_sources(paths, label):
        with (runner.directory / (label + '.stdout.log')).open('wb') as stdout, \
                (runner.directory / (label + '.stderr.log')).open('wb') as stderr:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                   *map(str, paths)], stdout=stdout, stderr=stderr, timeout=30).returncode
    if compile_sources([runner.root / name for name in SOURCES], 'javac'):
        raise ToolError('authored Java local class fixture does not compile; see private logs')
    # A local shadow lacking a member cannot borrow the enclosing type's member.
    guard = runner.directory / 'Guard.java'
    guard.write_text('class Guard { static class Leaf { static class Nested {} } '
                     'Object broken() { class Leaf {} return new Leaf.Nested(); } }\n')
    if not compile_sources([guard], 'javac-negative'):
        raise ToolError('javac accepted a local shadow member guard')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for name, target in RECEIVER_BINDINGS.items():
        seed = 'invoke' if name == 'invoke' else 'fixture.ReceiverProbe.' + name
        source = authored('ReceiverProbe.java', name)
        wanted = [(target, 'scoped')]
        if name == 'fieldCall':
            wanted.append((('ReceiverProbe.java', 3, 'field'), 'local'))
        elif name != 'invoke':
            wanted.append((('ReceiverProbe.java', 23, 'Leaf') if name == 'local'
                           else ('Leaf.java', 2, 'Leaf'), 'scoped'))
        wanted.sort()
        for limit in (0, 1, 100):
            for ambiguous in (False, True):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                record(RECEIVERS, f'{name}:{limit}:{ambiguous}',
                       {'matched': [source], 'total': len(wanted), 'count': min(limit, len(wanted)),
                        'valid': True, 'complete': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'total': doc.get('pagination', {}).get('total'), 'count': len(doc['items']),
                        'valid': len(set(edges(doc))) == len(doc['items']) and all(edge in wanted for edge in edges(doc)),
                        'complete': limit < len(wanted) or edges(doc) == wanted})
        qualifier = 'fixture.Leaf.marker' if target[0] == 'Leaf.java' else 'marker'
        doc = runner.json('graph', 'path', seed, qualifier, '--max-depth', 1)
        record(RECEIVERS, name + ':path', [(source, target)],
               sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']))
    doc = runner.json('graph', 'dependents', 'fixture.Leaf.marker', '--limit', 100)
    record(RECEIVERS, 'package:reverse',
           sorted(authored('ReceiverProbe.java', name) for name, target in RECEIVER_BINDINGS.items()
                  if target[0] == 'Leaf.java'), sorted(identity(row['other']) for row in doc['items']))
    # The scoped copy has one marker declaration. Later local types expose
    # only decoy(), so lexical seeding cannot mask misbound receiver calls.
    # Seven authored callers fit below the ten-neighbour renderer budget.
    doc = runner.json('explore', 'marker', '--rwr', '--max-files', 100,
                      cwd=runner.root / 'receiver-view')
    record(RECEIVER_EXPLORE, 'marker:callers',
           sorted(('receiver-view/ReceiverProbe.java', authored('ReceiverProbe.java', name)[1],
                   'viewInvoke' if name == 'invoke' else 'fixture.receiverview.ReceiverProbe.' + name)
                  for name, target in RECEIVER_BINDINGS.items() if target[0] == 'Leaf.java'),
           sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    for (file, name), bound in BINDINGS.items():
        for limit in (0, 1, 100):
            for ambiguous in (False, True):
                doc = runner.json('graph', 'dependencies', qualified(file, name), '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                observed = edges(doc)
                wanted = sorted((target, 'local' if target[2] == 'marker' else 'scoped') for target in bound)
                record(SCOPES, f'{file}:{name}:{limit}:{ambiguous}',
                       {'matched': [authored(file, name)], 'total': len(wanted),
                        'count': min(limit, len(wanted)), 'valid': True, 'complete': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'total': doc.get('pagination', {}).get('total'), 'count': len(observed),
                        'valid': len(set(observed)) == len(observed) and all(edge in wanted for edge in observed),
                        'complete': limit < len(wanted) or observed == wanted})
    for qualifier, target in [('fixture.Probe.Leaf.Nested', MEMBER[1]),
                              ('fixture.Probe.Leaf.marker', MEMBER[2]),
                              ('fixture.Leaf.Nested', ('Leaf.java', 3, 'Nested'))]:
        selected = {target}
        wanted = sorted(authored(file, name) for (file, name), bound in BINDINGS.items()
                        if any(end in selected for end in bound))
        doc = runner.json('graph', 'dependents', qualifier, '--limit', 100)
        record(MEMBERS, qualifier + ':reverse', wanted,
               sorted(identity(row['other']) for row in doc['items']))
        for (file, name), bound in BINDINGS.items():
            doc = runner.json('graph', 'path', qualified(file, name), qualifier, '--max-depth', 1)
            record(MEMBERS, file + ':' + name + ':path:' + qualifier,
                   sorted((authored(file, name), end) for end in bound if end in selected),
                   sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']))
    doc = runner.json('explore', 'Nested', '--rwr', '--max-files', 100)
    record(EXPLORE, 'callers', sorted((file, authored(file, name)[1], qualified(file, name))
                                   for (file, name), bound in BINDINGS.items()
                                   if any(end[2] == 'Nested' for end in bound)),
           sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    doc = runner.json('graph', 'dependencies', 'same', '--include-ambiguous', '--limit', 100)
    record(MEMBERS, 'local-self-field', [(local_targets('Probe.java', 'self', ('Leaf',))[0], 'scoped')], edges(doc))
    # The local receiver cannot borrow a package callable absent from its
    # declared type. Validate rejection independently with the compiler.
    receiver_guard = SOURCES['ReceiverProbe.java'].replace(
        'class Leaf { int marker() { return 6; } }', 'class Leaf {}')
    if receiver_guard == SOURCES['ReceiverProbe.java']:
        raise ToolError('receiver member guard was not injected')
    (runner.root / 'ReceiverProbe.java').write_text(receiver_guard)
    if not compile_sources([runner.root / name for name in SOURCES], 'javac-receiver-negative'):
        raise ToolError('javac accepted a receiver shadow member leak')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'fixture.ReceiverProbe.local', '--limit', 100,
                          *(['--include-ambiguous'] if ambiguous else []))
        record(RECEIVERS, f'missing-callable:{ambiguous}',
               [(('ReceiverProbe.java', 23, 'Leaf'), 'scoped')], edges(doc))
    # Keep erroneous source separate from the positive javac fixture. The
    # index must retain the local type dependency without inventing Nested.
    changed = SOURCES['Probe.java'].replace('  return new Leaf();\n }\n}',
                                           '  return new Leaf.Nested();\n }\n}')
    if changed == SOURCES['Probe.java']:
        raise ToolError('local shadow negative fixture was not injected')
    (runner.root / 'Probe.java').write_text(changed)
    (runner.root / 'ReceiverProbe.java').write_text(SOURCES['ReceiverProbe.java'])
    if not compile_sources([runner.root / name for name in SOURCES], 'javac-shadow-negative'):
        raise ToolError('javac accepted an enclosing-member leak')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'fixture.Probe.emptyShadow', '--limit', 100,
                          *(['--include-ambiguous'] if ambiguous else []))
        record(MEMBERS, f'missing-member:{ambiguous}',
               [(local_targets('Probe.java', 'emptyShadow', ('Leaf',))[0], 'scoped')], edges(doc))
    site_expected, site_actual = java_receiver_site_contracts.exercise(binary, base)
    expected.update(site_expected)
    actual.update(site_actual)
    return expected, actual
