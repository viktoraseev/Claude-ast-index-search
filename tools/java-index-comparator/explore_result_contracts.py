"""Retained Java method-result obligations; a mismatch remains a failed check.

Each input is valid Java, and resolves to the authored declaration in view/.
Colliding other/ methods prevent name-only resolution from satisfying a case.
This executes exploration itself, not native DB or graph agreement as an oracle.
"""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURE = 'explore:java-inferred-result-slots'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-explore-inferred-result-slots-v1'
REASON = ('independent source/javac/CLI: Java inferred array/matrix/parameterized/nested '
          'method results, repeated common-bound and assignment-target inference, '
          'same-line overload seed unions and directory-owned RWR neighbours; not MCP equivalence')
CASES = {
    'array': ('<T> T[] project(T[] input) { return input; }',
              'Leaf[] value', 'project(value)[0].resultBeacon()'),
    'matrix': ('<T> T[][] project(T[][] input) { return input; }',
               'Leaf[][] value', 'project(value)[0][0].resultBeacon()'),
    'slot': ('<T> java.util.List<T> project(java.util.List<T> input) { return input; }',
             'java.util.List<Leaf> value', 'project(value).get(0).resultBeacon()'),
    'nested': ('<T> java.util.List<java.util.List<T>> project(java.util.List<java.util.List<T>> input) { return input; }',
               'java.util.List<java.util.List<Leaf>> value', 'project(value).get(0).get(0).resultBeacon()'),
    'common': ('<T extends Leaf> T project(T first, T second) { return first; }',
               'Left first, Right second', 'project(first, second).resultBeacon()'),
    'target': ('<T> T project() { return null; }', '',
               'value.resultBeacon()'),
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
        raise ToolError('Java result artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='explore-results-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for directory in ('view', 'other'):
        (runner.root / directory).mkdir()
        (runner.root / directory / 'Leaf.java').write_text(
            f'package {directory};\npublic class Leaf {{\n public int resultBeacon() {{ return 1; }}\n}}\n')
    expected, actual = {}, {}
    for name, (declaration, parameters, expression) in CASES.items():
        source = ('package view;\nclass Left extends Leaf {} class Right extends Leaf {}\n'
                  'class Probe {\n ' + declaration + '\n int invoke(' + parameters + ') {\n ' +
                  ('Leaf value = project();\n ' if name == 'target' else '') +
                  'return ' + expression + ';\n }\n}\n')
        path = runner.root / 'view/Probe.java'
        path.write_text(source)
        with (runner.directory / (name + '.javac.log')).open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                     str(path), str(runner.root / 'view/Leaf.java')],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored Java result case does not compile; see private logs')
        runner.command('rebuild', '--force')
        for built in (False, True):
            if built:
                runner.json('graph', 'build')
            doc = runner.json('explore', 'resultBeacon', '--rwr', cwd=runner.root / 'view')
            key = f'{name}:{built}'
            expected[key] = [('view.Probe.invoke', 'project/view/Probe.java', 5, 'caller')]
            actual[key] = sorted((row['name'], runner.path(row['path']), row['line'], row['link'])
                                 for row in doc['neighbours'])
    path.write_text('''package view;
class Left extends Leaf {} class Right extends Leaf {}
class Probe {
 int resultBeacon(Left first) { return 1; } int resultBeacon(Right second) { return 2; }
 int alpha() { return resultBeacon(new Left()); }
 int beta() { return resultBeacon(new Right()); }
}
''')
    with (runner.directory / 'seed-union.javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                 str(path), str(runner.root / 'view/Leaf.java')],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('authored Java overload seed union does not compile')
    runner.command('rebuild', '--force')
    for built in (False, True):
        if built:
            runner.json('graph', 'build')
        doc = runner.json('explore', 'resultBeacon', '--rwr', cwd=runner.root / 'view')
        key = f'seed-union:{built}'
        expected[key] = [('view.Probe.' + name, 'project/view/Probe.java', line, 'caller')
                         for name, line in (('alpha', 5), ('beta', 6))]
        actual[key] = sorted((row['name'], runner.path(row['path']), row['line'], row['link'])
                             for row in doc['neighbours'])
    return expected, actual
