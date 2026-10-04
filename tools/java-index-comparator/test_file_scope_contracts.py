"""Shared root scope fixtures must execute source behavior and retain gaps."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import file_scope_contracts


class FileScopeContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('file scope has no MCP equivalent')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        self.feature = next(iter(file_scope_contracts.FEATURES))
        self.check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()

    def evaluate(self):
        self.fixture.evaluate(self.check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (self.check['id'],)).fetchone()

    def test_actual_production_scope_and_rendering_preserve_target(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
            'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual([p.name for p in self.root.iterdir()], ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_java_scope_cannot_be_skipped_or_pass_empty_output(self):
        self.assertIn(self.feature, required_features())
        row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (self.feature,)).fetchone()
        self.assertEqual(row['status'], 'implemented')
        self.assertTrue(row['reason'].startswith('independent source/state:'))
        self.assertIn('not MCP equivalence', row['reason'])
        for actual in ({}, {'api': []}, {'api': [('Wrong.java', 99)]}):
            self.fixture._file_scope_results = None
            with patch('file_scope_contracts.exercise', return_value=(
                    {self.feature: {'api': [('View.java', 3)]}}, {self.feature: actual})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        with patch('file_scope_contracts.exercise', side_effect=ToolError('synthetic incomplete inventory')):
            self.fixture._file_scope_results = None
            self.assertEqual(self.evaluate()['verdict'], 'error')
        for feature in ('global:scope-command-matrix', 'global:format'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                              (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            file_scope_contracts.exercise(self.fixture.binary, Path('/private/tmp'))


if __name__ == '__main__':
    unittest.main()
