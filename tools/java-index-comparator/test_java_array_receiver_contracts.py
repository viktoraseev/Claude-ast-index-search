"""Array acceptance executes production and never closes a semantic parent."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, required_features
from common import ToolError, connect
import java_array_receiver_contracts as arrays


class ArrayReceiverContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_array_family(self):
        expected, actual = arrays.exercise(self.binary, self.directory)
        self.assertEqual({f: [k for k, v in expected[f].items() if actual[f].get(k) != v]
                          for f in arrays.FEATURES}, {f: [] for f in arrays.FEATURES})
        self.assertEqual(len(expected[arrays.EXPLORE]), 6)
        self.assertEqual(len(expected[arrays.GRAPH]), 400)
        for guard in arrays.GUARDS:
            for ambiguous in (False, True):
                self.assertEqual(expected[arrays.GRAPH][guard + ':guard:' + str(ambiguous)], [])

    def test_inventory_and_boundary_cannot_skip_applicable_java(self):
        with patch.object(arrays.mobile_contracts, 'inventory', return_value='incomplete'):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                arrays.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            arrays.exercise(self.binary, '/private/tmp')

    def test_ids_parents_and_failed_obligations_survive(self):
        state = connect(self.directory / 'checks.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute('INSERT INTO coverage VALUES (?,\'pending\',\'retained obligations\')', (parent,))
        state.execute("INSERT INTO checks(id,feature,subject) VALUES ('prior-java-case','graph','retained')")
        arrays.plan_arrays(state, self.directory)
        before = list(state.execute('SELECT id,feature,subject FROM checks ORDER BY id'))
        arrays.plan_arrays(state, self.directory)
        self.assertEqual(before, list(state.execute('SELECT id,feature,subject FROM checks ORDER BY id')))
        self.assertTrue(arrays.FEATURES <= required_features())
        fixture = Fixture(self.directory, self.binary, self.directory / 'unused-index', state, None)
        want = {f: {'endpoint': 'authored-marker'} for f in arrays.FEATURES}
        got = {f: {} for f in arrays.FEATURES}
        with patch.object(arrays, 'exercise', return_value=(want, got)) as exercise:
            for row in state.execute("SELECT * FROM checks WHERE id!='prior-java-case'"):
                fixture.evaluate(row)
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(dict(state.execute("SELECT verdict,count(*) FROM checks WHERE id!='prior-java-case' GROUP BY verdict")), {'fail': 2})
        for parent in ('graph', 'explore:semantic-resolution'):
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (parent,)).fetchone()[0], 'pending')
        self.assertEqual(state.execute("SELECT id FROM checks WHERE subject='retained'").fetchone()[0], 'prior-java-case')
        for row in state.execute("SELECT * FROM checks WHERE id!='prior-java-case'"):
            self.assertIn('not MCP equivalence', json.loads(row['expected_json'])['source'])
        fixture._array_receiver_results = None
        skipped = {f: {'endpoint': 'inapplicable'} for f in arrays.FEATURES}
        with patch.object(arrays, 'exercise', return_value=(want, skipped)):
            for row in state.execute("SELECT * FROM checks WHERE id!='prior-java-case'"):
                fixture.evaluate(row)
        self.assertEqual(dict(state.execute("SELECT verdict,count(*) FROM checks WHERE id!='prior-java-case' GROUP BY verdict")), {'fail': 2})
        fixture._array_receiver_results = None
        with patch.object(arrays, 'exercise', side_effect=ToolError('interrupted array contract')):
            for row in state.execute("SELECT * FROM checks WHERE id!='prior-java-case'"):
                fixture.evaluate(row)
        self.assertEqual(dict(state.execute("SELECT verdict,count(*) FROM checks WHERE id!='prior-java-case' GROUP BY verdict")), {'error': 2})


if __name__ == '__main__':
    unittest.main()
