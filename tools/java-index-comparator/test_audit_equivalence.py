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

    def deferred_outline_pair(self, *, prior_status='pending', prior_verdict=None,
                              prior_expected=None, feature='outline'):
        for path, original in ((self.original, True), (self.optimized, False)):
            state = connect(path)
            with state:
                state.execute("INSERT OR REPLACE INTO metadata VALUES ('audit_scope','java')")
                state.execute("INSERT INTO coverage VALUES (?,'implemented','MCP declarations')", (feature,))
                state.execute("INSERT INTO checks(id,feature,subject,status,verdict,expected_json) "
                              "VALUES ('outline-case',?,'Example.java',?,?,?)",
                              (feature, prior_status if original else 'complete',
                               prior_verdict if original else 'pass',
                               prior_expected if original else '[]'))
            state.close()

    def test_intentionally_deferred_outline_can_finish_without_losing_the_case(self):
        self.deferred_outline_pair()
        result = self.result()
        self.assertTrue(result['verified'], result['issues'])
        self.assertEqual(result['original_cases'], 2)
        self.assertEqual(result['present_cases'], 2)
        self.assertEqual(result['deferred_reference_outline_cases'], 1)
        # Allowing an unobserved baseline does not allow dropping its case or
        # treating a still-pending new run as a successful comparison.
        self.mutate("UPDATE checks SET status='pending',verdict=NULL WHERE id='outline-case'")
        self.assertEqual(self.result()['issues'], {'unfinished_case': 1})
        self.mutate("DELETE FROM checks WHERE id='outline-case'")
        self.assertEqual(self.result()['issues'], {'missing_case': 1})

    def test_constructor_outline_uses_the_same_deferred_case_policy(self):
        self.deferred_outline_pair(feature='outline:constructors')
        result = self.result()
        self.assertTrue(result['verified'], result['issues'])
        self.assertEqual(result['deferred_reference_outline_cases'], 1)

    def test_deferred_outline_with_a_new_mismatch_is_not_accepted(self):
        self.deferred_outline_pair()
        self.mutate("UPDATE checks SET verdict='fail' WHERE id='outline-case'")
        result = self.result()
        self.assertFalse(result['verified'])
        self.assertEqual(result['issues'], {'unresolved_deferred_outline': 1})

    def test_partial_or_failed_outline_reference_is_not_intentional_deferral(self):
        for status, verdict, expected in [('running', None, None),
                                          ('pending', 'error', None),
                                          ('pending', None, '[]')]:
            with self.subTest(status=status, verdict=verdict, expected=expected):
                self.deferred_outline_pair(prior_status=status, prior_verdict=verdict,
                                           prior_expected=expected)
                self.assertEqual(self.result()['issues'], {'incomplete_reference': 1})
                for path in (self.original, self.optimized):
                    state = connect(path)
                    with state:
                        state.execute("DELETE FROM checks WHERE id='outline-case'")
                        state.execute("DELETE FROM coverage WHERE feature='outline'")
                    state.close()

    def test_non_outline_incomplete_reference_still_fails(self):
        state = connect(self.original)
        with state:
            state.execute("UPDATE checks SET status='pending',verdict=NULL")
        state.close()
        self.assertEqual(self.result()['issues'], {'incomplete_reference': 1})


if __name__ == '__main__':
    unittest.main()
