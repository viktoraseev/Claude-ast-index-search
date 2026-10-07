"""Executed Java failures and guards against incomplete/fabricated coverage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
from publication_error_contracts import held
from root_contracts import Runner
import watch_vcs_error_contracts as contracts


class WatchVcsErrorTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('internal failures are not MCP equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(row)
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_watch_readiness_retries_only_publication_contention(self):
        runner = Runner(self.binary, self.directory)
        runner.root = self.root
        database = self.directory / 'readiness.sqlite'
        runner.environment.update(AST_INDEX_ROOT=str(self.root), AST_INDEX_DB_PATH=str(database))
        runner.command('rebuild', '--force')
        before = database.read_bytes()
        with held(database.with_suffix('.publish.lock')):
            # Polling a valid, present declaration during the generation swap
            # must wait; the real CLI intentionally rejects concurrent reads.
            for _ in range(2):
                self.assertFalse(contracts.declarations_ready(runner, 'Sentinel'))
        self.assertTrue(contracts.declarations_ready(runner, 'Sentinel'))
        self.assertFalse(contracts.declarations_ready(runner, 'Missing'))
        self.assertEqual(database.read_bytes(), before)
        database.write_bytes(b'fixture corrupt database')
        with self.assertRaises(ToolError):
            contracts.declarations_ready(runner, 'Sentinel')
        database.unlink()
        with self.assertRaises(ToolError):
            contracts.declarations_ready(runner, 'Sentinel')

    def test_production_family_with_complete_acceptance(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                row = self.evaluate(feature)
                diff = json.loads(row['diff_json'] or '{}')
                self.assertEqual(row['verdict'], 'pass', {'error': row['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertEqual(self.evaluate(feature)['verdict'], 'pass')
                expected = json.loads(row['expected_json'])
                self.assertEqual(expected['source'], contracts.REASON)
                self.assertTrue(contracts.acceptance_keys(feature) <= expected['samples'].keys())
            self.assertEqual(exercise.call_count, len(contracts.FEATURES))
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_each_executable_criterion_and_applicability_is_required(self):
        complete = {feature: {key: True for key in contracts.acceptance_keys(feature)}
                    for feature in contracts.FEATURES}
        for section in complete.values(): section['inventory'] = contracts.INVENTORY
        for feature in sorted(contracts.FEATURES):
            self.fixture._watch_vcs_error_results = None
            with patch.object(contracts, 'exercise', return_value=(complete, complete)):
                self.assertEqual(self.evaluate(feature)['verdict'], 'pass')
            for omitted in sorted(contracts.acceptance_keys(feature)):
                samples = {f: dict(s) for f, s in complete.items()}
                del samples[feature][omitted]
                self.fixture._watch_vcs_error_results = None
                with patch.object(contracts, 'exercise', return_value=(samples, samples)):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail', omitted)
            for sample in ({}, {**complete[feature], 'applicable-java': False},
                           {**complete[feature], 'inventory': {'Inventory.kt': '.kt'}}):
                samples = {**complete, feature: sample}
                self.fixture._watch_vcs_error_results = None
                with patch.object(contracts, 'exercise', return_value=(samples, samples)):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')

    def test_inventory_and_artifact_boundaries(self):
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

    def test_planning_preserves_case_ids_and_pending_parent(self):
        ids = dict(self.state.execute('SELECT id,feature FROM checks'))
        contracts.plan_errors(self.state, self.root)
        self.assertEqual(ids, dict(self.state.execute('SELECT id,feature FROM checks')))
        self.assertTrue(contracts.FEATURES <= required_features())
        parent = self.state.execute("SELECT * FROM coverage WHERE feature='global:format'").fetchone()
        self.assertEqual(parent['status'], 'pending')
        for gap in ('backend/channel', 'late-marker', 'update/graph refresh'):
            self.assertIn(gap, parent['reason'])


if __name__ == '__main__':
    unittest.main()
