"""Execute Java selector failures; no IDE response or native DB is an oracle."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect, source_snapshot
from replay import replay
import selector_error_contracts as contracts


class SelectorErrorTests(unittest.TestCase):
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
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('selector failures are not MCP equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_selector_and_availability_error_composition(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertEqual(json.loads(result['expected_json'])['source'], contracts.REASON)
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_java_errors_cannot_be_skipped_or_shape_only_passed(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in sorted(contracts.FEATURES):
            self.assertIn(feature, required_features())
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'implemented')
            for got in ({}, {'failure': {'exit': 0, 'stdout': '{}'}}, {'failure': 'inapplicable'}):
                self.fixture._selector_error_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'failure': {'exit': 1, 'stdout': ''}}}, {feature: got})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._selector_error_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')

    def test_inventory_boundary_and_fingerprint(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed selector contract\n' if path.name == 'selector_error_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)

    def test_failed_family_replays_through_production_without_an_oracle(self):
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', {
            'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
            'audit_scope': 'java',
        }.items())
        self.state.executemany("UPDATE checks SET status='complete',verdict='fail' WHERE feature=?",
                               [(feature,) for feature in contracts.FEATURES])
        self.state.commit()
        result = replay(self.directory / 'evidence.sqlite', self.root, self.binary,
                        self.directory / 'replays')
        self.assertTrue(result['verified'], result['counts'])
        self.assertEqual(result['counts'], {'pass': 2})
        verification = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(verification.execute('SELECT count(*) FROM checks').fetchone()[0], 2)
            self.assertEqual(verification.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        finally:
            verification.close()


if __name__ == '__main__':
    unittest.main()
