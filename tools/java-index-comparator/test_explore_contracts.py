"""Exploration contracts execute production without substituting native DB truth."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import explore_contracts


class ExplorationContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('authored source is not MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        self.plan()

    def plan(self):
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', ('explore:ranking-budgets',)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_bounded_java_exploration_executes_production(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'],
                         'error': result['error'], 'missing': len(diff.get('missing', [])),
                         'unexpected': len(diff.get('unexpected', []))})
        self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_contract_cannot_be_silently_skipped(self):
        (self.root / 'Foreign.kt').write_text('// inventory only, outside parser scope\n')
        self.plan()
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        feature = 'explore:ranking-budgets'
        self.assertIn(feature, required_features())
        self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'implemented')
        with patch('explore_contracts.exercise', return_value=({feature: {'nearest': True}}, {feature: {'nearest': False}})):
            self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._explore_results = None
        with patch('explore_contracts.exercise', side_effect=ToolError('synthetic fixture interrupted')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            explore_contracts.exercise(self.binary, self.directory.parent.parent.parent)

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'explore_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
