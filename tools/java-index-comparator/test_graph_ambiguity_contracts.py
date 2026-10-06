"""Production graph ambiguity and audit honesty on compact Java sources."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import graph_ambiguity_contracts as contracts


class GraphAmbiguityTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_ambiguity_pages_rendering_and_zero_hop_paths(self):
        root, state, fixture = self.fixture()
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                fixture.evaluate(check)
                result = state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
                fixture.evaluate(check)
            self.assertEqual(exercise.call_count, 1)
        self.assertFalse(fixture.database.exists())
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['Inventory.xml', 'Sentinel.java'])
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def fixture(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (root / 'Inventory.xml').write_text('<inventory/>\n')
        state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('independent graph contracts are not MCP equivalence')
        fixture = Fixture(root, self.binary, self.directory / 'unused-index', state, oracle)
        plan(state, [{'path': 'Sentinel.java'}], '  graph  Symbol graph', root=root, java_only=True)
        return root, state, fixture

    def test_executable_contracts_cannot_skip_or_claim_parent_or_mcp_coverage(self):
        root, state, fixture = self.fixture()
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertIn('not MCP equivalence', row['reason'])
            check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'identity': 'inapplicable'}, {'identity': 'wrong'}, {'identity': 'correct'}):
                fixture._graph_ambiguity_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'identity': 'correct'}}, {feature: observed})):
                    fixture.evaluate(check)
                result = state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                self.assertEqual(result['verdict'], 'pass' if observed == {'identity': 'correct'} else 'fail')
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            fixture._graph_ambiguity_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('interrupted fixture')):
                fixture.evaluate(check)
            self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        for feature in ('graph', 'global:format', 'global:scope-command-matrix'):
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(state.execute('SELECT count(distinct extension) FROM file_inventory').fetchone()[0], 2)
        self.assertFalse(fixture.database.exists())
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['Inventory.xml', 'Sentinel.java'])
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_inventory_is_error_and_contract_edits_invalidate_evidence(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        previous, read = adapter_digest(), Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed ambiguity contract\n' if path.name == 'graph_ambiguity_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
