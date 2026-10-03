"""Compact Java CLI regressions and honest source-vs-MCP coverage gates."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from build_index import build_ast_index
from common import ToolError, connect
import mobile_contracts
import unused_dep_contracts as contracts


class UnusedDependencyContracts(unittest.TestCase):
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
        oracle.call.side_effect = AssertionError('source/state fixtures cannot claim MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'target-index.sqlite', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        row = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(row)
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_java_scope_types_api_chains_and_options_execute_once(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                with self.subTest(feature=feature):
                    row = self.evaluate(feature)
                    diff = json.loads(row['diff_json'] or '{}')
                    self.assertEqual(row['verdict'], 'pass', {'feature': feature, 'verdict': row['verdict'],
                        'error': row['error'], 'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                    self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_full_inventory_retains_applicable_target_and_semantic_gaps(self):
        for feature in contracts.FEATURES | contracts.PENDING.keys() | {'unused-deps:target'}:
            self.assertIn(feature, required_features())
        for feature in contracts.PENDING:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        # An applicable non-Maven Java build cannot silently become absent.
        (self.root / 'build.gradle').write_text("plugins { id 'java' }\n")
        mobile_contracts.inventory(self.state, self.root)
        contracts.plan_unused(self.state, self.root)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:target'").fetchone()[0], 'pending')
        self.assertEqual(self.evaluate('unused-deps:target')['verdict'], 'unsupported')
        (self.root / 'build.gradle').unlink()
        (self.root / 'pom.xml').write_text('<project><groupId>fixture</groupId><artifactId>consumer</artifactId>'
            '<dependencies><dependency><groupId>fixture</groupId><artifactId>library</artifactId>'
            '</dependency></dependencies></project>')
        library = self.root / 'library'
        library.mkdir()
        (library / 'pom.xml').write_text('<project><groupId>fixture</groupId><artifactId>library</artifactId></project>')
        (library / 'Library.java').write_text('class Library {}\n')
        mobile_contracts.inventory(self.state, self.root)
        contracts.plan_unused(self.state, self.root)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='unused-deps:target'").fetchone()[0], 'pending')
        self.assertEqual(self.evaluate('unused-deps:target')['verdict'], 'unsupported')
        for feature in contracts.FEATURES:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'implemented')
            self.fixture._unused_dep_results = None
            with patch.object(contracts, 'exercise', return_value=({feature: {'case': 1}}, {feature: {'case': 2}})):
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, self.directory.parent.parent.parent)

    def test_independently_edgeless_target_runs_cli_and_cannot_hide_bad_output(self):
        (self.root / 'pom.xml').write_text('<project><groupId>fixture</groupId><artifactId>single</artifactId></project>')
        mobile_contracts.inventory(self.state, self.root)
        contracts.plan_unused(self.state, self.root)
        build_ast_index(str(self.binary), self.root, self.fixture.database, 'edgeless')
        row = self.evaluate('unused-deps:target')
        self.assertEqual(row['verdict'], 'pass', row['error'])
        self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
        with patch.object(self.fixture, 'text_cli', return_value="Module 'single' has no dependencies.\n"):
            self.assertEqual(self.evaluate('unused-deps:target')['verdict'], 'fail')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
