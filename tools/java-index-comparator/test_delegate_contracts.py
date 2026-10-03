"""Production Java delegation is isolated from real external programs/settings."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan
from common import ToolError, connect
import delegate_contracts


class DelegateContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('delegation does not establish MCP parser equivalence')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        self.check = self.state.execute("SELECT * FROM checks WHERE feature='agrep'").fetchone()

    def test_production_arguments_fallback_exit_codes_and_target_preservation(self):
        self.fixture.evaluate(self.check)
        row = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (self.check['id'],)).fetchone()
        self.assertEqual(row['verdict'], 'pass', tuple(row))
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        reason = self.state.execute("SELECT reason FROM coverage WHERE feature='agrep'").fetchone()[0]
        self.assertTrue(reason.startswith('internal CLI:'), reason)

    def test_well_shaped_wrong_delegate_outcome_is_not_support(self):
        with patch('delegate_contracts.exercise', return_value=(
                {'agrep': {'failure': {'exit': 1}}}, {'agrep': {'failure': {'exit': 0}}})):
            self.fixture.evaluate(self.check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (self.check['id'],)).fetchone()[0], 'fail')

    def test_outside_artifact_boundary_and_interruption_never_pass(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            delegate_contracts.exercise(self.binary, Path('/'))
        with patch('delegate_contracts.exercise', side_effect=ToolError('synthetic interruption')):
            self.fixture.evaluate(self.check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (self.check['id'],)).fetchone()[0], 'error')
