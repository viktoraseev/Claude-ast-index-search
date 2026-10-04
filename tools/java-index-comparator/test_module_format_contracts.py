"""Executed Java module rendering regressions, with authored expectations."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, canonical_json, connect
import module_format_contracts as contracts


class ModuleFormatContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_family_formats_identities_categories_and_states(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        for feature in sorted(contracts.FEATURES):
            with self.subTest(feature=feature):
                mismatches = [key for key in expected[feature]
                              if canonical_json(expected[feature][key]) != canonical_json(actual[feature].get(key))]
                self.assertEqual(mismatches, [], {'feature': feature, 'mismatches': mismatches})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])

    def test_applicable_family_cannot_be_skipped_or_claim_parent_or_mcp_coverage(self):
        oracle = Mock()
        oracle.call.side_effect = AssertionError('module formats are not MCP equivalence')
        fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        for feature in sorted(contracts.FEATURES):
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for result, verdict in (({feature: {'identity': 'wrong'}}, 'fail'),
                                    ({feature: {'identity': 'correct'}}, 'pass')):
                fixture._module_format_results = None
                with patch.object(contracts, 'exercise', return_value=({feature: {'identity': 'correct'}}, result)):
                    fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], verdict)
            fixture._module_format_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('interrupted full inventory')):
                fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        self.assertFalse(fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_framework_inventory_is_an_error_for_applicable_fixture(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.gradle'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)


if __name__ == '__main__':
    unittest.main()
