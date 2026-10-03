"""Caller/context checks execute production on small public-safe Java source."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import context_contracts


class ContextContractsTests(unittest.TestCase):
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
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_java_calls_and_context_execute_production_once_for_the_family(self):
        with patch('context_contracts.exercise', wraps=context_contracts.exercise) as exercise:
            for feature in sorted(context_contracts.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'verdict': result['verdict'],
                                 'error': result['error'], 'missing': len(diff.get('missing', [])),
                                 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_features_and_failures_cannot_be_hidden_as_absence(self):
        # A target with no calls still needs an executable contract; neither
        # missing build/framework markers nor foreign files make it inapplicable.
        (self.root / 'Foreign.kt').write_text('// inventory only; parser is out of scope\n')
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in context_contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            self.fixture._context_results = None
            with patch('context_contracts.exercise', return_value=({feature: {'owner': 1}}, {feature: {'owner': 2}})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._context_results = None
            with patch('context_contracts.exercise', side_effect=ToolError('synthetic interrupted fixture')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in context_contracts.PENDING:
            self.assertIn(feature, required_features())
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            context_contracts.exercise(self.binary, self.directory.parent.parent.parent)

    def test_contract_edits_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'context_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
