"""Compact production regressions for the shared Java project insight family."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, canonical_json, connect
from root_contracts import Runner
import insight_scope_contracts as contracts


class InsightScopeTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Inventory.kt').write_text('// inventory only\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('independent insight fixtures are not MCP truth')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}],
             '  class  Classes\n  symbol  Symbols\n  file  Files', root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_production_scope_family_counts_identities_limits_and_rendering(self):
        with patch.object(contracts, 'exercise', wraps=contracts.exercise) as exercise:
            for feature in sorted(contracts.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                    'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_java_insights_cannot_be_skipped_or_claim_parent_coverage(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in sorted(contracts.FEATURES):
            self.assertIn(feature, required_features())
            coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(coverage['status'], 'implemented')
            self.assertTrue(coverage['reason'].startswith('independent source/state:'))
            for observed in ({}, {'inventory:project': {'.java': 0}}, {'inventory:project': 'inapplicable'}):
                self.fixture._insight_scope_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'inventory:project': {'.java': 8, '.kt': 1, '.xml': 1}}},
                        {feature: observed})):
                    self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            self.fixture._insight_scope_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete inventory')):
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('global:scope-command-matrix', 'global:format', 'graph', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.fixture.binary, Path('/private/tmp'))
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise_module_counts(self.fixture.binary, Path('/private/tmp'))

    def test_incomplete_full_inventory_is_error_not_inapplicable(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.fixture.binary, self.directory)

    def test_internal_map_checker_keeps_root_identity_and_its_evidence_label(self):
        runner = Runner(self.fixture.binary, self.directory)
        runner.root.mkdir()
        (runner.root / '.git').mkdir()
        (runner.root / 'Main.java').write_text('class Main extends PrimaryBase {}\n')
        attached = self.directory / 'attached'
        attached.mkdir()
        (attached / 'Main.java').write_text('class Main extends AttachedBase {}\n')
        runner.environment.update(AST_INDEX_ROOT=str(runner.root),
                                  AST_INDEX_DB_PATH=str(self.directory / 'internal-index.sqlite'))
        runner.command('rebuild', '--force')
        runner.command('subtree', 'add', 'attached-label', attached)
        runner.command('rebuild', '--force')
        state = connect(self.directory / 'internal-checks.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        state.execute("INSERT INTO coverage VALUES ('map','implemented','internal CLI/DB; not MCP equivalence')")
        fixture = Fixture(runner.root, self.fixture.binary, self.directory / 'internal-index.sqlite', state, None)
        for number, module in enumerate((None, '')):
            state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (str(number), 'map', canonical_json({'module': module})))
            check = state.execute('SELECT * FROM checks WHERE id=?', (str(number),)).fetchone()
            fixture.evaluate(check)
            self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (str(number),)).fetchone()[0], 'pass')
        check = state.execute("SELECT * FROM checks WHERE id='0'").fetchone()
        cli = fixture.cli
        def omit_owner(*args):
            result = cli(*args)
            if result['groups']:
                result['groups'].pop()
            return result
        with patch.object(fixture, 'cli', side_effect=omit_owner):
            fixture.evaluate(check)
        self.assertEqual(state.execute("SELECT verdict FROM checks WHERE id='0'").fetchone()[0], 'fail')
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertIn('not MCP equivalence', state.execute("SELECT reason FROM coverage WHERE feature='map'").fetchone()[0])

    def test_module_count_family_matches_authored_declarations(self):
        expected, actual = contracts.exercise_module_counts(self.fixture.binary, self.directory)
        mismatches = [key for key in expected if canonical_json(expected[key]) != canonical_json(actual.get(key))]
        self.assertEqual(mismatches, [], {'mismatch_count': len(mismatches), 'samples': mismatches[:10]})

    def test_module_inventory_cannot_hide_an_applicable_build_descriptor(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE path='pom.xml' OR path LIKE '%/pom.xml'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise_module_counts(self.fixture.binary, self.directory)

    def test_fixture_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# fixture edit\n' if path.name == 'insight_scope_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
