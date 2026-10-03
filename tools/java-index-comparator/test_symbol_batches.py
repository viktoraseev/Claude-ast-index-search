import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit import Fixture, SCHEMA
from benchmark_symbol_batches import measure, measure_names, owners
from common import ToolError, connect


class SymbolBatchProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = connect(self.root / 'original.sqlite')
        self.state = connect(self.root / 'probe.sqlite')
        self.addCleanup(self.baseline.close)
        self.addCleanup(self.state.close)
        for connection in (self.baseline, self.state):
            connection.executescript(SCHEMA)
        self.expected = [{'name': 'run', 'qualifiedName': 'p.Example.run', 'file': 'Example.java', 'line': 2}]
        with self.baseline:
            self.baseline.execute("INSERT INTO checks(id,feature,subject,status,verdict,expected_json) "
                                  "VALUES ('outline','outline','Example.java','complete','pass',?)",
                                  (json.dumps(self.expected),))
            self.baseline.execute('INSERT INTO source_structures VALUES (?,?,?,?)', ('Example.java', 0, 0, json.dumps([
                {'kind': 'class', 'qualified_name': 'p.Example'},
                {'kind': 'method', 'qualified_name': 'p.Example.run'},
                {'kind': 'class', 'qualified_name': 'p.Empty'}])))
        self.fixture = Fixture(self.root, self.root, self.root, self.state, Mock())

    def test_captures_real_scope_and_compares_each_qualified_pattern(self):
        self.fixture.client.call.return_value = {'symbols': self.expected + [
            {'name': 'other', 'qualifiedName': 'unrelated.Example.other', 'file': 'Other.java', 'line': 1}]}
        result = measure(self.fixture, self.baseline, 10)
        self.assertEqual([request.args[1]['query'] for request in self.fixture.client.call.call_args_list],
                         ['Example.*', 'p.Example.*'])
        for group in result.values():
            self.assertEqual((group['owners'], group['pass'], group['members']), (1, 1, 1))
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 2)

    def test_nonempty_truth_cannot_be_reported_pass_for_empty_batch(self):
        self.fixture.client.call.return_value = {'symbols': []}
        result = measure(self.fixture, self.baseline, 1)
        self.assertEqual(result['fully-qualified']['mismatch'], 1)
        self.assertEqual(result['simple-qualified']['pass'], 0)

    def test_missing_independent_owner_inventory_is_not_inferred_from_native(self):
        with self.baseline:
            self.baseline.execute('DELETE FROM source_structures')
        with self.assertRaises(ToolError):
            list(owners(self.baseline, self.root))

    def test_full_name_probe_preserves_exact_identity_and_detects_lost_definition(self):
        with self.baseline:
            self.baseline.execute("INSERT INTO checks(id,feature,subject,status,verdict,expected_json) "
                                  "VALUES ('symbol','symbol','run','complete','pass',?)", (json.dumps(self.expected),))
        self.fixture.client.call.return_value = {'symbols': self.expected}
        result = measure_names(self.fixture, self.baseline, 1)
        self.assertEqual((result['cases'], result['pass']), (1, 1))
        self.assertEqual(self.fixture.client.call.call_args.args[1]['query'], 'run')
        self.fixture.client.call.return_value = {'symbols': []}
        result = measure_names(self.fixture, self.baseline, 1)
        self.assertEqual(result['mismatch'], 1)


if __name__ == '__main__':
    unittest.main()
