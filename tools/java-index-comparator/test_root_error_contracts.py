"""Execute Java root failures and reject fabricated applicability/coverage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, source_snapshot
from replay import replay
import root_error_contracts as contracts


class RootErrorTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('root errors are not MCP equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        self.assertIsNotNone(row)
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_production_family_with_complete_acceptance(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            row = self.evaluate()
            diff = json.loads(row['diff_json'] or '{}')
            self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
                'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
            self.assertEqual(self.evaluate()['verdict'], 'pass')
            self.assertEqual(exercise.call_count, 1)
            expected = json.loads(row['expected_json'])
            self.assertEqual(expected['source'], contracts.REASON)
            self.assertTrue(contracts.acceptance_keys() <= expected['samples'].keys())
            self.assertIn(contracts.FEATURE, required_features())
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')

    def test_each_executable_criterion_is_required(self):
        required = contracts.acceptance_keys()
        complete = {key: True for key in required}
        complete['inventory'] = contracts.INVENTORY
        self.fixture._root_error_results = None
        with patch.object(contracts, 'exercise', return_value=(
                {contracts.FEATURE: complete}, {contracts.FEATURE: complete})):
            self.assertEqual(self.evaluate()['verdict'], 'pass')
        for omitted in sorted(required):
            samples = {key: value for key, value in complete.items() if key != omitted}
            self.fixture._root_error_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {contracts.FEATURE: samples}, {contracts.FEATURE: samples})):
                self.assertEqual(self.evaluate()['verdict'], 'fail', omitted)

    def test_applicable_java_cannot_be_silently_skipped(self):
        complete = {key: True for key in contracts.acceptance_keys()}
        complete['inventory'] = contracts.INVENTORY
        for samples in ({}, {'applicable-java': False}, {'inventory': 'inapplicable'},
                        {**complete, 'applicable-java': False},
                        {**complete, 'inventory': {'Inventory.kt': '.kt'}}):
            self.fixture._root_error_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {contracts.FEATURE: samples}, {contracts.FEATURE: samples})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        inventory = contracts.mobile_contracts.inventory
        for extension in ('.java', '.kt', '.xml'):
            def incomplete(state, root, extension=extension):
                inventory(state, root)
                state.execute('DELETE FROM file_inventory WHERE extension=?', (extension,))
            with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
                with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                    contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_pending_parent_and_existing_ids_survive_planning(self):
        before = dict(self.state.execute('SELECT id,feature FROM checks'))
        contracts.plan_errors(self.state, self.root)
        self.assertEqual(before, dict(self.state.execute('SELECT id,feature FROM checks')))
        row = self.state.execute("SELECT * FROM coverage WHERE feature='global:format'").fetchone()
        self.assertEqual(row['status'], 'pending')
        for gap in ('watch/VCS failures',
                    'restore commit/recovery I/O', 'refresh/update publication failures'):
            self.assertIn(gap, row['reason'])

    def test_original_case_id_replays_without_oracle(self):
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', {
                'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
                'audit_scope': 'java'}.items())
            self.state.execute("UPDATE checks SET status='complete',verdict='fail' WHERE feature=?",
                               (contracts.FEATURE,))
        original = self.state.execute('SELECT id FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()[0]
        summary = replay(self.directory / 'evidence.sqlite', self.root, self.binary,
                         self.directory / 'replay')
        self.assertTrue(summary['verified'], summary['counts'])
        self.assertEqual(summary['counts'], {'pass': 1})
        verification = connect(Path(summary['verification']), read_only=True)
        try:
            self.assertEqual(verification.execute('SELECT id FROM checks').fetchone()[0], original)
            self.assertEqual(verification.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        finally:
            verification.close()


if __name__ == '__main__':
    unittest.main()
