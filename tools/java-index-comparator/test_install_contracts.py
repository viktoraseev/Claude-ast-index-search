"""Installation verification uses production and owned fixtures, never real external CLIs."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan
from common import ToolError, connect
import install_contracts


class InstallationContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('installation has no MCP equivalent')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def test_whole_installation_family_runs_once_and_preserves_target(self):
        before = (self.root / 'Sentinel.java').read_bytes()
        with patch('install_contracts.exercise', wraps=install_contracts.exercise) as exercise:
            for feature in sorted(install_contracts.FEATURES):
                with self.subTest(feature=feature):
                    coverage = self.state.execute('SELECT status,reason FROM coverage WHERE feature=?', (feature,)).fetchone()
                    self.assertEqual(coverage['status'], 'implemented')
                    self.assertIn('not MCP equivalence', coverage['reason'])
                    check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                    self.fixture.evaluate(check)
                    result = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                    self.assertEqual(result['verdict'], 'pass', tuple(result))
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual((self.root / 'Sentinel.java').read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_wrong_but_well_shaped_outcome_cannot_pass(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='install-claude-plugin'").fetchone()
        with patch('install_contracts.exercise', return_value=(
                {'install-claude-plugin': {'failed-child': {'exit': 1}}},
                {'install-claude-plugin': {'failed-child': {'exit': 0}}})):
            self.fixture.evaluate(check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')

    def test_boundaries_and_interruption_cannot_create_passing_coverage(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            install_contracts.exercise(self.binary, Path('/'))
        with patch('install_contracts.exercise', side_effect=ToolError('synthetic interruption')) as exercise:
            for feature in sorted(install_contracts.FEATURES):
                check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                self.fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
            self.assertEqual(exercise.call_count, 1)


if __name__ == '__main__':
    unittest.main()
