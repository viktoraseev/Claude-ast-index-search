"""Source/state VCS fixtures must execute CLI and preserve unresolved scope."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import vcs_contracts


class VcsContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('Git/source state is not an MCP oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_history_diffs_fallback_and_exact_exclusion_totals_execute_production(self):
        for feature in sorted(vcs_contracts.FEATURES):
            with self.subTest(feature=feature):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
                                 'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_disposable_contract_cannot_be_classified_absent(self):
        # The target need not be a Git project; the command is still applicable
        # to the Java repair, via disposable source/state fixtures.
        for feature in vcs_contracts.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            self.assertIn('not MCP equivalence', row['reason'])
            self.fixture._vcs_results = None
            with patch('vcs_contracts.exercise', return_value=({feature: {'value': 1}}, {feature: {'value': 2}})), \
                    patch('vcs_contracts.exclusion_budget', return_value=({}, {})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._vcs_results = None
            with patch('vcs_contracts.exercise', side_effect=ToolError('synthetic interrupted source fixture')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        status = self.state.execute("SELECT status FROM coverage WHERE feature='search:rank-presets'").fetchone()[0]
        self.assertEqual(status, 'pending')
        for exercise in (vcs_contracts.exercise, vcs_contracts.exclusion_budget):
            with self.assertRaisesRegex(ToolError, 'inside repository'):
                exercise(self.binary, self.root.parent.parent.parent.parent.parent)

    def test_vcs_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'vcs_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
