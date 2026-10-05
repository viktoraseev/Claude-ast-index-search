"""Compact production regressions for Java management mutation rendering."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, canonical_json, connect
import mutation_format_contracts as contracts


class MutationFormatTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_formats_and_authored_mutation_effects(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        for feature in sorted(contracts.FEATURES):
            mismatches = [key for key in expected[feature]
                          if canonical_json(expected[feature][key]) != canonical_json(actual[feature].get(key))]
            self.assertEqual(mismatches, [], {'feature': feature, 'mismatches': mismatches})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])

    def test_audit_runs_family_once_without_oracle_or_target_mutations(self):
        oracle = Mock()
        oracle.call.side_effect = AssertionError('management mutation formats have no MCP equivalent')
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
                self.assertEqual(json.loads(row['expected_json'])['source'], contracts.REASON)
            self.assertEqual(exercise.call_count, 1)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        self.assertFalse(fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])

    def test_applicable_missing_wrong_or_fake_inapplicable_results_cannot_pass(self):
        fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, Mock())
        contracts.plan_formats(self.state, self.root)
        for feature in contracts.FEATURES:
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'effect': 'inapplicable'}, {'effect': {'removed': False}},
                             {'effect': {'removed': True}, 'unexpected': True}):
                fixture._mutation_format_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'effect': {'removed': True}}}, {feature: observed})):
                    fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            fixture._mutation_format_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete fixture')):
                fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')

    def test_incomplete_all_type_inventory_and_external_mutation_scope_are_errors(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_changed_mutation_contract_invalidates_audit_checkpoints(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'mutation_format_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
