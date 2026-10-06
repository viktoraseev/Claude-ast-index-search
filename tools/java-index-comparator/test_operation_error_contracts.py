"""Execute failure composition; matching empty samples cannot prove acceptance."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, source_snapshot
from replay import replay
import operation_error_contracts as contracts


class OperationErrorTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Inventory.kt').write_text('// inventory only\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('operation failures are not MCP equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_error_family_and_positive_controls(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                row = self.evaluate(feature)
                diff = json.loads(row['diff_json'] or '{}')
                self.assertEqual(row['verdict'], 'pass', {'feature': feature, 'error': row['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                expected = json.loads(row['expected_json'])
                self.assertEqual(expected['source'], contracts.REASON)
                self.assertTrue(contracts.acceptance_keys(feature) <= expected['samples'].keys())
                self.assertIn(feature, required_features())
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')

    def test_matching_partial_or_inapplicable_samples_fail(self):
        for feature in sorted(contracts.FEATURES):
            for samples in ({}, {'inventory': 'inapplicable'}, {'inventory': {'.java': 0}}):
                self.fixture._operation_error_results = None
                with patch.object(contracts, 'exercise', return_value=({feature: samples}, {feature: samples})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._operation_error_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')

    def test_each_acceptance_item_is_required_even_when_remaining_items_match(self):
        # A shape-only fixture can match every value while omitting an entire
        # production branch. Derive the obligations independently of output.
        for feature in sorted(contracts.FEATURES):
            required = contracts.acceptance_keys(feature)
            for omitted in sorted(required):
                samples = {key: True for key in required - {omitted}}
                self.fixture._operation_error_results = None
                with patch.object(contracts, 'exercise', return_value=({feature: samples}, {feature: samples})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail', omitted)

    def test_full_inventory_and_artifact_boundary_are_enforced(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_existing_case_ids_and_pending_parent_are_preserved(self):
        before = dict(self.state.execute('SELECT id,feature FROM checks'))
        contracts.plan_errors(self.state, self.root)
        self.assertEqual(before, dict(self.state.execute('SELECT id,feature FROM checks')))
        coverage = self.state.execute("SELECT * FROM coverage WHERE feature='global:format'").fetchone()
        self.assertEqual(coverage['status'], 'pending')
        for gap in ('lexical scan I/O', 'root-registration I/O', 'watch/VCS failures',
                    'restore commit/recovery I/O', 'refresh/update publication failures'):
            self.assertIn(gap, coverage['reason'])

    def test_failed_family_replays_without_mcp_and_retains_case_ids(self):
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', {
                'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
                'audit_scope': 'java'}.items())
            self.state.executemany("UPDATE checks SET status='complete',verdict='fail' WHERE feature=?",
                                   [(feature,) for feature in sorted(contracts.FEATURES)])
        ids = {r[0] for r in self.state.execute('SELECT id FROM checks WHERE verdict=\'fail\'')}
        summary = replay(self.directory / 'evidence.sqlite', self.root, self.binary,
                         self.directory / 'replay')
        self.assertTrue(summary['verified'], summary['counts'])
        self.assertEqual(summary['counts'], {'pass': len(contracts.FEATURES)})
        verification = connect(Path(summary['verification']), read_only=True)
        try:
            self.assertEqual({r[0] for r in verification.execute('SELECT id FROM checks')}, ids)
            self.assertEqual(verification.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        finally:
            verification.close()


if __name__ == '__main__':
    unittest.main()
