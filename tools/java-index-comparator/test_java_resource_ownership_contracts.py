"""Production acceptance for dependency modes, metadata, types and roots."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from common import ToolError, connect
import java_resource_ownership_contracts as contracts
import android_dependency_contracts
from audit import Fixture, SCHEMA, next_check


class JavaResourceOwnershipTests(unittest.TestCase):
    def test_related_ownership_criteria_use_authored_expected_results(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            expected, actual = contracts.exercise(binary, Path(temporary))
        for label, want in expected[contracts.FEATURE].items():
            with self.subTest(label=label):
                self.assertEqual(want, actual[contracts.FEATURE][label])

    def test_artifact_boundary_is_enforced(self):
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(Path('target/release/ast-index'), Path('/private/tmp'))

    def test_java_projection_executes_without_legacy_xml_fixture_and_keeps_bad_results_red(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'read-only-target'
            root.mkdir()
            (root / 'Sentinel.java').write_text('class Sentinel {}')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            state.execute("INSERT INTO metadata VALUES ('audit_scope','java')")
            android_dependency_contracts.plan_dependencies(state, root, java_only=True)
            oracle = Mock()
            oracle.call.side_effect = AssertionError('independent projection cannot call MCP')
            fixture = Fixture(root, binary, directory / 'unused.sqlite', state, oracle)
            with patch.object(android_dependency_contracts, 'exercise', side_effect=AssertionError('Java cycle must not execute XML-only criteria')):
                while (check := next_check(state)) is not None:
                    fixture.evaluate(check)
                    row = state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                    self.assertEqual(row['verdict'], 'pass', row['error'] or row['diff_json'])
            oracle.call.assert_not_called()
            self.assertEqual([p.name for p in root.iterdir()], ['Sentinel.java'])
            feature = 'resource-usages:java-namespace-ownership'
            fixture._android_dependency_java_results = ({feature: {'shared-java': {'locations': [('app/Use.java', 1)]}}},
                                                         {feature: {'shared-java': {'locations': []}}})
            check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            fixture.evaluate(check)
            self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')


if __name__ == '__main__':
    unittest.main()
