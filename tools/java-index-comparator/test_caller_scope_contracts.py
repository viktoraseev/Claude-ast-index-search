"""One production fixture exercises the related Java caller-scope contracts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import caller_scope_contracts as contracts


class CallerScopeTests(unittest.TestCase):
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
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('source scope checks must not call MCP')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_shared_fixture_scope_before_limits(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_sources_cannot_be_skipped_and_gaps_remain_pending(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertTrue(coverage['reason'].startswith('independent source/state:'))
            for observed in ({}, {'inventory:project': {'.java': 0}}, {'inventory:project': 'inapplicable'}):
                self.fixture._caller_scope_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'inventory:project': {'.java': 4, '.kt': 1, '.xml': 1}}},
                        {feature: observed})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._caller_scope_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('synthetic incomplete inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('global:scope-command-matrix', 'global:format', 'call-tree:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.fixture.binary, Path('/private/tmp'))

    def test_tree_normalization_keeps_duplicates_and_unknown_owners(self):
        runner = Mock()
        runner.path.side_effect = lambda value: value
        text = '    ← wrong (Probe.java:3)\n    ← wrong (Probe.java:3)\n'
        self.assertEqual(contracts.tree_rows(text, runner), [(1, 'Probe.java', 3, 'wrong')] * 2)
        for text in ('    ← owner (missing location)\n', '     ← owner (Probe.java:3)\n'):
            with self.assertRaises(ToolError):
                contracts.tree_rows(text, runner)

    def test_adapter_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# fixture edit\n' if path.name == 'caller_scope_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
