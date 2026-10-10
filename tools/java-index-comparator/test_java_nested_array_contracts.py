"""Execute the nested array family; missing ownership is never a pass."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_nested_array_contracts as contracts
import scope_acceptance


class NestedArrayContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// mixed inventory\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.oracle = Mock()
        self.oracle.call.side_effect = AssertionError('source fixture has no MCP oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'target.sqlite', self.state, self.oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)
        self.feature = next(iter(contracts.FEATURES))

    def evaluate(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_family_and_retained_guards(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            row = self.evaluate()
            diff = json.loads(row['diff_json'] or '{}')
            self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
                'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
            self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
            samples = json.loads(row['expected_json'])['samples']
            criterion = next(item for item in scope_acceptance.specification()['criteria']
                             if item['feature'] == self.feature)
            scope_acceptance.validate_population(samples, criterion)
            for label in contracts.CASES:
                self.assertIn(label + ':True', samples)
                self.assertIn(label + ':text', samples)
            for key in ('inventory', 'javac-positive', 'attached:javac', 'attached:update',
                        'attached:rebuild', 'attached:changed-result'):
                self.assertIn(key, samples)
            # The acceptance ledger must retain every guard and attached-root
            # proof, including the nested declaring-owner assertion shapes.
            for key in ('rank-guard:True', 'primitive-guard:True', 'attached:update', 'attached:changed-result'):
                smaller = dict(samples)
                del smaller[key]
                with self.assertRaises(scope_acceptance.AcceptancePending):
                    scope_acceptance.validate_population(smaller, criterion)
        self.oracle.call.assert_not_called()
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:semantic-resolution'").fetchone()[0], 'pending')

    def test_missing_wrong_inapplicable_and_interrupted_proofs_remain_failures(self):
        self.assertIn(self.feature, required_features())
        before = [tuple(r) for r in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')]
        contracts.plan_slots(self.state, self.root)
        self.assertEqual(before, [tuple(r) for r in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')])
        for got in ({}, {'slot': []}, {'slot': 'inapplicable'}, {'slot': 'wrong-owner'}):
            self.fixture._java_nested_array_results = None
            with patch.object(contracts, 'exercise', return_value=({self.feature: {'slot': ['Base']}}, {self.feature: got})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._java_nested_array_results = None
        with patch.object(contracts, 'exercise', side_effect=ToolError('interrupted fixture')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:semantic-resolution'").fetchone()[0], 'pending')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='extensions'").fetchone()[0], 'out-of-scope')

    def test_boundary_and_adapter_fingerprint(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))
        original, read = adapter_digest(), Path.read_bytes
        with patch.object(Path, 'read_bytes', lambda p: read(p) + (
                b'\n# changed contract\n' if p.name == 'java_nested_array_contracts.py' else b'')):
            self.assertNotEqual(adapter_digest(), original)

    def test_applicable_family_cannot_skip_incomplete_mixed_inventory(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'full inventory incomplete'):
                contracts.exercise(self.binary, self.directory)


if __name__ == '__main__':
    unittest.main()
