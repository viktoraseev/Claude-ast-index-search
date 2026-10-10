"""Java RWR receiver ownership across graph states; authored source, not MCP truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'explore:java-graph-state-ownership'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-explore-graph-state-ownership-v1'
REASON = ('independent source/javac/CLI: Java RWR resolved and empty caller ownership '
          'with absent/fresh/stale graphs, nominal/generic/overload/reference receivers, '
          'directory and attached-root intersections, source refresh and read-only graph state; '
          'not MCP equivalence')
SOURCES = {
    'other/Leaf.java': '''package other;
public class Leaf {
 public int ownershipBeacon() { return 2; }
 public int emptyBeacon() { return 2; }
}
''',
    'view/Leaf.java': '''package view;
class Leaf {
 int ownershipBeacon() { return 1; }
 int emptyBeacon() { return 1; }
}
''',
    'view/Probe.java': '''package view;
class Box<T> { T value; T get() { return value; } }
class Probe {
 int direct(Leaf value) { return value.ownershipBeacon(); }
 int generic(Box<Leaf> box) { return box.get().ownershipBeacon(); }
 int reference(Leaf value) { java.util.function.IntSupplier f = value::ownershipBeacon; return f.getAsInt(); }
 int foreign(other.Leaf value) { return value.ownershipBeacon() + value.emptyBeacon(); }
 int foreignChain(Box<other.Leaf> box) { return box.get().ownershipBeacon(); }
 int select(Leaf value) { return value.ownershipBeacon(); } int select(other.Leaf value) { return value.emptyBeacon(); }
 Leaf factory(Leaf value) { return value; } other.Leaf factory(other.Leaf value) { return value; }
 int overload(Leaf value) { return factory(value).ownershipBeacon(); }
 int foreignOverload(other.Leaf value) { return factory(value).ownershipBeacon(); }
 <T> T identity(T value) { return value; }
 int inferred(Leaf value) { return identity(value).ownershipBeacon(); }
 int foreignInferred(other.Leaf value) { return identity(value).ownershipBeacon(); }
}
''',
}
SCALAR_CASES = {
    'later-parameter': (True, '<T> T project(int tag, T input) { return input; }',
                        'Leaf input', 'project(0, input)', ''),
    'identical-witnesses': (True, '<T> T project(T first, T second) { return first; }',
                           'Leaf input', 'project(input, input)', ''),
    'bounded': (True, '<T extends Leaf> T project(T input) { return input; }',
                'Leaf input', 'project(input)', ''),
    'common-declared-bound': (True, '<T extends Leaf> T project(T first, T second, T third) { return first; }',
                              'Left first, Right second, Leaf third', 'project(first, second, third)', ''),
    'method-class-shadow': (True, '<T> T project(T input) { return input; }',
                            'Leaf input', 'project(input)', '<T extends other.Leaf>'),
    'bound-guard': (False, '<T extends Leaf> T project(T input) { return input; }',
                    'other.Leaf input', 'project(input)', ''),
    'conflicting-witnesses': (False, '<T> T project(T first, T second) { return first; }',
                             'Leaf first, other.Leaf second', 'project(first, second)', ''),
    'primitive-guard': (False, '<T> T project(T input) { return input; }',
                        'int input', 'project(input)', ''),
    'erased-object-guard': (False, '<T> T project(T input) { return input; }',
                            'Object input', 'project(input)', ''),
}



def plan_contracts(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java exploration fixtures must stay in repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='explore-graph-state-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    attached = runner.directory / 'attached'
    attached.mkdir()
    for root in (runner.root, attached):
        for relative, source in SOURCES.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        (root / 'Sentinel.kt').write_text('// excluded foreign parser sentinel\n')
        (root / 'sentinel.xml').write_text('<fixture/>\n')
    with (runner.directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                 *[str(runner.root / path) for path in SOURCES]],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('authored Java exploration fixture does not compile; see private log')
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
    finally:
        inventory.close()
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    record('inventory-and-explicit-exclusions', {'.java': 3, '.kt': 1, '.xml': 1}, counts)
    runner.command('rebuild', '--force')
    runner.command('subtree', 'add', 'extra', attached)
    runner.command('rebuild', '--force')
    for phase in ('absent', 'fresh', 'stale', 'refreshed'):
        if phase in ('fresh', 'refreshed'):
            runner.json('graph', 'build')
        elif phase == 'stale':
            # Keep callers valid but change dispatch to the out-of-scope owner.
            path = runner.root / 'view/Probe.java'
            path.write_text(SOURCES['view/Probe.java'].replace('direct(Leaf value)', 'direct(other.Leaf value)'))
            runner.command('update')
        before = runner.json('graph', 'status')['graph']
        record(phase + ':graph-stage', (phase != 'absent', phase == 'stale'),
               (before['built'], before['stale']))
        for prefix, flags, cwd in (
                ('project/', ('--local',), runner.root / 'view'),
                ('attached/', ('--subtree', 'extra'), runner.root)):
            wanted = [('view.Probe.' + name, prefix + 'view/Probe.java', line, 'caller')
                      for name, line in (('direct', 4), ('generic', 5), ('reference', 6), ('select', 9),
                                         ('overload', 11), ('inferred', 14))
                      if name != 'direct' or prefix == 'attached/' or phase in ('absent', 'fresh')]
            if prefix == 'attached/':
                wanted.extend(('view.Probe.' + name, prefix + 'view/Probe.java', line, 'caller')
                              for name, line in (('foreign', 7), ('foreignChain', 8),
                                                 ('foreignOverload', 12), ('foreignInferred', 15)))
            empty = [('view.Probe.' + name, prefix + 'view/Probe.java', line, 'caller')
                     for name, line in (('foreign', 7), ('select', 9))] if prefix == 'attached/' else []
            for seed, want in (('ownershipBeacon', sorted(wanted)), ('emptyBeacon', sorted(empty))):
                for cap in (0, 1, 6):
                    doc = runner.json(*flags, 'explore', seed, '--rwr', '--max-files', cap, cwd=cwd)
                    record(f'{phase}:{prefix}{seed}:{cap}', want,
                           sorted((row['name'], runner.path(row['path']), row['line'], row['link'])
                                  for row in doc['neighbours']))
        record(phase + ':read-only-graph-state', before, runner.json('graph', 'status')['graph'])
    for name, (valid, declaration, parameters, expression, class_parameters) in SCALAR_CASES.items():
        path = runner.root / 'view/Probe.java'
        path.write_text('package view;\nclass Probe' + class_parameters + ' {\n ' + declaration +
                        '\n int invoke(' + parameters + ') {\n return ' + expression +
                        '.ownershipBeacon();\n }\n}\nclass Left extends Leaf {} class Right extends Leaf {}\n')
        with (runner.directory / (name + '.javac.log')).open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                     *[str(runner.root / item) for item in SOURCES]],
                                    stdout=log, stderr=log, timeout=30)
        record('scalar:' + name + ':javac', valid, result.returncode == 0)
        runner.command('rebuild', '--force')
        for built in (False, True):
            if built:
                runner.json('graph', 'build')
            doc = runner.json('--local', 'explore', 'ownershipBeacon', '--rwr', cwd=runner.root / 'view')
            record('scalar:' + name + ':' + str(built),
                   [('view.Probe.invoke', 'project/view/Probe.java', 4, 'caller')] if valid else [],
                   sorted((row['name'], runner.path(row['path']), row['line'], row['link'])
                          for row in doc['neighbours']))
    return expected, actual
