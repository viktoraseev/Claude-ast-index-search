"""Production local type occurrence identities and applicability safeguards."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, adapter_digest, connect
import java_colliding_type_contracts as types


class CollidingTypeContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_colliding_type_occurrences(self):
        expected, actual = types.exercise(self.binary, self.directory)
        differences = {feature: [key for key, want in expected[feature].items()
                                 if actual[feature].get(key) != want] for feature in sorted(types.FEATURES)}
        self.assertEqual(differences, {feature: [] for feature in sorted(types.FEATURES)})

    def test_incomplete_inventory_and_external_mutation_are_errors(self):
        inventory = types.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(types.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                types.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            types.exercise(self.binary, '/private/tmp')

    def test_planning_preserves_pending_parents_and_case_ids(self):
        state = connect(self.directory / 'plan.sqlite')
        self.addCleanup(state.close)
        state.executescript('CREATE TABLE coverage(feature TEXT PRIMARY KEY,status TEXT,reason TEXT);'
                           'CREATE TABLE checks(id TEXT PRIMARY KEY,feature TEXT,subject TEXT);')
        state.execute("INSERT INTO coverage VALUES ('graph','pending','other unresolved criteria')")
        state.execute("INSERT INTO checks VALUES ('existing-case','graph','existing criterion')")
        types.plan_types(state, self.directory)
        types.plan_types(state, self.directory)
        self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='graph'").fetchone()[0], 'pending')
        self.assertEqual(state.execute("SELECT id FROM checks WHERE subject='existing criterion'").fetchone()[0], 'existing-case')
        self.assertEqual(state.execute('SELECT count(*) FROM checks').fetchone()[0], 3)

    def test_fixture_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# occurrence fixture edit\n' if path.name == 'java_colliding_type_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
