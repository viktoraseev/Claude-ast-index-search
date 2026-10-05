"""Compact production regressions for read-only Java management formats."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, canonical_json, connect
import management_format_contracts as contracts


class ManagementFormatTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Inventory.kt').write_text('// inventory only\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_family_values_source_identities_formats_limits_and_states(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        for feature in sorted(contracts.FEATURES):
            mismatches = [key for key in expected[feature]
                          if canonical_json(expected[feature][key]) != canonical_json(actual[feature].get(key))]
            self.assertEqual(mismatches, [], {'feature': feature, 'mismatches': mismatches})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])

    def test_audit_executes_once_and_applicable_contract_cannot_silently_skip(self):
        oracle = Mock()
        oracle.call.side_effect = AssertionError('management formats are not MCP equivalence')
        fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                fixture.evaluate(check)
                row = self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                diff = json.loads(row['diff_json'] or '{}')
                self.assertEqual(row['verdict'], 'pass', {'feature': feature, 'error': row['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertEqual(json.loads(row['expected_json'])['source'], contracts.REASONS[feature])
            self.assertEqual(exercise.call_count, 1)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'inventory': 'inapplicable'}, {'inventory': {'.java': 0}}):
                fixture._management_format_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'inventory': {'.java': 1, '.kt': 1, '.xml': 1}}}, {feature: observed})):
                    fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            fixture._management_format_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        self.assertFalse(fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_inventory_is_an_error_instead_of_inapplicable(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)


if __name__ == '__main__':
    unittest.main()
