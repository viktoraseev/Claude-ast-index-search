"""Compact public Java production regressions for navigation formats."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import navigation_format_contracts as contracts


class NavigationFormatContractsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
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
        oracle.call.side_effect = AssertionError('navigation formats are not MCP equivalence')
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)
        self.feature = next(iter(contracts.FEATURES))
        self.check = self.state.execute('SELECT * FROM checks WHERE feature=?', (self.feature,)).fetchone()

    def evaluate(self):
        self.fixture.evaluate(self.check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (self.check['id'],)).fetchone()

    def test_production_navigation_formats_pages_and_root_identities(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'],
            'error': result['error'], 'missing': len(diff.get('missing', [])),
            'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual([p.name for p in self.root.iterdir()], ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_navigation_cannot_be_skipped_or_claim_parent_coverage(self):
        self.assertIn(self.feature, required_features())
        coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (self.feature,)).fetchone()
        self.assertEqual(coverage['status'], 'implemented')
        self.assertTrue(coverage['reason'].startswith('independent source/state:'))
        self.assertIn('not MCP equivalence', coverage['reason'])
        for observation in ({'children': []}, {'children': [{'path': 'Wrong.java', 'line': 99}]}):
            self.fixture._navigation_format_results = None
            with patch('navigation_format_contracts.exercise', return_value=(
                    {self.feature: {'children': [{'path': 'Node.java', 'line': 3}]}},
                    {self.feature: observation})):
                self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._navigation_format_results = None
        with patch('navigation_format_contracts.exercise', side_effect=ToolError('synthetic incomplete inventory')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.fixture.binary, Path('/private/tmp'))

    def test_page_preserves_duplicate_identities_and_rejects_empty_or_wrong_sites(self):
        wanted = [('Node.java', 3), ('Node.java', 3)]
        pagination = contracts.expected_page(wanted, 100)['pagination']
        self.assertEqual(contracts.page(wanted, wanted, pagination, 100), contracts.expected_page(wanted, 100))
        self.assertFalse(contracts.page(wanted[:1], wanted, pagination, 100)['complete'])
        self.assertFalse(contracts.page([('Other.java', 99)], wanted, pagination, 1)['identities'])
        self.assertFalse(contracts.page([], wanted, pagination, 100)['complete'])
        self.assertNotEqual(contracts.page(wanted, wanted, {**pagination, 'total': True}, 100),
                            contracts.expected_page(wanted, 100))
        self.assertNotEqual(contracts.page(wanted, wanted, {**pagination, 'truncated': 0}, 100),
                            contracts.expected_page(wanted, 100))


if __name__ == '__main__':
    unittest.main()
