"""Executable Java dependency identity contracts and honest coverage labels."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_dependency_contracts as contracts
import mobile_contracts
import unused_dep_contracts


class JavaDependencyContracts(unittest.TestCase):
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
        self.oracle = Mock()
        self.oracle.call.side_effect = AssertionError('authored source contracts have no MCP oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'target.sqlite', self.state, self.oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_whole_import_type_family_runs_production_once(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                row = self.evaluate(feature)
                diff = json.loads(row['diff_json'] or '{}')
                self.assertEqual(row['verdict'], 'pass', {
                    'feature': feature, 'error': row['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
                self.assertIn(feature, required_features())
            self.assertEqual(exercise.call_count, 1)
        self.oracle.call.assert_not_called()
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertEqual((self.root / 'Sentinel.java').read_text(), 'class Sentinel {}\n')

    def test_bad_production_results_are_failures_and_semantics_stay_pending(self):
        expected = {f: {'sample': {'owner': 'alpha'}} for f in contracts.FEATURES}
        actual = {f: {'sample': {'owner': 'beta'}} for f in contracts.FEATURES}
        with patch.object(contracts, 'exercise', return_value=(expected, actual)):
            for feature in sorted(contracts.FEATURES):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:semantic-resolution'").fetchone()[0], 'pending')
        # Java dependencies in a complete mixed-type inventory cannot silently
        # become absent because synthetic import checks are implemented.
        (self.root / 'pom.xml').write_text('<project><groupId>fixture</groupId><artifactId>consumer</artifactId>'
            '<dependencies><dependency><groupId>fixture</groupId><artifactId>library</artifactId>'
            '</dependency></dependencies></project>')
        library = self.root / 'library'
        library.mkdir()
        (library / 'pom.xml').write_text('<project><groupId>fixture</groupId><artifactId>library</artifactId></project>')
        (library / 'Library.java').write_text('class Library {}\n')
        (self.root / 'marker.xml').write_text('<marker/>')
        mobile_contracts.inventory(self.state, self.root)
        unused_dep_contracts.plan_unused(self.state, self.root)
        contracts.plan_dependencies(self.state, self.root)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:target'").fetchone()[0], 'pending')
        self.assertEqual(self.state.execute("SELECT count(*) FROM coverage WHERE feature LIKE 'unused-deps%' AND status='inapplicable'").fetchone()[0], 0)
        with patch.object(contracts, 'exercise', side_effect=ToolError('synthetic interruption')):
            self.fixture._java_dependency_results = None
            self.assertEqual(self.evaluate(sorted(contracts.FEATURES)[0])['verdict'], 'error')

    def test_resume_fingerprint_and_artifact_boundary(self):
        original, read = adapter_digest(), Path.read_bytes
        with patch.object(Path, 'read_bytes', lambda p: read(p) + (
                b'\n# changed contract\n' if p.name == 'java_dependency_contracts.py' else b'')):
            self.assertNotEqual(adapter_digest(), original)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, self.directory.parent.parent.parent)


if __name__ == '__main__':
    unittest.main()
