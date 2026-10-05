"""Production root-induced module graph and negative audit coverage tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import module_root_contracts as contracts


class ModuleRootTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_five_production_commands_share_root_ownership_and_induced_graph(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        for feature in contracts.FEATURES:
            differences = [k for k in expected[feature] if expected[feature][k] != actual[feature].get(k)]
            self.assertEqual(len(differences), 0, {'count': len(differences), 'keys': differences[:10]})

    def test_applicable_family_cannot_skip_or_claim_mcp_equivalence(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('root module fixture has no MCP oracle')
        fixture = Fixture(root, self.binary, self.directory / 'unused-index', state, oracle)
        plan(state, [{'path': 'Sentinel.java'}], '  class  Classes\n  file  Files', root=root, java_only=True)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertIn('not MCP equivalence', row['reason'])
            check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'identity': 'inapplicable'}, {'identity': 'wrong'}, {'identity': 'correct'}):
                fixture._module_root_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'identity': 'correct'}}, {feature: observed})):
                    fixture.evaluate(check)
                verdict = state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0]
                self.assertEqual(verdict, 'pass' if observed == {'identity': 'correct'} else 'fail')
        self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='global:scope-command-matrix'").fetchone()[0], 'pending')
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertFalse(fixture.database.exists())
        self.assertEqual([p.name for p in root.iterdir()], ['Sentinel.java'])
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_full_inventory_is_error(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)


if __name__ == '__main__':
    unittest.main()
