"""Execute inherited imports and reject incomplete or fake scope coverage."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import java_inherited_import_contracts as contracts


class InheritedImportContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_inherited_imports_execute_production_and_javac(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        for feature in contracts.FEATURES:
            differences = [key for key in expected[feature] if expected[feature][key] != actual[feature].get(key)]
            self.assertEqual(differences, [])

    def test_java_contract_cannot_be_skipped_or_claim_mcp_equivalence(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (root / 'inventory.kt').write_text('// inventory only\n')
        state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('unused-deps has no equivalent MCP operation')
        fixture = Fixture(root, self.binary, self.directory / 'unused-index', state, oracle)
        plan(state, [{'path': 'Sentinel.java'}], '  class  Classes\n  file  Files', root=root, java_only=True)
        self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertIn('not MCP equivalence', row['reason'])
            check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'binding': 'inapplicable'}, {'binding': 'wrong'}, {'binding': 'correct'}):
                fixture._java_inherited_import_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'binding': 'correct'}}, {feature: observed})):
                    fixture.evaluate(check)
                verdict = state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0]
                self.assertEqual(verdict, 'pass' if observed == {'binding': 'correct'} else 'fail')
        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix'):
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (parent,)).fetchone()[0], 'pending')
        self.assertFalse(fixture.database.exists())
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_incomplete_inventory_and_external_mutation_are_rejected(self):
        inventory = contracts.mobile_contracts.inventory

        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.txt'")

        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_planning_preserves_existing_case_ids_completed_rows_and_parent_gaps(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        state = connect(self.directory / 'planning.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        plan(state, [{'path': 'Sentinel.java'}], '  class  Classes\n  file  Files', root=root, java_only=True)
        feature = next(iter(contracts.FEATURES))
        with state:
            state.execute("UPDATE checks SET status='complete',verdict='pass',expected_json='{}',actual_json='{}' WHERE feature=?", (feature,))
        before = [tuple(row) for row in state.execute('SELECT * FROM checks WHERE feature=?', (feature,))]
        parents = [tuple(row) for row in state.execute("SELECT * FROM coverage WHERE feature IN ('unused-deps:semantic-resolution','global:scope-command-matrix') ORDER BY feature")]
        contracts.plan_imports(state, root)
        contracts.plan_imports(state, root)
        self.assertEqual(before, [tuple(row) for row in state.execute('SELECT * FROM checks WHERE feature=?', (feature,))])
        self.assertEqual(parents, [tuple(row) for row in state.execute("SELECT * FROM coverage WHERE feature IN ('unused-deps:semantic-resolution','global:scope-command-matrix') ORDER BY feature")])


if __name__ == '__main__':
    unittest.main()
