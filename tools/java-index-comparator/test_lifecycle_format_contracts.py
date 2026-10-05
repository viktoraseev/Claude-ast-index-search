"""Small executed format contracts; mutations stay in disposable artifacts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, canonical_json, connect
import lifecycle_format_contracts as contracts


class LifecycleFormatTests(unittest.TestCase):
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
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_lifecycle_formats_and_source_state(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        mismatches = [key for key in expected[contracts.FEATURE]
                      if canonical_json(expected[contracts.FEATURE][key]) !=
                      canonical_json(actual[contracts.FEATURE].get(key))]
        self.assertEqual(mismatches, [])

    def readiness_probe(self, first=b'', later=None, exited=False, budget=1024):
        """Schedule lock acquisition before observable stdout, without wall-clock races."""
        log = self.directory / 'readiness.log'
        log.write_bytes(first)
        runner = Mock(root=self.root, output_budget=budget)
        runner.json.return_value = {'watching': True}
        child = Mock()
        child.poll.return_value = 0 if exited else None
        elapsed = 0

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds
            if later is not None:
                log.write_bytes(later)

        with patch.object(contracts.time, 'monotonic', side_effect=lambda: elapsed), \
                patch.object(contracts.time, 'sleep', side_effect=sleep):
            return contracts.watch_readiness(runner, child, log, 1)

    def test_lock_acquisition_before_readiness_output_is_not_a_flush_failure(self):
        ready = (json.dumps({'command': 'watch', 'status': 'watching',
                             'root': str(self.root)}) + '\n').encode()
        self.assertTrue(self.readiness_probe(later=ready))
        # A nonempty partial write is not yet a complete JSON-line event.
        self.assertTrue(self.readiness_probe(first=ready[:20], later=ready))

    def test_missing_partial_or_wrong_readiness_cannot_pass(self):
        ready = json.dumps({'command': 'watch', 'status': 'watching', 'root': str(self.root)}).encode()
        for first in (b'', ready, b'invalid\n', b'{}\n',
                      b'{"command":"watch","status":"updated"}\n',
                      b'{"command":"watch","status":"watching","root":"other"}\n'):
            with self.subTest(first=first):
                self.assertFalse(self.readiness_probe(first=first))
        with self.assertRaisesRegex(ToolError, 'readiness'):
            self.readiness_probe(first=ready + b'\n', exited=True)
        with self.assertRaisesRegex(ToolError, 'budget'):
            self.readiness_probe(first=b'x' * 65, budget=64)

    def test_audit_keeps_applicable_wrong_or_missing_results_unresolved(self):
        oracle = Mock()
        oracle.call.side_effect = AssertionError('lifecycle formats have no MCP oracle')
        fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        feature = contracts.FEATURE
        self.assertIn(feature, required_features())
        coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
        self.assertEqual(coverage['status'], 'implemented')
        self.assertIn('not MCP equivalence', coverage['reason'])
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        for actual in ({'inventory': {'.java': 1, '.kt': 1}}, {}, {'inventory': 'inapplicable'},
                       {'inventory': {'.java': 0}}, {'inventory': {'.java': 1}}):
            fixture._lifecycle_format_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {feature: {'inventory': {'.java': 1, '.kt': 1}}}, {feature: actual})):
                fixture.evaluate(check)
            row = self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
            self.assertEqual(row['verdict'], 'pass' if actual == {'inventory': {'.java': 1, '.kt': 1}} else 'fail')
            self.assertEqual(json.loads(row['expected_json'])['source'], contracts.REASON)
        fixture._lifecycle_format_results = None
        with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
            fixture.evaluate(check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertFalse(fixture.database.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])

    def test_boundary_and_incomplete_inventory_fail_before_mutations(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)


if __name__ == '__main__':
    unittest.main()
