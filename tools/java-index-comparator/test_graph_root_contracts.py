"""Executed root-induced Java graphs and audit honesty guards."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import graph_root_contracts as contracts


class GraphRootTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('graph root state is not MCP truth')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  graph  Symbol graph', root=self.root, java_only=True)

    def test_production_family_filters_roots_before_traversal_and_pages(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in contracts.FEATURES:
                check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                self.fixture.evaluate(check)
                result = self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'error': result['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.fixture.evaluate(check)
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertEqual((self.root / 'Inventory.kt').read_text(), '// inventory only\n')
        self.assertFalse(self.fixture.database.exists())

    def test_applicable_family_cannot_be_skipped_or_claim_mcp_or_parent_coverage(self):
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'identity': 'inapplicable'}, {'identity': 'wrong'}, {'identity': 'correct'}):
                self.fixture._graph_root_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'identity': 'correct'}}, {feature: observed})):
                    self.fixture.evaluate(check)
                verdict = self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0]
                self.assertEqual(verdict, 'pass' if observed == {'identity': 'correct'} else 'fail')
            self.fixture._graph_root_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        for feature in ('global:scope-command-matrix', 'graph'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_full_inventory_is_error_not_absence(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)

    def test_changed_contract_invalidates_saved_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed graph roots\n' if path.name == 'graph_root_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
