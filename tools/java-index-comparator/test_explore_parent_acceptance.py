"""Unexecuted Java exploration obligations must keep parent acceptance pending."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from audit import SCHEMA, plan
from common import canonical_json, connect, stable_id
import explore_acceptance as acceptance
from scope_acceptance_spec import population_shape


class ExploreParentTests(unittest.TestCase):
    def test_pending_parent_has_executable_acceptance_and_retains_reason(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            (root / 'Probe.java').write_text('class Probe { void leaf() {} }')
            state = connect(Path(directory) / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Probe.java'}], '  explore  Explore an area', root=root, java_only=True)
            feature = 'explore:semantic-resolution'
            row = state.execute('SELECT subject,status FROM checks WHERE feature=?', (feature,)).fetchone()
            self.assertIsNotNone(row, 'pending Java parent has no executed acceptance mechanism')
            self.assertEqual(row['status'], 'pending')
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
            self.assertIsNotNone(state.execute('SELECT note FROM parent_acceptance_notes WHERE parent=?', (feature,)).fetchone())

    def test_failed_missing_unsupported_or_narrowed_proof_never_closes_parent(self):
        # Small gate inputs isolate validation; they do not supply source/MCP truth.
        state = sqlite3.connect(':memory:')
        state.row_factory = sqlite3.Row
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        samples = {'branches': [1, 2]}
        criterion = dict(feature='authored-gate-input', subject='synthetic', fixture='gate-double',
                         production='src/commands/explore.rs', contract='synthetic validation input',
                         samples_count=1, sample_keys_sha256=stable_id(sorted(samples)),
                         population_sha256=population_shape(samples, 'authored-gate-input'))
        bindings = {key: 'synthetic-' + key for key in acceptance.BINDINGS}
        bindings['audit_scope'] = 'java'
        state.executemany('INSERT INTO metadata VALUES (?,?)', bindings.items())
        state.execute("INSERT INTO coverage VALUES (?,'pending','preserved unresolved Java behaviour')", (acceptance.FEATURE,))
        state.execute("INSERT INTO coverage VALUES (?,'implemented','gate double')", (criterion['feature'],))
        identity = stable_id({'feature': criterion['feature'], 'subject': criterion['subject']})
        state.execute("""INSERT INTO checks(id,feature,subject,status,verdict,completed_at,expected_json,actual_json,diff_json)
                         VALUES (?,?,?,'complete','pass',1,?,?,?)""",
                      (identity, criterion['feature'], criterion['subject'],
                       canonical_json({'source': 'gate double', 'samples': samples}),
                       canonical_json(samples), canonical_json({'missing': [], 'unexpected': []})))
        with patch.object(acceptance.checklist, 'criteria', return_value=[criterion]), \
                patch.object(acceptance, 'required_acceptance_features', return_value={criterion['feature']}):
            acceptance.plan(state, java_only=True)
            before = state.total_changes
            self.assertEqual(acceptance.readiness(state, bindings)['executed_checks'], 1)
            self.assertEqual(before, state.total_changes)
            for mutation in (
                    "UPDATE checks SET verdict='fail' WHERE feature='authored-gate-input'",
                    "UPDATE checks SET verdict='unsupported' WHERE feature='authored-gate-input'",
                    "UPDATE checks SET completed_at=NULL WHERE feature='authored-gate-input'",
                    "DELETE FROM checks WHERE feature='authored-gate-input'",
                    "UPDATE coverage SET status='inapplicable' WHERE feature='authored-gate-input'"):
                with self.subTest(mutation=mutation):
                    state.execute('SAVEPOINT negative')
                    state.execute(mutation)
                    with self.assertRaises(acceptance.AcceptancePending):
                        acceptance.readiness(state, bindings)
                    state.execute('ROLLBACK TO negative')
                    state.execute('RELEASE negative')
            narrowed = {'branches': [1]}
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_population(narrowed, criterion)
            state.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES ('retained',?,'unsupported-case','complete','unsupported')",
                          (criterion['feature'],))
            state.execute("DELETE FROM checks WHERE id='retained'")
            acceptance.plan(state, java_only=True)
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.readiness(state, bindings)

    def test_api_changes_or_omitted_result_obligations_block_specification(self):
        acceptance.validate_specification(acceptance.specification())
        with patch.object(acceptance.checklist, 'criteria', return_value=acceptance.checklist.criteria()[:-1]):
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_specification(acceptance.specification())
        with patch.object(acceptance.checklist, 'cli_surface', return_value='changed'):
            with self.assertRaises(acceptance.AcceptancePending):
                acceptance.validate_specification(acceptance.specification())

    def test_final_gate_requires_exploration_acceptance(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        factory = tempfile.TemporaryDirectory
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=base)):
            fixture.setUp()
        try:
            with patch('explore_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved Java exploration')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved Java exploration'):
                    fixture.check()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
