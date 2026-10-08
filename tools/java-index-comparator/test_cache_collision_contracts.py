"""The unified fixture executes cache collision production, with strict acceptance."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, source_snapshot
from replay import replay
import cache_collision_contracts as contracts


class CacheCollisionContracts(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('cache identities have no MCP equivalent')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_production_and_replay(self):
        row = self.evaluate()
        self.assertEqual(row['verdict'], 'pass', row['error'] or row['diff_json'])
        self.assertEqual(json.loads(row['expected_json'])['source'], contracts.REASON)
        with patch.object(contracts, 'exercise', side_effect=AssertionError('must be cached')):
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

    def test_partial_matching_acceptance_and_fake_inapplicability_fail(self):
        complete = {key: True for key in contracts.acceptance_keys()}
        complete['inventory'] = contracts.INVENTORY
        samples = [{key: value for key, value in complete.items() if key != omitted}
                   for omitted in contracts.acceptance_keys()]
        samples.extend(({}, {**complete, 'applicable-java': False},
                        {**complete, 'inventory': {'Inventory.kt': '.kt'}}))
        for sample in samples:
            self.fixture._cache_collision_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {contracts.FEATURE: sample}, {contracts.FEATURE: sample})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')

    def test_complete_inventory_boundary_and_durable_plan(self):
        inventory = contracts.mobile_contracts.inventory
        for extension in ('.java', '.kt', '.xml'):
            def incomplete(state, root, extension=extension):
                inventory(state, root)
                state.execute('DELETE FROM file_inventory WHERE extension=?', (extension,))
            with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
                with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                    contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, '/private/tmp')
        ids = dict(self.state.execute('SELECT id,feature FROM checks'))
        contracts.plan_cache(self.state, self.root)
        self.assertEqual(ids, dict(self.state.execute('SELECT id,feature FROM checks')))
        self.assertIn(contracts.FEATURE, required_features())
