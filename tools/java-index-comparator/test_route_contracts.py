"""Small source-driven module-route regressions, isolated from target and MCP."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import route_contracts


class RouteContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
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
        oracle.call.side_effect = AssertionError('module routes have no MCP equivalence contract')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_source_graph_path_depth_kind_caps_and_timeouts(self):
        result = self.evaluate('module-route:budgets')
        # Failure messages contain only aggregates; logs stay in artifacts.
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
                         'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_rendered_hop_identities_and_partial_results(self):
        result = self.evaluate('module-route:rendering')
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
                         'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})

    def test_absence_does_not_skip_applicable_source_fixture_or_count_as_mcp(self):
        for feature in route_contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            self.assertIn('not MCP equivalence', row['reason'])
            with patch('route_contracts.exercise', return_value=({'count': 0}, {'count': 1})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            with patch('route_contracts.exercise', side_effect=ToolError('synthetic interrupted inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            route_contracts.exercise(self.binary, self.root.parent.parent.parent.parent.parent,
                                     'module-route:budgets')

    def test_independent_self_edge_enumerator_respects_depth(self):
        self.assertEqual(route_contracts.module_contracts.paths(route_contracts.EDGES, 's', 's', 0, 'all'), [])
        self.assertEqual(route_contracts.module_contracts.paths(route_contracts.EDGES, 's', 's', 1, 'all'),
                         [(('s', 's', 'compile'),)])


if __name__ == '__main__':
    unittest.main()
