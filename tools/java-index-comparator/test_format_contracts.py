"""Java format checks exercise the production CLI; oracle equivalence is separate."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect
import format_contracts
from root_contracts import Runner


class FormatContractsTests(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('format contracts are not MCP equivalence')
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_formats_locations_metadata_limits_and_root_ownership(self):
        for feature in sorted(format_contracts.FEATURES):
            with self.subTest(feature=feature):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'],
                    'error': result['error'], 'missing': len(diff.get('missing', [])),
                    'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_fixture_cannot_be_skipped_or_claim_parent_or_mcp_coverage(self):
        for feature in format_contracts.FEATURES:
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertTrue(coverage['reason'].startswith('independent source/state:'))
            self.assertIn('not MCP equivalence', coverage['reason'])
            self.fixture._format_results = None
            with patch('format_contracts.exercise', return_value=({feature: {'sites': 1}}, {feature: {'sites': 0}})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._format_results = None
            with patch('format_contracts.exercise', side_effect=ToolError('synthetic interrupted inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            format_contracts.exercise(self.fixture.binary, Path('/private/tmp'))

    def test_json_shape_or_wrong_source_identity_does_not_pass(self):
        runner = Runner(self.fixture.binary, self.directory)
        candidate = format_contracts.rows('annotations', ['project/Probe.java'])
        item = {**candidate[0], 'path': 'Probe.java'}
        def observe(rows, count=1):
            return format_contracts.observe(json.dumps({'items': rows, 'count': count}),
                'json', 'annotations', runner, candidate, 100)
        self.assertTrue(observe([item])['complete'])
        self.assertFalse(observe([{**item, 'line': 99}])['identities'])
        self.assertFalse(observe([{**item, 'content': 'unrelated'}])['identities'])
        self.assertFalse(observe([{**item, 'path': '[attached] Probe.java'}])['paths'])
        self.assertFalse(observe([item, item], 2)['identities'])
        self.assertFalse(observe([], 0)['complete'])
        self.assertFalse(observe([item], True)['metadata'])

    def test_same_line_provider_names_and_multiplicity_are_preserved(self):
        runner = Runner(self.fixture.binary, self.directory / 'same-line')
        runner.root.mkdir(parents=True)
        (runner.root / '.git').mkdir()
        (runner.root / 'Factory.java').write_text(
            'class Factory { @Provides Widget first() { return null; } '
            '@Binds Widget second(Widget value) { return value; } }\n')
        for limit in (0, 1, 100):
            output = runner.json('provides', 'Widget', '--limit', str(limit))
            self.assertEqual(output['count'], min(2, limit))
            self.assertEqual([(r['path'], r['line'], r['name']) for r in output['items']],
                             [('Factory.java', 1, 'first'), ('Factory.java', 1, 'second')][:limit])


if __name__ == '__main__':
    unittest.main()
