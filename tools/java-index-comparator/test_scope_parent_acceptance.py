"""Scope acceptance must execute and retain the complete Java selector family."""
from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from audit import SCHEMA, next_check, plan
from common import canonical_json, connect, stable_id
import scope_acceptance as acceptance
import scope_acceptance_spec as spec


def temporary_artifacts():
    artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests/scope-acceptance'
    artifacts.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=artifacts)


class ScopePlanningTests(unittest.TestCase):
    def test_scope_parent_has_an_executable_check_and_full_child_ledger(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests/scope-acceptance'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'root'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {}')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Example.java'}], '', root=root, java_only=True)
            parent = state.execute("SELECT * FROM checks WHERE feature='global:scope-command-matrix'").fetchone()
            self.assertIsNotNone(parent, 'scope notes cannot replace an executable acceptance check')
            self.assertEqual(parent['status'], 'pending')
            import scope_acceptance
            required = {item['feature'] for item in scope_acceptance.specification()['criteria']}
            retained = {row[0] for row in state.execute(
                'SELECT feature FROM parent_acceptance_members WHERE parent=?', (scope_acceptance.FEATURE,))}
            self.assertEqual(retained, required)
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?',
                                          (scope_acceptance.FEATURE,)).fetchone()[0], 'pending')


class ScopeProofTests(unittest.TestCase):
    """Small state-machine inputs isolate the acceptance gate, not MCP truth."""
    def setUp(self):
        temporary = temporary_artifacts()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.feature, self.subject = 'global:scope:java-navigation', 'synthetic-proof'
        self.identity = stable_id({'feature': self.feature, 'subject': self.subject})
        self.samples = {'selected-root': {'identities': [['Example.java', 1]],
                                          'outside-scope': [], 'total': 1}, 'empty-page': []}
        self.criterion = dict(feature=self.feature, subject=self.subject,
                              fixture='synthetic gate input', production='src/commands/analysis.rs',
                              contract='selected declaration identity, outside-scope guard, total and empty page',
                              samples_count=len(self.samples), sample_keys_sha256=stable_id(sorted(self.samples)),
                              population_sha256=spec.population_shape(self.samples, self.feature))
        checklist = {**acceptance.specification(), 'criteria': [self.criterion]}
        for name, result in (('specification', checklist), ('required_acceptance_features', {self.feature})):
            mock = patch.object(acceptance, name, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)
        self.bindings = {key: 'synthetic-' + key for key in acceptance.BINDINGS}
        self.bindings['audit_scope'] = 'java'
        with self.state:
            self.state.executemany('INSERT INTO metadata VALUES (?,?)', self.bindings.items())
            self.state.execute("INSERT INTO coverage VALUES (?,'pending','retained original Java obligation')",
                               (acceptance.FEATURE,))
            self.state.execute("INSERT INTO coverage VALUES (?,'implemented','independent source/state')", (self.feature,))
            self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                               (self.identity, self.feature, self.subject))
        self.prove(self.state, self.identity)
        acceptance.plan(self.state, java_only=True)

    def prove(self, state, identity):
        with state:
            state.execute('''UPDATE checks SET status='complete',verdict='pass',completed_at=1,
                expected_json=?,actual_json=?,diff_json=?,error=NULL WHERE id=?''',
                (canonical_json({'source': 'independent source/state: synthetic gate input', 'samples': self.samples}),
                 canonical_json(self.samples), canonical_json({'missing': [], 'unexpected': []}), identity))

    def ready(self):
        return acceptance.readiness(self.state, self.bindings)

    def test_fresh_execution_is_bounded_and_original_notes_are_retained(self):
        before = self.state.total_changes
        result = self.ready()
        self.assertEqual((result['criteria'], result['executed_checks'], result['assertions']), (1, 1, 2))
        self.assertEqual(before, self.state.total_changes)
        self.assertLess(len(canonical_json(result)), 1024)
        self.assertEqual(self.state.execute('SELECT note FROM parent_acceptance_notes').fetchone()[0],
                         'retained original Java obligation')

    def test_failed_missing_unexecuted_unsupported_and_inapplicable_children_block_parent(self):
        statements = [
            'DELETE FROM checks WHERE id=?',
            "UPDATE checks SET status='pending' WHERE id=?",
            "UPDATE checks SET status='running' WHERE id=?",
            "UPDATE checks SET verdict='fail' WHERE id=?",
            "UPDATE checks SET verdict='unsupported' WHERE id=?",
            "UPDATE checks SET verdict='error' WHERE id=?",
            "UPDATE checks SET completed_at=NULL WHERE id=?",
            "UPDATE checks SET expected_json=NULL WHERE id=?",
            "UPDATE checks SET actual_json='{}' WHERE id=?",
            "UPDATE checks SET diff_json='{}' WHERE id=?",
        ]
        for statement in statements:
            with self.subTest(statement=statement):
                self.state.execute('SAVEPOINT mutation')
                self.state.execute(statement, (self.identity,))
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
                self.state.execute('ROLLBACK TO mutation')
                self.state.execute('RELEASE mutation')
        for status in ('inapplicable', 'out-of-scope', 'pending'):
            self.state.execute('SAVEPOINT mutation')
            self.state.execute('UPDATE coverage SET status=? WHERE feature=?', (status, self.feature))
            with self.assertRaises(acceptance.AcceptancePending):
                self.ready()
            self.state.execute('ROLLBACK TO mutation')
            self.state.execute('RELEASE mutation')

    def test_dropping_nested_negatives_from_both_sides_cannot_create_a_pass(self):
        smaller = json.loads(canonical_json(self.samples))
        del smaller['selected-root']['outside-scope']
        with self.state:
            self.state.execute('UPDATE checks SET expected_json=?,actual_json=? WHERE id=?',
                               (canonical_json({'source': 'synthetic', 'samples': smaller}),
                                canonical_json(smaller), self.identity))
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'nested scope assertion population'):
            self.ready()

    def test_stale_bindings_and_append_only_identity_obligations_block_closure(self):
        for key in acceptance.BINDINGS:
            with self.subTest(binding=key), self.assertRaises(acceptance.AcceptancePending):
                acceptance.readiness(self.state, {**self.bindings, key: 'stale'})
        with self.state:
            self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('later',?,'concurrent-obligation')",
                               (self.feature,))
        with self.assertRaises(acceptance.AcceptancePending):
            self.ready()
        with self.state:
            self.state.execute("DELETE FROM checks WHERE id='later'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'missing or changed'):
            self.ready()

    def test_scheduler_and_replay_execute_children_without_copying_passes(self):
        with self.state:
            self.state.execute("UPDATE checks SET status='pending',verdict=NULL WHERE id=?", (self.identity,))
        self.assertEqual(next_check(self.state)['id'], self.identity)
        target = connect(self.directory / 'replay.sqlite')
        self.addCleanup(target.close)
        target.executescript(SCHEMA)
        with target:
            target.executemany('INSERT INTO metadata VALUES (?,?)', self.bindings.items())
        observed = []

        def evaluate(check):
            self.assertIsNone(check['expected_json'])
            self.assertIsNone(check['verdict'])
            observed.append(check['id'])
            self.prove(target, check['id'])

        acceptance.replay_children(SimpleNamespace(state=target, evaluate=evaluate), self.state)
        self.assertEqual(observed, [self.identity])
        self.assertEqual(acceptance.readiness(target, self.bindings)['executed_checks'], 1)
        self.assertIsNone(self.state.execute('SELECT verdict FROM checks WHERE id=?', (self.identity,)).fetchone()[0])

    def test_new_java_commands_or_options_require_explicit_criteria_but_foreign_only_changes_do_not(self):
        read = Path.read_text
        for old, new in (
            ('enum Commands {', 'enum Commands {\n    NovelJavaProbe,'),
            ('in_file: Option<String>', 'novel_selector: Option<String>'),
            ('from_file: Option<String>', 'novel_endpoint: Option<String>'),
        ):
            def changed(path, *args, **kwargs):
                text = read(path, *args, **kwargs)
                return text.replace(old, new, 1) if path.name == 'main.rs' else text
            with self.subTest(option=new), patch.object(Path, 'read_text', changed):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'advertised Java'):
                    self.ready()

        def foreign_only(path, *args, **kwargs):
            text = read(path, *args, **kwargs)
            return text.replace('Suspend {', 'Suspend {\n        novel_foreign_option: bool,', 1) if path.name == 'main.rs' else text
        with patch.object(Path, 'read_text', foreign_only):
            self.ready()

    def test_final_gate_checks_scope_proofs_instead_of_trusting_implemented_status(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        with temporary_artifacts() as directory, patch('test_completion.tempfile.TemporaryDirectory',
                side_effect=lambda: factory(dir=directory)):
            fixture.setUp()
            try:
                with patch('scope_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved scope')):
                    with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved scope'):
                        fixture.check()
            finally:
                fixture.doCleanups()


class ScopeIdentityTests(unittest.TestCase):
    def test_only_ephemeral_fixture_key_prefix_is_folded_and_expected_values_stay_exact(self):
        feature = 'global:scope:java-file-views'
        first = {'json:all:/repo/.artifacts/first/file-scope-one/attached/View.java:imports': ['java.util.List']}
        second = {'json:all:/repo/.artifacts/second/file-scope-two/attached/View.java:imports': ['java.util.List']}
        self.assertEqual(spec.assertion_keys(first, feature), spec.assertion_keys(second, feature))
        self.assertEqual(spec.population_shape(first, feature), spec.population_shape(second, feature))
        changed_owner = {'json:all:/repo/.artifacts/second/file-scope-two/project/View.java:imports': ['java.util.List']}
        self.assertNotEqual(spec.assertion_keys(first, feature), spec.assertion_keys(changed_owner, feature))
        self.assertEqual(next(iter(first.values())), ['java.util.List'])
        self.assertNotEqual(canonical_json(first), canonical_json(second))
        self.assertNotEqual(spec.assertion_keys(first, 'other-java-contract'),
                            spec.assertion_keys(second, 'other-java-contract'))
        collision = {**first, **second}
        with self.assertRaisesRegex(ValueError, 'normalization collision'):
            spec.population_shape(collision, feature)

    def test_spec_covers_every_declared_scope_feature_and_java_cli_command(self):
        checklist = acceptance.specification()
        acceptance.validate_specification(checklist)
        self.assertEqual({item['feature'] for item in checklist['criteria']},
                         spec.required_acceptance_features())
        self.assertEqual(spec.unmapped_commands(), [])
        self.assertNotIn('resource-usages:xml-namespace-ownership', spec.required_acceptance_features())
        self.assertNotIn('unused-deps:android-ownership', spec.required_acceptance_features())


if __name__ == '__main__':
    unittest.main()
