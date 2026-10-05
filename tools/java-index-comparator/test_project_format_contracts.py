"""Execute shared project formats on small Java sources, without an MCP oracle."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import project_format_contracts as contracts


class ProjectFormatTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('project formats do not establish MCP equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_family_counts_pages_errors_and_delegate_format_composition(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                with self.subTest(feature=feature):
                    result = self.evaluate(feature)
                    diff = json.loads(result['diff_json'] or '{}')
                    self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                        'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                    self.assertEqual(json.loads(result['expected_json'])['source'], contracts.REASONS[feature])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_contracts_cannot_be_skipped_or_shape_only_passes(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in sorted(contracts.FEATURES):
            self.assertIn(feature, required_features())
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'implemented')
            for got in ({}, {'inventory': {'.java': 0}}, {'inventory': 'inapplicable'},
                        {'declared-output': {'groups': []}}):
                self.fixture._project_format_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'inventory': {'.java': 5, '.kt': 1, '.xml': 1},
                                   'declared-output': {'groups': [{'name': 'Synthetic'}]}}}, {feature: got})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._project_format_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        self.assertIn('global-selector', self.state.execute("SELECT reason FROM coverage WHERE feature='global:format'").fetchone()[0])

    def test_full_inventory_boundary_and_runtime_fingerprint(self):
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
            return content + b'\n# fixture edit\n' if path.name == 'project_format_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
