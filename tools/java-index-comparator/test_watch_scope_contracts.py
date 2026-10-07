"""Executed watch scope coverage must retain every criterion and prior case ID."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import watch_scope_contracts as contracts


class WatchScopeContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('watch has no MCP equivalent')
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  file  Files', root=self.root, java_only=True)

    def evaluate(self):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_production_watch_scope(self):
        row = self.evaluate()
        diff = json.loads(row['diff_json'] or '{}')
        self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
            'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertTrue(contracts.acceptance_keys() <= json.loads(row['expected_json'])['samples'].keys())
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')

    def test_missing_or_inapplicable_criteria_cannot_pass(self):
        complete = {key: True for key in contracts.acceptance_keys()}
        complete['inventory'] = contracts.INVENTORY
        for omitted in sorted(complete):
            sample = dict(complete); del sample[omitted]
            self.fixture._watch_scope_results = None
            with patch.object(contracts, 'exercise', return_value=({contracts.FEATURE: sample}, {contracts.FEATURE: sample})):
                self.assertEqual(self.evaluate()['verdict'], 'fail', omitted)
        for sample in ({}, {**complete, 'applicable-java': False},
                       {**complete, 'inventory': {'.kt': 1}}):
            self.assertFalse(contracts.acceptance_complete(sample, sample))

    def test_inventory_and_artifact_boundary(self):
        inventory = contracts.mobile_contracts.inventory
        for extension in contracts.INVENTORY:
            def incomplete(state, root, extension=extension):
                inventory(state, root)
                state.execute('DELETE FROM file_inventory WHERE extension=?', (extension,))
            with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
                with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                    contracts.exercise(self.fixture.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.fixture.binary, '/private/tmp')

    def test_planning_preserves_parent_obligations_and_ids(self):
        before = list(self.state.execute('SELECT id FROM checks ORDER BY id'))
        contracts.plan_scope(self.state, self.root)
        self.assertEqual(before, list(self.state.execute('SELECT id FROM checks ORDER BY id')))
        self.assertIn(contracts.FEATURE, required_features())
        parent = self.state.execute("SELECT * FROM coverage WHERE feature='global:format'").fetchone()
        self.assertEqual(parent['status'], 'pending')
        self.assertIn('persistent publication recovery', parent['reason'])
        self.assertIn('attached-root notification', parent['reason'])
        for feature in ('perl-subs', 'xml-usages:syntax', 'resource-usages:xml-syntax'):
            row = self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()
            if row is not None:
                self.assertEqual(row['status'], 'out-of-scope')


if __name__ == '__main__':
    unittest.main()
