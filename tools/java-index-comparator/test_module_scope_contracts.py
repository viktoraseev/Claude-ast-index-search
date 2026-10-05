"""Executed Java module directory regressions with independent expectations."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import module_scope_contracts as contracts


class ModuleScopeTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('module directory scope is not MCP truth')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def test_production_five_commands_share_literal_directory_scope(self):
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
        self.assertFalse(self.fixture.database.exists())

    def test_applicable_family_cannot_be_skipped_or_claim_mcp_or_parent_coverage(self):
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertIn('not MCP equivalence', coverage['reason'])
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'identity': 'inapplicable'}, {'identity': 'wrong'}, {'identity': 'correct'}):
                self.fixture._module_scope_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'identity': 'correct'}}, {feature: observed})):
                    self.fixture.evaluate(check)
                verdict = self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0]
                self.assertEqual(verdict, 'pass' if observed == {'identity': 'correct'} else 'fail')
            self.fixture._module_scope_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:scope-command-matrix'").fetchone()[0], 'pending')
        self.assertIn('attached-root module graphs', self.state.execute(
            "SELECT reason FROM coverage WHERE feature='global:scope-command-matrix'").fetchone()[0])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_incomplete_all_type_inventory_is_error_not_absence(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)

    def test_route_text_keeps_dotted_and_path_module_identities(self):
        result = {'empty_reason': None, 'paths': [{'length': 1, 'hops': [
            {'from': 'scope_.app', 'to': 'scope%/live', 'kind': 'compile'}]}], 'truncated': False}
        text = ('scope_.app → scope%/live (1 path, shortest = 1 hop)\n\n'
                '  Path 1 (1 hop):\n    scope_.app → scope%/live [compile]\n')
        self.assertEqual(contracts.route_contracts.text_observation(text, result), {
            'hops': [('scope_.app', 'scope%/live', 'compile')], 'lengths': [1],
            'reason': True, 'count': 1, 'ansi': False})


if __name__ == '__main__':
    unittest.main()
