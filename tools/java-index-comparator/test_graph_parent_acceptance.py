"""Graph parent acceptance regressions; gate doubles are not MCP evidence."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from audit import SCHEMA, plan
from common import canonical_json, connect, stable_id
import graph_acceptance as acceptance
import graph_overload_contracts
import graph_mcp_contracts
import call_hierarchy_contracts as callers
import mobile_contracts
import test_call_tree_mcp_contracts as tree_tests


class GraphParentPlanningTests(unittest.TestCase):
    def test_pending_graph_has_an_executable_acceptance_check(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            (root / 'Probe.java').write_text('class Probe { void leaf() {} }')
            state = connect(Path(directory) / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Probe.java'}], '  graph  Symbol graph',
                 root=root, java_only=True)
            check = state.execute("SELECT subject,status FROM checks WHERE feature='graph'").fetchone()
            self.assertIsNotNone(check, 'pending graph parent has no executed acceptance mechanism')
            self.assertEqual(check['status'], 'pending')
            self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='graph'").fetchone()[0],
                             'pending', 'planning alone must not close the graph parent')


class GraphParentProofTests(unittest.TestCase):
    def setUp(self):
        tree_tests.CallTreeMcpTests.setUp(self)
        mobile_contracts.inventory(self.state, self.root)
        self.bindings = {key: 'synthetic-' + key for key in acceptance.BINDINGS}
        self.bindings['audit_scope'] = 'java'
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', self.bindings.items())
        self.state.execute("INSERT OR REPLACE INTO metadata VALUES ('java_files','1')")
        self.state.execute("INSERT OR REPLACE INTO coverage VALUES ('graph','pending','retained dispatch obligations')")
        for row in self.state.execute('SELECT * FROM checks WHERE feature=?', (callers.FEATURE,)):
            expected = json.loads(row['expected_json'])
            self.fixture.oracle_store.page(row['id'], 0, 'ide_call_hierarchy',
                {'project_path': str(self.root), 'file': 'Probe.java', 'subject': row['subject']},
                {'fixture': 'authored gate double'})
            expected.update(queries=1, declarations=1)
            self.state.execute('''UPDATE checks SET expected_json=?,actual_json=?,diff_json=?,completed_at=1
                WHERE id=?''', (canonical_json(expected), canonical_json({'items': expected['items']}),
                               canonical_json({'missing': [], 'unexpected': []}), row['id']))
        graph_mcp_contracts.plan(self.state)
        for row in self.state.execute('SELECT * FROM checks WHERE feature=?', (graph_mcp_contracts.FEATURE,)):
            self.fixture.evaluate(row)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (row['id'],)).fetchone()[0], 'pass')
        acceptance.plan(self.state, java_only=True)

    def target_ready(self):
        # Only isolates proof validation. Source acceptance is separately
        # executed against production; these doubles never claim live MCP truth.
        with patch.object(acceptance.engine, 'readiness', return_value={'source': 'synthetic gate input'}):
            return acceptance.readiness(self.state, self.bindings)

    def test_target_composition_is_read_only_bounded_and_preserves_notes(self):
        before = self.state.total_changes
        result = self.target_ready()
        self.assertEqual((result['target_anchors'], result['target_executed_checks']), (6, 12))
        self.assertEqual(before, self.state.total_changes)
        self.assertLess(len(canonical_json(result)), 512)
        self.assertIsNotNone(self.state.execute("SELECT note FROM parent_acceptance_notes WHERE parent='graph'").fetchone())

    def test_missing_failed_unsupported_and_unexecuted_java_proofs_block_closure(self):
        mutations = (
            "DELETE FROM checks WHERE feature='graph:mcp-direct-callers' AND subject='seed'",
            "UPDATE checks SET verdict='unsupported' WHERE feature='graph:mcp-direct-callers'",
            "UPDATE checks SET verdict='fail' WHERE feature='call-tree:mcp-direct-callers'",
            "UPDATE checks SET completed_at=NULL WHERE feature='graph:mcp-direct-callers'",
            "DELETE FROM oracle_pages",
            "DELETE FROM source_structures",
            "DELETE FROM call_hierarchy_anchors WHERE name='seed'",
            "UPDATE coverage SET status='inapplicable' WHERE feature='graph:mcp-direct-callers'",
            "UPDATE checks SET expected_json='{}',actual_json='{}' WHERE feature='graph:mcp-direct-callers'",
        )
        for sql in mutations:
            with self.subTest(sql=sql):
                self.state.execute('SAVEPOINT negative')
                self.state.execute(sql)
                with self.assertRaises(acceptance.AcceptancePending):
                    self.target_ready()
                self.state.execute('ROLLBACK TO negative')
                self.state.execute('RELEASE negative')

    def test_projection_metadata_and_retained_unknown_id_cannot_be_dropped(self):
        row = self.state.execute("SELECT * FROM checks WHERE feature='graph:mcp-direct-callers' AND subject='seed'").fetchone()
        expected = json.loads(row['expected_json'])
        for key, value in (('oracle_owners', []), ('selected_seeds', []), ('source_case_id', 'changed')):
            self.state.execute('SAVEPOINT negative')
            self.state.execute('UPDATE checks SET expected_json=? WHERE id=?',
                               (canonical_json({**expected, key: value}), row['id']))
            with self.assertRaisesRegex(acceptance.AcceptancePending, 'metadata changed'):
                self.target_ready()
            self.state.execute('ROLLBACK TO negative')
            self.state.execute('RELEASE negative')
        self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('retained',?,'unmapped-obligation')",
                           (graph_mcp_contracts.FEATURE,))
        self.state.execute("DELETE FROM checks WHERE id='retained'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaises(acceptance.AcceptancePending):
            self.target_ready()

    def test_source_only_stale_or_narrowed_checklist_never_closes(self):
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.readiness(self.state, self.bindings)
        with patch.object(acceptance.checklist, 'cli_surface', return_value='changed'):
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_specification(acceptance.specification())
        criterion = acceptance.checklist.criteria()[0]
        with patch.object(acceptance.checklist, 'criteria', return_value=[criterion]):
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_specification(acceptance.specification())

    def test_replay_retains_unsupported_java_ids_and_does_not_copy_passes(self):
        identity = stable_id({'feature': graph_mcp_contracts.FEATURE, 'subject': 'retained-unknown'})
        self.state.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,'complete','unsupported')",
                           (identity, graph_mcp_contracts.FEATURE, 'retained-unknown'))
        state = connect(self.directory / 'new-edition.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', self.bindings.items())
        seen = []
        def evaluate(row):
            seen.append(row['id'])
            self.assertIsNone(row['expected_json'])
            state.execute("UPDATE checks SET status='complete',verdict='unsupported' WHERE id=?", (row['id'],))
        fixture = SimpleNamespace(root=self.root, state=state, structure=self.fixture.structure,
                                  client=object(), evaluate=evaluate)
        with patch('replay.StoredOracle', autospec=True):
            acceptance.replay_children(fixture, self.state)
        self.assertIn(identity, seen)
        self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (identity,)).fetchone()[0], 'unsupported')

    def test_final_gate_requires_graph_acceptance(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=self.directory)):
            fixture.setUp()
        try:
            with patch('graph_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved graph parent')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved graph parent'):
                    fixture.check()
        finally:
            fixture.doCleanups()


class GraphOverloadProductionTests(unittest.TestCase):
    def test_source_contract_executes_production_without_oracle(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        expected, actual = graph_overload_contracts.exercise(binary, base)
        self.assertEqual(expected, actual)
        criterion = next(c for c in acceptance.checklist.criteria() if c['feature'] == graph_overload_contracts.FEATURE)
        self.assertEqual((len(expected), stable_id(sorted(expected))),
                         (criterion['samples_count'], criterion['sample_keys_sha256']))
        acceptance.validate_population(expected, criterion)
        # Dropping the constructor containment branch from both sides must
        # never promote a narrowed population to parent proof.
        smaller = json.loads(canonical_json(expected))
        smaller['forward:path:createAlpha:alphaSeed']['paths'].pop()
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.validate_population(smaller, criterion)


if __name__ == '__main__':
    unittest.main()
