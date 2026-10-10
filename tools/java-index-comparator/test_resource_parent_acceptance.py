"""Java resource parent needs executed acceptance, not accumulated coverage notes."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from audit import SCHEMA, plan
from common import canonical_json, connect, stable_id
import resource_acceptance as acceptance
import resource_acceptance_spec as spec
import mobile_contracts
import android_contracts


class ResourceParentPlanningTests(unittest.TestCase):
    def test_pending_parent_has_an_executable_acceptance_check(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            directory = Path(temporary)
            root = directory / 'root'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {}')
            state = connect(directory / 'evidence.sqlite')
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': 'Example.java'}], '', root=root, java_only=True)
                check = state.execute("SELECT status FROM checks WHERE feature='android:syntax-resolution'").fetchone()
                self.assertIsNotNone(check, 'Java resource parent has no executable acceptance mechanism')
                self.assertEqual(check['status'], 'pending')
                self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='android:syntax-resolution'").fetchone()[0], 'pending')
                import resource_acceptance
                required = {item['feature'] for item in resource_acceptance.specification()['criteria']}
                members = {row[0] for row in state.execute('SELECT feature FROM parent_acceptance_members WHERE parent=?',
                                                         (resource_acceptance.FEATURE,))}
                self.assertEqual(members, required)
            finally:
                state.close()


class ResourceProofTests(unittest.TestCase):
    """Synthetic gate inputs test refusal to close; production fixtures run separately."""
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'Example.java').write_text('class Example {}')
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        mobile_contracts.inventory(self.state, self.root)
        android_contracts.plan_android(self.state, self.root)
        self.feature, self.subject = 'resource-usages:java-definition-bindings', 'synthetic-proof'
        self.samples = {'visible': {'locations': [['Example.java', 1]], 'total': 1}, 'rejected': []}
        self.criterion = dict(feature=self.feature, subject=self.subject, samples_count=2,
                              sample_keys_sha256=stable_id(sorted(self.samples)),
                              population_sha256=spec.population_shape(self.samples, self.feature),
                              fixture='synthetic gate input', production='src/indexer.rs',
                              contract='selected definition identity and rejected access guard')
        checklist = {**acceptance.specification(), 'criteria': [self.criterion]}
        for name, value in [('specification', checklist), ('required_acceptance_features', {self.feature})]:
            mock = patch.object(acceptance, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        metadata = dict(self.state.execute('SELECT key,value FROM metadata'))
        self.bindings = {key: metadata.get(key, 'synthetic-' + key) for key in acceptance.BINDINGS}
        self.bindings['audit_scope'] = 'java'
        self.identity = stable_id({'feature': self.feature, 'subject': self.subject})
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', self.bindings.items())
            self.state.execute("INSERT INTO coverage VALUES (?,'implemented','independent source/state')", (self.feature,))
            self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                               (self.identity, self.feature, self.subject))
            self.prove(self.identity, self.samples)
            for row in self.state.execute("SELECT id FROM checks WHERE subject='target-absence'"):
                self.prove(row[0], {'absence': True})
        acceptance.plan(self.state, java_only=True)

    def prove(self, identity, samples):
        self.state.execute("""UPDATE checks SET status='complete',verdict='pass',completed_at=1,
            expected_json=?,actual_json=?,diff_json=?,error=NULL WHERE id=?""",
                           (canonical_json({'source': 'independent source/state: synthetic gate input', 'samples': samples}),
                            canonical_json(samples), canonical_json({'missing': [], 'unexpected': []}), identity))

    def ready(self):
        return acceptance.readiness(self.state, self.bindings)

    def test_complete_proof_is_bounded_and_does_not_relabel_other_parents(self):
        self.state.execute("INSERT INTO coverage VALUES ('graph','pending','retained semantic obligation')")
        before = self.state.total_changes
        result = self.ready()
        self.assertEqual((result['criteria'], result['executed_checks'], result['assertions']), (1, 1, 2))
        self.assertEqual(self.state.total_changes, before)
        self.assertLess(len(canonical_json(result)), 1024)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='graph'").fetchone()[0], 'pending')
        self.assertIsNotNone(self.state.execute('SELECT note FROM parent_acceptance_notes WHERE parent=?',
                                               (acceptance.FEATURE,)).fetchone())

    def test_unexecuted_failed_unsupported_stale_or_missing_java_proof_cannot_close(self):
        for sql in ["DELETE FROM checks WHERE id=?", "UPDATE checks SET status='pending' WHERE id=?",
                    "UPDATE checks SET verdict='unsupported' WHERE id=?", "UPDATE checks SET verdict='error' WHERE id=?",
                    "UPDATE checks SET verdict='fail' WHERE id=?", "UPDATE checks SET completed_at=NULL WHERE id=?",
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

    def test_nested_assertion_drop_and_unknown_retained_subject_cannot_narrow_parent(self):
        self.state.execute('SAVEPOINT negative')
        self.prove(self.identity, {'visible': {'total': 1}, 'rejected': []})
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'assertion population'):
            self.ready()
        self.state.execute('ROLLBACK TO negative')
        self.state.execute('RELEASE negative')
        self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                           ('retained-unknown', self.feature, 'unreviewed-obligation'))
        self.prove('retained-unknown', self.samples)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'no reviewed executable criterion'):
            self.ready()
        self.state.execute("DELETE FROM checks WHERE id='retained-unknown'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'missing or changed'):
            self.ready()

    def test_present_target_and_missing_inventory_or_absence_proof_block_acceptance(self):
        for sql in ["UPDATE coverage SET status='pending' WHERE feature='resource-usages:target'",
                    "DELETE FROM checks WHERE subject='target-absence'",
                    "UPDATE checks SET actual_json='{}' WHERE subject='target-absence'",
                    'DELETE FROM android_applicability', 'DELETE FROM file_inventory']:
            with self.subTest(sql=sql):
                self.state.execute('SAVEPOINT negative')
                self.state.execute(sql)
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
                self.state.execute('ROLLBACK TO negative')
                self.state.execute('RELEASE negative')
        # Real positive framework inputs cannot be relabelled as absent even
        # when a caller forges coverage and empty-command pass rows.
        path = self.root / 'app/src/main/res/values/strings.xml'
        path.parent.mkdir(parents=True)
        path.write_text('<resources><string name="hit">Value</string></resources>')
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
        with patch.object(acceptance.engine, 'readiness', return_value={}):
            with self.assertRaisesRegex(acceptance.AcceptancePending, 'inventory is invalid'):
                self.ready()

    def test_absence_population_is_only_valid_for_the_exact_recorded_subject(self):
        criterion = {**self.criterion, 'feature': 'resource-usages', 'subject': 'disposable-java-android'}
        proof = acceptance.criterion_for_proof({'feature': 'resource-usages', 'subject': 'target-absence'}, criterion)
        self.assertEqual(proof['samples_count'], 1)
        self.assertEqual(proof['population_sha256'], spec.population_shape({'absence': True}, 'resource-usages'))
        with self.assertRaises(acceptance.AcceptancePending):
            acceptance.criterion_for_proof({'feature': 'resource-usages', 'subject': 'other-absence'}, criterion)

    def test_new_resource_option_blocks_an_unreviewed_checklist(self):
        original = Path.read_text
        def changed(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path.name == 'main.rs':
                text = text.replace('    ResourceUsages {', '    ResourceUsages {\n        novel_java_flag: bool,', 1)
            return text
        with patch.object(Path, 'read_text', changed):
            with self.assertRaises(acceptance.AcceptancePending):
                self.ready()

    def test_final_gate_requires_resource_acceptance_even_if_parent_is_labelled_implemented(self):
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=self.root)):
            fixture.setUp()
        try:
            with patch('resource_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved Java resource parent')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved Java resource parent'):
                    fixture.check()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
