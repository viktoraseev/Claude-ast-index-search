"""Executed source result contracts preserve pending parents and failed evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, adapter_digest
import java_dependency_result_contracts as contracts


class DependencyResultTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// inventory sentinel\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.oracle = Mock()
        self.oracle.call.side_effect = AssertionError('source evidence cannot call MCP')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'target.sqlite', self.state, self.oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)
        self.feature = next(iter(contracts.FEATURES))

    def evaluate(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_class_generic_and_var_result_family(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            row = self.evaluate()
            diff = json.loads(row['diff_json'] or '{}')
            self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
                'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
            self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.oracle.call.assert_not_called()
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])

    def test_known_remaining_overload_and_external_projection_are_positive_obligations(self):
        # Neither failure may disappear by being converted to an invalid-Java
        # guard or by replacing the external List signature with authored Box.
        for name in ('inherited-overload', 'jdk-list-projection',
                     'boxed-result', 'unboxed-result', 'array-covariant-result'):
            self.assertIs(contracts.CASES[name][3], True)
        self.assertIn('java.util.List<shared.Child>', contracts.CASES['jdk-list-projection'][0])
        self.assertIn('get(0).instance()', contracts.CASES['jdk-list-projection'][2])
        self.assertEqual(contracts.CASES['inherited-overload'][2], 'b.choose("x")')
        self.assertIn('child-overload-control', contracts.VALID_NEGATIVE_CASES)
        self.assertIs(contracts.CASES['child-overload-control'][3], False)

    def test_bad_missing_or_interrupted_results_never_pass_or_close_parents(self):
        self.assertIn(self.feature, required_features())
        before = [tuple(row) for row in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')]
        contracts.plan_results(self.state, self.root)
        self.assertEqual(before, [tuple(row) for row in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')])
        for got in ({}, {'case': 'inapplicable'}, {'case': []}, {'case': 'wrong-owner'}):
            self.fixture._java_dependency_result_results = None
            with patch.object(contracts, 'exercise', return_value=({self.feature: {'case': 'Base'}}, {self.feature: got})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._java_dependency_result_results = None
        with patch.object(contracts, 'exercise', side_effect=ToolError('interrupted contract')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix', 'graph', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (parent,)).fetchone()[0], 'pending')
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='extensions'").fetchone()[0], 'out-of-scope')

    def test_boundary_and_resume_fingerprint(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))
        before, read = adapter_digest(), Path.read_bytes
        with patch.object(Path, 'read_bytes', lambda path: read(path) + (
                b'\n# result contract change\n' if path.name == 'java_dependency_result_contracts.py' else b'')):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
