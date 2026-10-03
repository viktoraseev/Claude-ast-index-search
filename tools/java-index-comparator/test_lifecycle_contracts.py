"""Index mutations are tested on public disposable fixtures, never targets."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan
from common import ToolError, connect, stable_id
import lifecycle_contracts


class LifecycleContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('lifecycle has no corresponding MCP mutation oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)

    def test_plan_and_fixture_execute_the_whole_family_once_without_target_mutations(self):
        before = (self.root / 'Sentinel.java').read_bytes()
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Find classes\n  symbol  Find symbols\n  file  Find files\n', root=self.root)
        for feature in lifecycle_contracts.FEATURES:
            status, reason = self.state.execute('SELECT status,reason FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(status, 'implemented', feature)
            self.assertIn('not MCP equivalence', reason)
        with patch('lifecycle_contracts.exercise', wraps=lifecycle_contracts.exercise) as exercise:
            for feature in sorted(lifecycle_contracts.FEATURES):
                check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                self.assertIsNotNone(check, feature)
                self.fixture.evaluate(check)
                result = self.state.execute('SELECT verdict,error FROM checks WHERE id=?', (check['id'],)).fetchone()
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error']})
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual((self.root / 'Sentinel.java').read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())

    def test_artifact_boundary_is_checked_before_creating_or_mutating_anything(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            lifecycle_contracts.exercise(self.binary, Path('/'))

    def test_watcher_is_reaped_even_if_a_mid_watch_probe_fails(self):
        original = lifecycle_contracts.Runner.classes
        watched = []

        def interrupted(runner):
            if runner.status()[1] is True:
                watched.append(runner)
                raise ToolError('synthetic interruption after watch startup')
            return original(runner)

        with patch.object(lifecycle_contracts.Runner, 'classes', interrupted):
            with self.assertRaisesRegex(ToolError, 'synthetic interruption'):
                lifecycle_contracts.exercise(self.binary, self.directory / 'interrupted')
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0].status(), (1, False))

    def test_well_shaped_but_wrong_lifecycle_outcome_is_not_a_pass(self):
        subject = 'disposable-fixture'
        identity = stable_id({'feature': 'clear', 'subject': subject})
        with self.state:
            self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)', (identity, 'clear', subject))
        with patch('lifecycle_contracts.exercise', return_value=(
                {'clear': {'database-removed': True}}, {'clear': {'database-removed': False}})):
            self.fixture.evaluate(self.state.execute('SELECT * FROM checks WHERE id=?', (identity,)).fetchone())
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (identity,)).fetchone()[0], 'fail')


if __name__ == '__main__':
    unittest.main()
