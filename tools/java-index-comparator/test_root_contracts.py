"""Root contracts execute production in bounded disposable Java fixtures."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import root_contracts


class RootContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('native root configuration has no MCP equivalent')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def test_whole_family_executes_once_and_preserves_target(self):
        before = (self.root / 'Sentinel.java').read_bytes()
        with patch('root_contracts.exercise', wraps=root_contracts.exercise) as exercise:
            for feature in sorted(root_contracts.FEATURES):
                with self.subTest(feature=feature):
                    coverage = self.state.execute('SELECT status,reason FROM coverage WHERE feature=?', (feature,)).fetchone()
                    self.assertEqual(coverage['status'], 'implemented')
                    self.assertIn('not MCP equivalence', coverage['reason'])
                    check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                    self.assertIsNotNone(check)
                    self.fixture.evaluate(check)
                    result = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                    self.assertEqual(result['verdict'], 'pass', tuple(result))
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual((self.root / 'Sentinel.java').read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:scope-command-matrix'").fetchone()[0], 'pending')
        self.assertIn('global:scope-command-matrix', required_features())

    def test_well_shaped_wrong_or_empty_outcome_cannot_pass(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='global:subtree'").fetchone()
        for observed in ({'named': {'file:1': {'returned': 0}}}, {}):
            with self.subTest(observed=observed):
                self.fixture._root_results = None
                with patch('root_contracts.exercise', return_value=(
                        {'global:subtree': {'named': {'file:1': {'returned': 1}}}},
                        {'global:subtree': observed})):
                    self.fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')

    def test_applicable_java_root_contracts_are_never_inventory_skipped(self):
        for feature in root_contracts.FEATURES:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'implemented')
            self.assertEqual(self.state.execute('SELECT count(*) FROM checks WHERE feature=?', (feature,)).fetchone()[0], 1)
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 1)

    def test_boundary_is_checked_before_mutations_and_failure_is_retained(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            root_contracts.exercise(self.binary, Path('/'))
        with patch('root_contracts.exercise', side_effect=ToolError('synthetic interruption')) as exercise:
            for feature in sorted(root_contracts.FEATURES):
                check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                self.fixture.evaluate(check)
                self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
            self.assertEqual(exercise.call_count, 1)

    def test_contract_changes_invalidate_resume_epoch(self):
        original = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed root contract\n' if path.name == 'root_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), original)


if __name__ == '__main__':
    unittest.main()
