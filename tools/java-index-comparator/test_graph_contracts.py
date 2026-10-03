"""Exercise actual Java graph commands without asserting MCP equivalence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import graph_contracts


class GraphContractsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// inventory only\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('synthetic source is not MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_graph_family_executes_production_and_preserves_target(self):
        with patch('graph_contracts.exercise', wraps=graph_contracts.exercise) as exercise:
            for feature in sorted(graph_contracts.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                                 'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_graph_cannot_be_silently_classified_as_absent(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in graph_contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertTrue(coverage['reason'].startswith('independent source/state:'))
            self.fixture._graph_results = None
            with patch('graph_contracts.exercise', return_value=({feature: {'edge': 1}}, {feature: {'edge': 2}})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._graph_results = None
            with patch('graph_contracts.exercise', side_effect=ToolError('synthetic interrupted fixture')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='graph'").fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            graph_contracts.exercise(self.binary, self.directory.parent.parent.parent)

    def test_contract_edits_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed graph contract\n' if path.name == 'graph_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
