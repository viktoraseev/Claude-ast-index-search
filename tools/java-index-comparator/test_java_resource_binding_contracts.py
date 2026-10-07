"""Java resource binding acceptance and honest applicability evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, required_features
from common import ToolError, connect
import java_resource_binding_contracts as contracts
import java_resource_contracts
import mobile_contracts


class JavaResourceBindingContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.state.execute("INSERT INTO coverage VALUES ('android:syntax-resolution','pending','retained obligation')")
        java_resource_contracts.plan_java_resources(self.state, self.root)
        oracle = self.oracle = Mock()
        oracle.call.side_effect = AssertionError('independent resource bindings must not call MCP')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused.sqlite', self.state, oracle)
        self.check = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()

    def verdict(self):
        self.fixture.evaluate(self.check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (self.check['id'],)).fetchone()

    def test_whole_lexical_family_executes_production_and_keeps_parent_pending(self):
        row = self.verdict()
        self.assertEqual(row['verdict'], 'pass', row['diff_json'] or row['error'])
        self.assertEqual(json.loads(row['expected_json'])['source'], contracts.REASON)
        self.oracle.call.assert_not_called()
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(list(self.root.iterdir()), [self.root / 'Sentinel.java'])
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='android:syntax-resolution'").fetchone()[0], 'pending')
        self.assertIn(contracts.FEATURE, required_features())
        old = [tuple(r) for r in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')]
        java_resource_contracts.plan_java_resources(self.state, self.root)
        self.assertEqual(old, [tuple(r) for r in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')])

    def test_missing_or_inapplicable_binding_output_is_a_failure(self):
        for actual in ({}, {'scope': 'inapplicable'}, {'locations': []}):
            self.fixture._java_resource_results = None
            with patch.object(java_resource_contracts, 'exercise', return_value=(
                    {contracts.FEATURE: {'locations': [('app/Use.java', 3)]}},
                    {contracts.FEATURE: actual})):
                self.assertEqual(self.verdict()['verdict'], 'fail')

    def test_fixture_cannot_skip_incomplete_inventory_or_escape_artifacts(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))
        original = mobile_contracts.inventory
        def incomplete(state, root):
            original(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'full inventory incomplete'):
                contracts.exercise(self.binary, self.directory)

    def test_present_resources_cannot_be_silently_classified_inapplicable(self):
        with patch.object(contracts, 'applicability', return_value=('inapplicable', 'synthetic defect')):
            expected, actual = contracts.exercise(self.binary, self.directory)
        self.assertEqual(expected[contracts.FEATURE]['applicability'], 'pending')
        self.assertEqual(actual[contracts.FEATURE]['applicability'], 'inapplicable')
        self.fixture._java_resource_results = expected, actual
        self.assertEqual(self.verdict()['verdict'], 'fail')


if __name__ == '__main__':
    unittest.main()
