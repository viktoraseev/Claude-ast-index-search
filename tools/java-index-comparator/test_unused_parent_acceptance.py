"""A completed subset must never close the Java unused-dependency parent."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import SCHEMA, plan
from common import canonical_json, connect, stable_id
import unused_acceptance as acceptance
import unused_acceptance_spec as spec
import mobile_contracts
import unused_dep_contracts


class UnusedParentPlanningTests(unittest.TestCase):
    def test_module_root_keys_normalize_only_the_disposable_directory(self):
        criterion = next(item for item in spec.criteria() if item['feature'] == 'global:scope:java-module-roots')
        first = {'/repo/.artifacts/tests/module-roots-one/project:--local': True}
        second = {'/repo/.artifacts/tests/module-roots-two/project:--local': True}
        self.assertEqual(acceptance.assertion_keys(first, criterion), acceptance.assertion_keys(second, criterion))
        different_root = {'/repo/.artifacts/tests/module-roots-two/attached:--local': True}
        self.assertNotEqual(acceptance.assertion_keys(first, criterion), acceptance.assertion_keys(different_root, criterion))
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'collision'):
            acceptance.validate_population({**first, **second}, criterion)
        from scope_acceptance_spec import criteria
        original = next(item for item in criteria() if item['feature'] == criterion['feature'])
        self.assertEqual(criterion['sample_keys_sha256'], original['sample_keys_sha256'])

    def test_pending_parent_has_an_executable_acceptance_check(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary) / 'root'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {}')
            state = connect(Path(temporary) / 'evidence.sqlite')
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': 'Example.java'}], '', root=root, java_only=True)
                row = state.execute("SELECT status FROM checks WHERE feature='unused-deps:semantic-resolution'").fetchone()
                self.assertIsNotNone(row, 'pending Java dependency parent has no executed acceptance check')
                self.assertEqual(row[0], 'pending')
                required = {item['feature'] for item in acceptance.specification()['criteria']}
                self.assertEqual(required, acceptance.required_acceptance_features())
                self.assertIn('unused-deps:java-retained-inference', required)
                self.assertIn('unused-deps:java-source-classpath-ambiguity', required)
                decision = next(item for item in spec.API_DECISIONS
                                if item['id'] == 'duplicate-source-classpath-order')
                self.assertEqual(decision['criterion'], 'unused-deps:java-source-classpath-ambiguity')
                self.assertNotIn('unused-deps:android-ownership', required)
                self.assertNotIn('resource-usages:xml-syntax', required)
            finally:
                state.close()


class UnusedParentProofTests(unittest.TestCase):
    """Synthetic gate data never stands in for executed production fixtures."""
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.root = directory / 'root'
        self.root.mkdir()
        (self.root / 'Example.java').write_text('class Example {}')
        self.state = connect(directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        mobile_contracts.inventory(self.state, self.root)
        unused_dep_contracts.plan_unused(self.state, self.root)
        self.feature, self.subject = 'unused-deps:java-retained-inference', 'synthetic-proof'
        self.samples = {'positive': {'owner': 'Base', 'categories': ['direct']}, 'guard': []}
        self.criterion = dict(feature=self.feature, subject=self.subject, samples_count=2,
                              sample_keys_sha256=stable_id(sorted(self.samples)),
                              population_sha256=acceptance.population_shape(self.samples, self.feature),
                              fixture='synthetic gate input', production='src/commands/modules.rs',
                              contract='retained positive ownership and negative guard',
                              input='synthetic gate data', expected='retained full proof', stop='all proof present')
        checklist = {**acceptance.specification(), 'criteria': [self.criterion]}
        for name, value in [('specification', checklist), ('required_acceptance_features', {self.feature})]:
            mock = patch.object(acceptance, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.identity = stable_id({'feature': self.feature, 'subject': self.subject})
        metadata = dict(self.state.execute('SELECT key,value FROM metadata'))
        self.bindings = {key: metadata.get(key, 'synthetic-' + key) for key in acceptance.BINDINGS}
        self.bindings.update(audit_scope='java', project_root=str(self.root))
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', self.bindings.items())
            self.state.execute("INSERT INTO coverage VALUES (?,'implemented','independent source/state')", (self.feature,))
            self.state.execute("INSERT OR REPLACE INTO coverage VALUES (?,'pending','retained obligation')", (acceptance.FEATURE,))
            self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                               (self.identity, self.feature, self.subject))
            self.prove(self.identity, self.samples)
        acceptance.plan(self.state, java_only=True)

    def prove(self, identity, samples):
        self.state.execute("""UPDATE checks SET status='complete',verdict='pass',completed_at=1,
            expected_json=?,actual_json=?,diff_json=?,error=NULL WHERE id=?""",
                           (canonical_json({'source': 'independent source/state: synthetic gate input', 'samples': samples}),
                            canonical_json(samples), canonical_json({'missing': [], 'unexpected': []}), identity))

    def ready(self):
        # Isolate child composition; target inventory and the unresolved API
        # decision each have separate refusal tests below.
        with patch.object(acceptance, 'target_proof', return_value={'checks': 1}), \
                patch.object(spec, 'API_DECISIONS', ()):
            return acceptance.readiness(self.state, self.bindings)

    def test_complete_child_population_is_bounded_and_does_not_close_parent_by_itself(self):
        before = self.state.total_changes
        result = self.ready()
        self.assertEqual((result['criteria'], result['executed_checks'], result['assertions']), (1, 1, 2))
        self.assertLess(len(canonical_json(result)), 2048)
        self.assertEqual(self.state.total_changes, before)
        self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (acceptance.FEATURE,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'source-classpath contract'):
            acceptance.readiness(self.state, self.bindings)

    def test_missing_failed_unsupported_or_stale_proof_blocks_acceptance(self):
        for sql in ["DELETE FROM checks WHERE id=?", "UPDATE checks SET status='pending' WHERE id=?",
                    "UPDATE checks SET verdict='fail' WHERE id=?", "UPDATE checks SET verdict='unsupported' WHERE id=?",
                    "UPDATE checks SET verdict='error' WHERE id=?", "UPDATE checks SET completed_at=NULL WHERE id=?",
                    "UPDATE checks SET expected_json='{}',actual_json='{}' WHERE id=?"]:
            with self.subTest(sql=sql):
                self.state.execute('SAVEPOINT negative')
                self.state.execute(sql, (self.identity,))
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
                self.state.execute('ROLLBACK TO negative')
                self.state.execute('RELEASE negative')
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.readiness(self.state, {**self.bindings, 'binary_sha256': 'stale'})

    def test_nested_branch_and_concurrent_unknown_case_cannot_disappear(self):
        self.prove(self.identity, {'positive': {'owner': 'Base'}, 'guard': []})
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'assertion branches'):
            self.ready()
        self.prove(self.identity, self.samples)
        self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                           ('concurrent', self.feature, 'new-obligation'))
        self.prove('concurrent', self.samples)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'unreviewed retained'):
            self.ready()
        self.state.execute("DELETE FROM checks WHERE id='concurrent'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'missing or changed'):
            self.ready()

    def test_java_only_or_stale_inventory_cannot_establish_target_absence(self):
        (self.root / 'build.gradle').write_text("plugins { id 'java' }")
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'inventory'):
            acceptance.target_proof(self.state, self.bindings)
        mobile_contracts.inventory(self.state, self.root)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'applicability/ownership unresolved'):
            acceptance.target_proof(self.state, self.bindings)

    def test_unexecuted_target_rows_and_dropped_inventory_are_rejected(self):
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'executed pass'):
            acceptance.target_proof(self.state, self.bindings)
        self.state.execute('DELETE FROM file_inventory')
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'inventory'):
            acceptance.target_proof(self.state, self.bindings)

    def test_new_command_option_needs_checklist_review(self):
        with patch.object(spec, 'cli_surface', return_value='new-api'):
            with self.assertRaises(acceptance.AcceptancePending):
                self.ready()

    def test_final_gate_requires_unused_acceptance_even_for_a_green_coverage_label(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=self.root.parent)):
            fixture.setUp()
        try:
            with patch('unused_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved dependency parent')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved dependency parent'):
                    fixture.check()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
