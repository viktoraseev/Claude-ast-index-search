"""The pending composition contract must execute production, never fake MCP evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import stack_contracts


class StackContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('source/state composition is not MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)
        self.feature = next(iter(stack_contracts.FEATURES))

    def evaluate(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()
        self.assertIsNotNone(check, 'the formerly pending family has no executable check')
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_composition_and_every_budget_execute_production(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
                         'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        expected = json.loads(result['expected_json'])['samples']
        for label in ('separate-modules', 'kmp-java', 'kmp-web', 'marker-cap', 'depth-7', 'depth-8',
                      'entry-exact', 'entry-over', 'gradle-file-exact', 'gradle-file-over',
                      'gradle-byte-exact', 'gradle-byte-over', 'gradle-single-file-over',
                      'gradle-default-single-file-exact', 'gradle-default-single-file-over'):
            self.assertIn(label + ':json', expected)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_mismatch_and_interruption_cannot_become_a_pass_or_absence(self):
        self.assertIn(self.feature, required_features())
        row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (self.feature,)).fetchone()
        self.assertEqual(row['status'], 'implemented')
        self.assertTrue(row['reason'].startswith('independent source/state:'))
        self.assertIn('not MCP equivalence', row['reason'])
        with patch('stack_contracts.exercise', return_value=({'flags': True}, {'flags': False})):
            self.assertEqual(self.evaluate()['verdict'], 'fail')
        with patch('stack_contracts.exercise', side_effect=ToolError('synthetic interrupted fixture')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            stack_contracts.exercise(self.binary, self.root.parent.parent.parent.parent.parent)

    def test_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'stack_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
