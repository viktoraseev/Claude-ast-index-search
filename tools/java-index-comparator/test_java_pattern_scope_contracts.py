"""Pattern-flow contracts execute production and retain independent evidence labels."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_pattern_scope_contracts as scopes


class PatternScopeContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// inventory only\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('source contracts must not claim MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_shared_fixture_checks_pattern_scopes_through_all_consumers(self):
        with patch.object(scopes, 'exercise', wraps=scopes.exercise) as exercise:
            for feature in sorted(scopes.FEATURES):
                with self.subTest(feature=feature):
                    result = self.evaluate(feature)
                    diff = json.loads(result['diff_json'] or '{}')
                    self.assertEqual(result['verdict'], 'pass',
                                     {'feature': feature, 'error': result['error'],
                                      'missing': len(diff.get('missing', [])),
                                      'unexpected': len(diff.get('unexpected', []))})
                    self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
                    samples = json.loads(result['expected_json'])['samples']
                    if feature == scopes.GRAPH:
                        for label, _, _ in scopes.continuation_cases('Item', 'slot', 'slot.itemMarker()', 'slot.decoyMarker()'):
                            self.assertIn('continuation:' + label + ':True:100', samples)
                            self.assertIn('continuation:' + label + ':path', samples)
                    else:
                        for offset in range(0, len(scopes.continuation_cases('Item', 'slot', 'slot.itemMarker()', 'slot.decoyMarker()')), 5):
                            for owner in ('Item', 'Decoy'):
                                key = f'continuation:{offset}:{owner}'
                                self.assertIn(key + ':100' if feature == scopes.TREE else key, samples)
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_contracts_cannot_be_silently_skipped(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in scopes.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            with patch.object(scopes, 'exercise', return_value=({feature: {'edges': []}},
                                                               {feature: {'edges': 'inapplicable'}})):
                self.fixture._pattern_scope_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            with patch.object(scopes, 'exercise', side_effect=ToolError('synthetic interrupted fixture')):
                self.fixture._pattern_scope_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('graph', 'call-tree:semantic-resolution', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            scopes.exercise(self.binary, self.directory.parent.parent.parent)

    def test_incomplete_inventory_cannot_establish_applicability(self):
        inventory = scopes.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(scopes.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                scopes.exercise(self.binary, self.directory)

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# pattern edit\n' if path.name == 'java_pattern_scope_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)

    def test_planning_preserves_completed_evidence_and_gap_reasons(self):
        feature = scopes.GRAPH
        with patch.object(scopes, 'exercise', return_value=({feature: {'sites': 1}}, {feature: {'sites': 1}})):
            result = self.evaluate(feature)
        before = [tuple(row) for row in self.state.execute('SELECT * FROM coverage ORDER BY feature')]
        scopes.plan_scopes(self.state, self.root)
        scopes.plan_scopes(self.state, self.root)
        self.assertEqual(before, [tuple(row) for row in self.state.execute('SELECT * FROM coverage ORDER BY feature')])
        row = self.state.execute('SELECT verdict FROM checks WHERE id=?', (result['id'],)).fetchone()
        self.assertEqual(row['verdict'], 'pass')


if __name__ == '__main__':
    unittest.main()
