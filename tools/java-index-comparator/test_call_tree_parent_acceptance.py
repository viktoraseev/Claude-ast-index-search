"""Refusal checks for source-only, stale, deleted and incomplete Java proofs."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_call_tree_mcp_contracts as tree_tests
import call_tree_acceptance as acceptance
import call_hierarchy_contracts as callers
import call_tree_mcp_contracts as trees
import mobile_contracts
from common import canonical_json, connect, stable_id
from audit import SCHEMA


class CallTreeParentTests(unittest.TestCase):
    def setUp(self):
        tree_tests.CallTreeMcpTests.setUp(self)
        mobile_contracts.inventory(self.state, self.root)
        self.state.execute("INSERT OR REPLACE INTO metadata VALUES ('java_files','1')")
        bindings = {key: 'synthetic-' + key for key in acceptance.BINDINGS}
        bindings['audit_scope'] = 'java'
        self.bindings = bindings
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', bindings.items())
        self.state.execute("INSERT OR REPLACE INTO coverage VALUES (?, 'pending','retained receiver/overload/root obligations')",
                           (acceptance.FEATURE,))
        for row in self.state.execute('SELECT * FROM checks WHERE feature=?', (callers.FEATURE,)).fetchall():
            expected = json.loads(row['expected_json'])
            request = {'project_path': str(self.root), 'file': 'Probe.java', 'subject': row['subject']}
            self.fixture.oracle_store.page(row['id'], 0, 'ide_call_hierarchy', request, {'fixture': 'authored test double'})
            expected.update(queries=1, declarations=1)
            self.state.execute("UPDATE checks SET expected_json=?,actual_json=?,diff_json=?,completed_at=1 WHERE id=?",
                (canonical_json(expected), canonical_json({'items': expected['items']}),
                 canonical_json({'missing': [], 'unexpected': []}), row['id']))
        for row in self.state.execute('SELECT * FROM checks WHERE feature=?', (trees.FEATURE,)).fetchall():
            self.fixture.evaluate(row)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (row['id'],)).fetchone()[0], 'pass')
        acceptance.plan(self.state, java_only=True)

    def target_ready(self):
        # Source acceptance has its own production fixtures and engine tests.
        # This test isolates target proof validation without promoting doubles
        # to live MCP coverage.
        with patch.object(acceptance.engine, 'readiness', return_value={'source': 'synthetic gate input'}):
            return acceptance.readiness(self.state, self.bindings)

    def test_complete_target_proof_is_read_only_and_bounded(self):
        before = self.state.total_changes
        result = self.target_ready()
        self.assertEqual(result['target_anchors'], 6)
        self.assertEqual(result['target_executed_checks'], 12)
        self.assertEqual(self.state.total_changes, before)
        self.assertLess(len(canonical_json(result)), 512)
        self.assertIsNotNone(self.state.execute('SELECT note FROM parent_acceptance_notes WHERE parent=?',
                                               (acceptance.FEATURE,)).fetchone())

    def test_source_only_and_missing_failed_unsupported_oracle_proofs_cannot_close(self):
        changes = (
            "DELETE FROM checks WHERE feature='call-tree:mcp-direct-callers' AND subject='seed'",
            "UPDATE checks SET verdict='unsupported' WHERE feature='call-tree:mcp-direct-callers'",
            "UPDATE checks SET verdict='fail' WHERE feature='call-tree:mcp-traversal-depth2'",
            "UPDATE checks SET completed_at=NULL WHERE feature='call-tree:mcp-direct-callers'",
            "DELETE FROM oracle_pages",
            "DELETE FROM call_hierarchy_anchors WHERE name='seed'",
            "DELETE FROM source_structures",
            "UPDATE checks SET expected_json='{}',actual_json='{}' WHERE feature='call-tree:mcp-direct-callers'",
        )
        for sql in changes:
            with self.subTest(sql=sql):
                self.state.execute('SAVEPOINT negative')
                self.state.execute(sql)
                with self.assertRaises(acceptance.AcceptancePending):
                    self.target_ready()
                self.state.execute('ROLLBACK TO negative')
                self.state.execute('RELEASE negative')

    def test_target_branch_metadata_and_retained_extra_ids_cannot_be_dropped(self):
        row = self.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?', (trees.FEATURE, 'seed')).fetchone()
        expected = json.loads(row['expected_json'])
        expected['branches'] = []
        self.state.execute('SAVEPOINT negative')
        self.state.execute('UPDATE checks SET expected_json=? WHERE id=?', (canonical_json(expected), row['id']))
        with self.assertRaises(acceptance.AcceptancePending):
            self.target_ready()
        self.state.execute('ROLLBACK TO negative')
        self.state.execute('RELEASE negative')
        self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                           ('retained-unsupported', trees.FEATURE, 'extra-recorded-obligation'))
        with self.assertRaises(acceptance.AcceptancePending):
            self.target_ready()
        self.state.execute("DELETE FROM checks WHERE id='retained-unsupported'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaises(acceptance.AcceptancePending):
            self.target_ready()

    def test_source_checklist_and_stale_edition_cannot_be_bypassed(self):
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.readiness(self.state, self.bindings)
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.readiness(self.state, {**self.bindings, 'binary_sha256': 'stale'})
        with patch.object(acceptance, 'cli_surface', return_value='changed'):
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_specification(acceptance.specification())

    def test_replay_preserves_historical_oracle_ids_outside_current_anchor_names(self):
        subject = 'unmapped-recorded-java-obligation'
        identity = stable_id({'feature': callers.FEATURE, 'subject': subject})
        self.state.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,'complete','unsupported')",
                           (identity, callers.FEATURE, subject))
        state = connect(self.directory / 'new-edition.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        state.executemany('INSERT INTO metadata VALUES (?,?)', self.bindings.items())
        replayed = []
        def evaluate(child):
            replayed.append(child['id'])
            state.execute("UPDATE checks SET status='complete',verdict='unsupported' WHERE id=?", (child['id'],))
        fixture = SimpleNamespace(root=self.root, state=state, client=object(),
                                  structure=self.fixture.structure, evaluate=evaluate)
        with patch('replay.StoredOracle', autospec=True):
            acceptance.replay_children(fixture, self.state)
        self.assertIn(identity, replayed)
        self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (identity,)).fetchone()[0], 'unsupported')
        self.assertIsNotNone(state.execute('SELECT id FROM parent_acceptance_members WHERE parent=? AND id=?',
                                          (acceptance.ORACLE_PARENT, identity)).fetchone())

    def test_final_gate_calls_call_tree_acceptance(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=self.directory)):
            fixture.setUp()
        try:
            with patch('call_tree_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved Java call-tree parent')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved Java call-tree parent'):
                    fixture.check()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
