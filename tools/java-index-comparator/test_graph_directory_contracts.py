"""Java graph directory checks execute production and preserve coverage gaps."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import graph_directory_contracts as contracts


class GraphDirectoryTests(unittest.TestCase):
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
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('source contracts cannot claim MCP equivalence')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  graph  Symbol graph', root=self.root, java_only=True)

    def check(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (next(iter(contracts.FEATURES)),)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_directory_intersections_traversal_pages_and_text(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            result = self.check()
            diff = json.loads(result['diff_json'] or '{}')
            self.assertEqual(result['verdict'], 'pass', {'error': result['error'],
                'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
            self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.check()
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_java_contract_cannot_be_silently_inapplicable(self):
        self.assertEqual(self.state.execute('SELECT count(distinct extension) FROM file_inventory').fetchone()[0], 2)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
            for observed in ({}, {'value': 'inapplicable'}, {'value': 'wrong'}):
                self.fixture._graph_directory_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'value': 'correct'}}, {feature: observed})):
                    self.assertEqual(self.check()['verdict'], 'fail')
            self.fixture._graph_directory_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('interrupted fixture')):
                self.assertEqual(self.check()['verdict'], 'error')
        for feature in ('graph', 'global:scope-command-matrix', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# directory edit\n' if path.name == 'graph_directory_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
