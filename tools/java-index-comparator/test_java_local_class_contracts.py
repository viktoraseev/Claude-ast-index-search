"""Execute local-class contracts and prevent silent applicability passes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_local_class_contracts as classes


class LocalClassContractsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// inventory only\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('independent source contracts must not call MCP')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_shared_production_fixture_checks_scopes_members_and_explore(self):
        with patch.object(classes, 'exercise', wraps=classes.exercise) as exercise:
            for feature in sorted(classes.FEATURES):
                with self.subTest(feature=feature):
                    result = self.evaluate(feature)
                    diff = json.loads(result['diff_json'] or '{}')
                    self.assertEqual(result['verdict'], 'pass',
                                     {'feature': feature, 'error': result['error'],
                                      'missing': len(diff.get('missing', [])),
                                      'unexpected': len(diff.get('unexpected', []))})
                    self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_features_cannot_be_skipped_and_broad_contracts_stay_pending(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in classes.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            with patch.object(classes, 'exercise', return_value=({feature: {'edges': []}},
                                                                   {feature: {'edges': 'inapplicable'}})):
                self.fixture._local_class_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            with patch.object(classes, 'exercise', side_effect=ToolError('synthetic interruption')):
                self.fixture._local_class_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('graph', 'call-tree:semantic-resolution', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            classes.exercise(self.binary, self.directory.parent.parent.parent)

    def test_incomplete_inventory_is_an_error(self):
        inventory = classes.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.kt'")
        with patch.object(classes.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                classes.exercise(self.binary, self.directory)

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# local class change\n' if path.name == 'java_local_class_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
