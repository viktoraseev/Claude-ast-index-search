"""Compact production regressions for Java file view formats, without MCP claims."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import file_view_contracts


class FileViewContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        oracle = Mock()
        oracle.call.side_effect = AssertionError('file views are independent source/state coverage')
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        self.feature = next(iter(file_view_contracts.FEATURES))
        self.check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()

    def evaluate(self):
        self.fixture.evaluate(self.check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (self.check['id'],)).fetchone()

    def test_actual_production_file_import_outline_and_api_views(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'],
            'error': result['error'], 'missing': len(diff.get('missing', [])),
            'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual([p.name for p in self.root.iterdir()], ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_source_cannot_pass_with_empty_or_wrong_behavior(self):
        self.assertIn(self.feature, required_features())
        coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (self.feature,)).fetchone()
        self.assertEqual(coverage['status'], 'implemented')
        self.assertTrue(coverage['reason'].startswith('independent source/state:'))
        self.assertIn('not MCP equivalence', coverage['reason'])
        for observed in ([], [{'path': 'Wrong.java', 'line': 99, 'content': 'fake'}]):
            self.fixture._file_view_results = None
            with patch('file_view_contracts.exercise', return_value=(
                    {self.feature: {'api': [{'path': 'View.java', 'line': 7, 'content': 'public class View {'}]}},
                    {self.feature: {'api': observed}})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._file_view_results = None
        with patch('file_view_contracts.exercise', side_effect=ToolError('synthetic incomplete inventory')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            file_view_contracts.exercise(self.fixture.binary, Path('/private/tmp'))


if __name__ == '__main__':
    unittest.main()
