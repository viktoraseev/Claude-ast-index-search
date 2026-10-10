"""Execute retained Java inferred-result regressions against the production CLI."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import explore_result_contracts as contracts
from root_contracts import Runner


class ExploreResultTests(unittest.TestCase):
    def setUp(self):
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        self.base.mkdir(parents=True, exist_ok=True)

    def test_inferred_result_slots_and_same_line_seed_union(self):
        expected, actual = contracts.exercise(self.binary, self.base)
        self.assertEqual([], [key for key in expected if expected[key] != actual.get(key)])

    def test_inferred_slots_keep_bounds_invariance_rank_and_owner_guards(self):
        runner = Runner(self.binary, Path(tempfile.mkdtemp(prefix='result-guards-', dir=self.base)))
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        for package in ('view', 'other'):
            directory = runner.root / package
            directory.mkdir()
            (directory / 'Leaf.java').write_text(
                f'package {package};\npublic class Leaf {{\n public int resultBeacon() {{ return 1; }}\n}}\n')
        cases = {
            'bounded-array': (False, False, '<T extends Leaf> T[] project(T[] input)',
                              'other.Leaf[] value', 'project(value)[0]'),
            'bounded-list': (False, False, '<T extends Leaf> List<T> project(List<T> input)',
                             'List<other.Leaf> value', 'project(value).get(0)'),
            'invariant-conflict': (False, False, '<T> List<T> project(List<T> first, List<T> second)',
                                   'List<Leaf> first, List<Child> second', 'project(first, second).get(0)'),
            'matrix-rank': (False, False, '<T> T[][] project(T[][] input)',
                            'Leaf[] value', 'project(value)[0][0]'),
            'fixed-slot': (False, False, '<T> Map<T,String> project(Map<T,String> input)',
                           'Map<Leaf,Integer> value', 'project(value).keySet().iterator().next()'),
            'explicit-conflict': (False, False, '<T> T[] project(T[] input)',
                                  'Leaf[] value', 'this.<other.Leaf>project(value)[0]'),
            'explicit-bound': (True, True, '<T extends Leaf> T[] project(T[] input)',
                               'Child[] value', 'this.<Leaf>project(value)[0]'),
            'explicit-only': (True, True, '<T> T[] project()', '', 'this.<Leaf>project()[0]'),
            'explicit-invariant-guard': (False, False, '<T> List<T> project(List<T> input)',
                                         'List<Child> value', 'this.<Leaf>project(value).get(0)'),
            'unwitnessed-bound': (True, True, '<T extends Leaf> List<T> project()',
                                  '', 'project().get(0)'),
            'decoy-array': (True, False, '<T> T[] project(T[] input)',
                            'other.Leaf[] value', 'project(value)[0]'),
            'bounded-nested': (True, True, '<T extends Leaf> List<List<T>> project(List<List<T>> input)',
                               'List<List<Child>> value', 'project(value).get(0).get(0)'),
            'postfix-result': (True, True, '<T> T project(T[] input)[]',
                               'Leaf[] value', 'project(value)[0]'),
        }
        path = runner.root / 'view/Probe.java'
        for label, (valid, caller, declaration, parameters, receiver) in cases.items():
            with self.subTest(case=label):
                path.write_text('package view;\nimport java.util.List; import java.util.Map;\nclass Child extends Leaf {}\n'
                                'class Probe {\n ' + declaration + ' { return null; }\n'
                                ' int invoke(' + parameters + ') { return ' + receiver + '.resultBeacon(); }\n}\n')
                with (runner.directory / (label + '.javac.log')).open('wb') as log:
                    compiled = subprocess.run(
                        ['javac', '-proc:none', '-d', str(runner.directory / 'classes'), str(path),
                         str(runner.root / 'view/Leaf.java'), str(runner.root / 'other/Leaf.java')],
                        stdout=log, stderr=log, timeout=30)
                self.assertEqual(valid, compiled.returncode == 0)
                runner.command('rebuild', '--force')
                for built in (False, True):
                    if built:
                        runner.json('graph', 'build')
                    doc = runner.json('explore', 'resultBeacon', '--rwr', cwd=runner.root / 'view')
                    self.assertEqual(
                        [('view.Probe.invoke', 'project/view/Probe.java', 6)] if caller else [],
                        sorted((row['name'], runner.path(row['path']), row['line'])
                               for row in doc['neighbours']), (label, built))


if __name__ == '__main__':
    unittest.main()
