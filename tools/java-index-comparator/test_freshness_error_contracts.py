"""Execute Java freshness failures and preserve complete acceptance on replay."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, source_snapshot
from replay import replay
from root_contracts import Runner
import freshness_error_contracts as contracts


class FreshnessErrors(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('freshness is an internal contract, not MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_production_and_original_case_replay(self):
        row = self.evaluate()
        diff = json.loads(row['diff_json'] or '{}')
        self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
            'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        expected = json.loads(row['expected_json'])
        self.assertEqual(expected['source'], contracts.REASON)
        self.assertTrue(contracts.acceptance_keys() <= expected['samples'].keys())
        with patch.object(contracts, 'exercise', side_effect=AssertionError('family must be cached')):
            self.assertEqual(self.evaluate()['verdict'], 'pass')
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', {
                'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
                'audit_scope': 'java'}.items())
            self.state.execute("UPDATE checks SET status='complete',verdict='fail' WHERE id=?", (row['id'],))
        summary = replay(self.directory / 'evidence.sqlite', self.root, self.binary, self.directory / 'replay')
        self.assertTrue(summary['verified'], summary['counts'])
        self.assertEqual(summary['counts'], {'pass': 1})
        verification = connect(Path(summary['verification']), read_only=True)
        try:
            self.assertEqual(verification.execute('SELECT id FROM checks').fetchone()[0], row['id'])
            self.assertEqual(verification.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        finally:
            verification.close()

    def test_partial_matching_samples_and_missing_inventory_cannot_pass(self):
        complete = {key: True for key in contracts.acceptance_keys()}
        complete['inventory'] = contracts.INVENTORY
        for omitted in sorted(contracts.acceptance_keys()):
            samples = {key: value for key, value in complete.items() if key != omitted}
            self.fixture._freshness_error_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {contracts.FEATURE: samples}, {contracts.FEATURE: samples})):
                self.assertEqual(self.evaluate()['verdict'], 'fail', omitted)
        for samples in ({}, {**complete, 'applicable-java': False},
                        {**complete, 'inventory': {'Inventory.kt': '.kt'}}):
            self.fixture._freshness_error_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {contracts.FEATURE: samples}, {contracts.FEATURE: samples})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')

    def test_full_inventory_and_artifact_boundary(self):
        inventory = contracts.mobile_contracts.inventory
        for extension in ('.java', '.kt', '.xml', '.gradle'):
            def incomplete(state, root, extension=extension):
                inventory(state, root)
                state.execute('DELETE FROM file_inventory WHERE extension=?', (extension,))
            with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
                with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                    contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, '/private/tmp')

    def test_java_file_links_and_directory_replacement(self):
        directory = self.directory / 'links'
        directory.mkdir()
        runner = Runner(self.binary, directory)
        runner.root.mkdir()
        (runner.root / '.git').mkdir()
        database = directory / 'index.sqlite'
        runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
        source = directory / 'authored-link-source.java'
        source.write_text('class LinkedBefore {}\n')
        link = runner.root / 'Linked.java'
        link.symlink_to(source)
        runner.command('rebuild', '--force')
        source.write_text('class LinkedAfterWithLongerName {}\n')
        self.assertEqual(runner.json('update')['status'], 'complete')
        self.assertEqual([r['name'] for r in runner.json('class', '--pattern', '*')['items']],
                         ['LinkedAfterWithLongerName'])
        source.unlink()
        code, output = runner.command('--format', 'json', 'update', acceptable=(0, 1))
        self.assertEqual((code, output), (1, ''))
        with sqlite3.connect(database) as connection:
            self.assertTrue(connection.execute("SELECT 1 FROM metadata WHERE key='index_update_dirty_at'").fetchone())
            self.assertEqual(connection.execute("SELECT name FROM symbols WHERE kind='class'").fetchall(),
                             [('LinkedAfterWithLongerName',)])
        source.write_text('class RecoveredLink {}\n')
        self.assertEqual(runner.json('update')['status'], 'complete')
        link.unlink()
        link.mkdir()
        self.assertEqual(runner.json('update')['status'], 'complete')
        self.assertEqual(runner.json('class', '--pattern', '*')['items'], [])
        self.assertEqual(source.read_text(), 'class RecoveredLink {}\n')

    def test_planning_preserves_existing_ids_and_pending_parents(self):
        ids = dict(self.state.execute('SELECT id,feature FROM checks'))
        parents = dict(self.state.execute("SELECT feature,status FROM coverage WHERE status='pending'"))
        contracts.plan_errors(self.state, self.root)
        self.assertEqual(ids, dict(self.state.execute('SELECT id,feature FROM checks')))
        self.assertEqual(parents, dict(self.state.execute("SELECT feature,status FROM coverage WHERE status='pending'")))
        self.assertIn(contracts.FEATURE, required_features())
        reason = self.state.execute("SELECT reason FROM coverage WHERE feature='global:format'").fetchone()[0]
        for gap in ('rollback/recovery I/O', 'notification backend/channel', 'moved-path'):
            self.assertIn(gap, reason)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
