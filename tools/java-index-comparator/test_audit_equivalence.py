import tempfile
from pathlib import Path
import unittest

from audit import SCHEMA
from check_audit_equivalence import compare
from common import ToolError, canonical_json, connect


class AuditEquivalenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.original = self.directory / 'old.sqlite'
        self.optimized = self.directory / 'new.sqlite'
        self.report = self.directory / 'report.sqlite'
        for path in (self.original, self.optimized):
            state = connect(path)
            state.executescript(SCHEMA)
            with state:
                state.executemany('INSERT INTO metadata VALUES (?,?)',
                                   [('project_root', str(self.directory / 'target')), ('snapshot_sha256', 'same-source')])
                state.execute("INSERT INTO coverage VALUES ('search:content','implemented','live MCP text')")
                state.execute("INSERT INTO coverage VALUES ('pending','pending','not yet supported')")
                state.execute("INSERT INTO checks(id,feature,subject,status,verdict,expected_json) "
                              "VALUES ('case','search:content','Example','complete','pass',?)",
                              (canonical_json([{'file': 'Example.java', 'line': 1}]),))
            state.close()

    def mutate(self, query, parameters=()):
        state = connect(self.optimized)
        with state:
            state.execute(query, parameters)
        state.close()

    def result(self):
        return compare(self.original, self.optimized, self.report)

    def test_preserves_text_locations_not_payload_shape_and_does_not_hide_pending_coverage(self):
        self.mutate('UPDATE checks SET expected_json=?', (canonical_json([
            {'file': 'Example.java', 'line': 1, 'provenance': 'MCP full-line snapshot'}]),))
        result = self.result()
        self.assertTrue(result['verified'])
        self.assertEqual(result['text_truth_cases'], 1)
        self.assertEqual(result['pending_features_after'], 1)

    def test_equal_totals_cannot_hide_one_removed_case_and_one_new_case(self):
        self.mutate("UPDATE checks SET id='replacement',subject='Other'")
        result = self.result()
        self.assertFalse(result['verified'])
        self.assertEqual(result['issues'], {'missing_case': 1})
        self.assertEqual(result['additional_cases'], 1)

    def test_same_ids_with_reduced_scope_or_incomplete_execution_fail(self):
        self.mutate("UPDATE checks SET status='pending',expected_json=NULL")
        self.mutate("UPDATE coverage SET status='pending' WHERE feature='search:content'")
        result = self.result()
        self.assertFalse(result['verified'])
        self.assertEqual(result['issues'], {'unfinished_case': 1, 'reduced_feature_coverage': 1})

    def test_same_pass_verdict_with_lost_oracle_location_fails(self):
        self.mutate("UPDATE checks SET expected_json='[]'")
        self.assertEqual(self.result()['issues'], {'changed_text_truth': 1})

    def test_unknown_new_truth_and_source_changes_are_not_accepted(self):
        self.mutate("UPDATE checks SET expected_json='{}'")
        self.assertEqual(self.result()['issues'], {'unknown_text_truth': 1})
        self.mutate("UPDATE metadata SET value='changed' WHERE key='snapshot_sha256'")
        with self.assertRaises(ToolError):
            self.result()


if __name__ == '__main__':
    unittest.main()
