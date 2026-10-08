"""Acceptance composition tests; synthetic gate inputs are not MCP evidence."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from audit import SCHEMA, next_check, plan
from common import canonical_json, connect, stable_id
import parent_acceptance as acceptance


def temporary_artifacts():
    directory = Path(__file__).resolve().parents[2] / '.artifacts/tests/parent-acceptance'
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=directory)


class AcceptanceProofTests(unittest.TestCase):
    """Tiny state-machine inputs; none of these claim production equivalence."""
    def setUp(self):
        directory = temporary_artifacts()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.feature, self.subject = 'global:format:java-file-views', 'synthetic-input'
        self.identity = stable_id({'feature': self.feature, 'subject': self.subject})
        self.samples = {'identity': ['Example.java', 1, 'Example'], 'empty-page': []}
        self.spec = {'parent': acceptance.FEATURE, 'criteria': [dict(
            feature=self.feature, subject=self.subject, samples_count=len(self.samples),
            sample_keys_sha256=stable_id(sorted(self.samples)),
            contract='file/outline text and JSON identities, empty pages',
            fixture='synthetic state-machine input', production='src/commands/files.rs')]}
        specification = patch.object(acceptance, 'specification', return_value=self.spec)
        specification.start()
        self.addCleanup(specification.stop)
        catalog = patch.object(acceptance, 'required_format_features', return_value={self.feature})
        catalog.start()
        self.addCleanup(catalog.stop)
        self.bindings = {key: 'synthetic-' + key for key in acceptance.BINDINGS}
        self.bindings['audit_scope'] = 'java'
        with self.state:
            self.state.executemany('INSERT INTO metadata VALUES (?,?)', self.bindings.items())
            self.state.execute("INSERT INTO coverage VALUES (?,'implemented','synthetic proof')", (self.feature,))
            self.state.execute("INSERT INTO coverage VALUES (?,'pending','synthetic parent')", (acceptance.FEATURE,))
            self.state.execute('''INSERT INTO checks(id,feature,subject,status,verdict,expected_json,
                actual_json,diff_json,completed_at) VALUES (?,?,?,'complete','pass',?,?,?,1)''',
                (self.identity, self.feature, self.subject,
                 canonical_json({'source': 'independent source/state: synthetic test', 'samples': self.samples}),
                 canonical_json(self.samples), canonical_json({'missing': [], 'unexpected': []})))
        acceptance.plan(self.state, java_only=True)

    def mutate(self, sql, parameters=()):
        with self.state:
            self.state.execute(sql, parameters)

    def ready(self):
        return acceptance.readiness(self.state, self.bindings)

    def test_composition_is_read_only_and_bounded(self):
        before = self.state.total_changes
        result = self.ready()
        self.assertEqual((result['criteria'], result['executed_checks'], result['assertions']), (1, 1, 2))
        self.assertEqual(before, self.state.total_changes)
        self.assertNotIn('executed_check_ids', result)

    def test_missing_failed_unexecuted_unsupported_error_and_invalid_proofs_block_closure(self):
        statements = [
            "DELETE FROM checks WHERE id=?",
            "UPDATE checks SET status='pending' WHERE id=?",
            "UPDATE checks SET status='running' WHERE id=?",
            "UPDATE checks SET verdict='fail' WHERE id=?",
            "UPDATE checks SET verdict='unsupported' WHERE id=?",
            "UPDATE checks SET verdict='error' WHERE id=?",
            "UPDATE checks SET verdict=NULL WHERE id=?",
            "UPDATE checks SET completed_at=NULL WHERE id=?",
            "UPDATE checks SET error='unresolved' WHERE id=?",
            "UPDATE checks SET feature='outline' WHERE id=?",
            "UPDATE checks SET subject='changed' WHERE id=?",
            "UPDATE checks SET expected_json=NULL WHERE id=?",
            "UPDATE checks SET actual_json=NULL WHERE id=?",
            "UPDATE checks SET actual_json='{}' WHERE id=?",
            "UPDATE checks SET expected_json='{}' WHERE id=?",
            "UPDATE checks SET diff_json=NULL WHERE id=?",
            "UPDATE checks SET diff_json='{}' WHERE id=?",
            "UPDATE checks SET diff_json='null' WHERE id=?",
            "UPDATE checks SET diff_json='{\"missing\":[1],\"unexpected\":[]}' WHERE id=?",
        ]
        for statement in statements:
            with self.subTest(statement=statement):
                self.state.execute('SAVEPOINT mutation')
                self.state.execute(statement, (self.identity,))
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
                self.state.execute('ROLLBACK TO mutation')
                self.state.execute('RELEASE mutation')

    def test_an_assertion_dropped_from_both_sides_is_not_a_pass(self):
        smaller = {'identity': self.samples['identity']}
        self.mutate('UPDATE checks SET expected_json=?,actual_json=? WHERE id=?',
                    (canonical_json({'source': 'synthetic', 'samples': smaller}), canonical_json(smaller), self.identity))
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'assertion population'):
            self.ready()

    def test_wrong_values_cannot_be_hidden_by_empty_diff(self):
        self.mutate('UPDATE checks SET actual_json=? WHERE id=?',
                    (canonical_json({**self.samples, 'identity': ['Other.java', 1, 'Other']}), self.identity))
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'expected behaviour'):
            self.ready()

    def test_every_missing_or_stale_edition_binding_blocks_closure(self):
        for key in acceptance.BINDINGS:
            with self.subTest(key=key):
                for value in (None, 'stale'):
                    with self.assertRaises(acceptance.AcceptancePending):
                        acceptance.readiness(self.state, {**self.bindings, key: value})
        self.mutate("UPDATE metadata SET value='changed' WHERE key='fixture_sha256'")
        with self.assertRaises(acceptance.AcceptancePending):
            self.ready()

    def test_replanning_cannot_erase_deleted_or_extra_failed_identities(self):
        self.mutate("INSERT INTO checks(id,feature,subject) VALUES ('extra',?,'future-criterion')", (self.feature,))
        with self.assertRaises(acceptance.AcceptancePending):
            self.ready()
        self.mutate("DELETE FROM checks WHERE id='extra'")
        acceptance.plan(self.state, java_only=True)
        with self.assertRaises(acceptance.AcceptancePending):
            self.ready()
        self.assertEqual(self.state.execute('SELECT count(*) FROM parent_acceptance_members').fetchone()[0], 2)

    def test_deleted_ledger_spec_changes_and_inapplicability_cannot_narrow_coverage(self):
        for sql in (
            'DELETE FROM parent_acceptance_members',
            'DELETE FROM parent_acceptance_editions',
            "UPDATE parent_acceptance_editions SET spec_sha256='changed'",
            "UPDATE coverage SET status='inapplicable' WHERE feature='global:format:java-file-views'",
            "UPDATE coverage SET status='out-of-scope' WHERE feature='global:format:java-file-views'",
        ):
            with self.subTest(sql=sql):
                self.state.execute('SAVEPOINT mutation')
                self.state.execute(sql)
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
                self.state.execute('ROLLBACK TO mutation')
                self.state.execute('RELEASE mutation')

    def test_missing_schema_and_new_advertised_java_commands_block_closure(self):
        from format_acceptance_spec import unmapped_commands
        read = Path.read_text

        def expanded(path, *args, **kwargs):
            content = read(path, *args, **kwargs)
            if path.name == 'main.rs':
                content = content.replace('enum Commands {', 'enum Commands {\n    NovelJavaProbe,', 1)
            return content

        self.assertEqual(unmapped_commands(), [])
        with patch.object(Path, 'read_text', expanded):
            self.assertEqual(unmapped_commands(), ['novel-java-probe'])
            with self.assertRaisesRegex(acceptance.AcceptancePending, 'advertised Java commands'):
                self.ready()
        self.state.execute('DROP TABLE parent_acceptance_editions')
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'missing format acceptance schema'):
            self.ready()

    def test_empty_narrowed_duplicate_and_unmapped_checklists_are_never_proof(self):
        criterion = self.spec['criteria'][0]
        for items in ([], [criterion, criterion], [{**criterion, 'fixture': ''}],
                      [{**criterion, 'feature': 'non-java-criterion'}]):
            with self.subTest(criteria=items), patch.object(acceptance, 'specification',
                    return_value={**self.spec, 'criteria': items}):
                with self.assertRaises(acceptance.AcceptancePending):
                    self.ready()
        with patch.object(acceptance, 'required_format_features', return_value={self.feature, 'missing-java-format'}):
            with self.assertRaisesRegex(acceptance.AcceptancePending, 'required API criteria'):
                self.ready()

    def test_scheduler_executes_children_before_acceptance(self):
        self.mutate("UPDATE checks SET status='pending',verdict=NULL WHERE id=?", (self.identity,))
        self.assertEqual(next_check(self.state)['id'], self.identity)

    def test_large_retained_populations_do_not_expand_parent_results(self):
        row = self.state.execute('SELECT expected_json,actual_json,diff_json FROM checks WHERE id=?',
                                 (self.identity,)).fetchone()
        with self.state:
            self.state.executemany('''INSERT INTO checks(id,feature,subject,status,verdict,
                expected_json,actual_json,diff_json,completed_at)
                VALUES (?,?,?,'complete','pass',?,?,?,1)''',
                ((f'additional-{number}', self.feature, f'synthetic-{number}', *row)
                 for number in range(10000)))
        result = self.ready()
        self.assertEqual(result['executed_checks'], 10001)
        self.assertLess(len(canonical_json(result)), 1024)
        self.mutate("UPDATE checks SET verdict='unsupported' WHERE id='additional-9999'")
        with self.assertRaises(acceptance.AcceptancePending):
            self.ready()

    def test_batch_replay_executes_children_instead_of_copying_historical_passes(self):
        source = connect(self.root / 'source.sqlite')
        target = connect(self.root / 'verification.sqlite')
        self.addCleanup(source.close)
        self.addCleanup(target.close)
        self.state.backup(source)
        with source:
            source.execute("UPDATE checks SET verdict='unsupported',error='old defect' WHERE id=?", (self.identity,))
        target.executescript(SCHEMA)
        with target:
            target.executemany('INSERT INTO metadata VALUES (?,?)', self.bindings.items())
        observed = []

        def evaluate(check):
            observed.append(check['id'])
            self.assertEqual(check['status'], 'pending')
            self.assertIsNone(check['expected_json'], 'historical proof must not be copied into a new edition')
            with target:
                target.execute('''UPDATE checks SET status='complete',verdict='pass',completed_at=2,
                    expected_json=?,actual_json=?,diff_json=? WHERE id=?''',
                    (canonical_json({'source': 'synthetic execution', 'samples': self.samples}),
                     canonical_json(self.samples), canonical_json({'missing': [], 'unexpected': []}), check['id']))

        with patch.object(acceptance, 'criteria', return_value=self.spec['criteria']):
            acceptance.replay_children(SimpleNamespace(state=target, evaluate=evaluate), source)
        self.assertEqual(observed, [self.identity])
        self.assertEqual(acceptance.readiness(target, self.bindings)['executed_checks'], 1)
        self.assertEqual(source.execute('SELECT verdict FROM checks WHERE id=?', (self.identity,)).fetchone()[0],
                         'unsupported')
        # An uncertain source identity retained only in the ledger is still
        # required; replay cannot silently omit it or create a shape-only pass.
        with source:
            source.execute('INSERT INTO parent_acceptance_members VALUES (?,?,?,?)',
                           (acceptance.FEATURE, 'lost-id', self.feature, 'lost-obligation'))
        with patch.object(acceptance, 'criteria', return_value=self.spec['criteria']):
            acceptance.replay_children(SimpleNamespace(state=target, evaluate=evaluate), source)
        with self.assertRaisesRegex(acceptance.AcceptancePending, 'missing or changed'):
            acceptance.readiness(target, self.bindings)

    def test_final_gate_calls_acceptance_instead_of_trusting_parent_status(self):
        # The original final-gate fixture supplies its synthetic population;
        # unmock the new component to test the integration's negative path.
        from test_completion import CompletionTests
        fixture = CompletionTests('test_complete_current_gate_input_passes_without_modifying_evidence')
        factory = tempfile.TemporaryDirectory
        directory = Path(__file__).resolve().parents[2] / '.artifacts/tests/parent-acceptance'
        with patch('test_completion.tempfile.TemporaryDirectory', side_effect=lambda: factory(dir=directory)):
            fixture.setUp()
        try:
            with patch('parent_acceptance.readiness', side_effect=acceptance.AcceptancePending('unproved parent')):
                with self.assertRaisesRegex(acceptance.AcceptancePending, 'unproved parent'):
                    fixture.check()
        finally:
            fixture.doCleanups()


class ParentPlanningTests(unittest.TestCase):
    def test_java_format_parent_has_an_executable_acceptance_check(self):
        with temporary_artifacts() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {}')
            state = connect(Path(directory) / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Example.java'}],
                 '  class  Classes\n  symbol  Symbols\n  file  Files', [], root, java_only=True)
            check = state.execute("SELECT subject,status FROM checks WHERE feature='global:format'").fetchone()
            self.assertIsNotNone(check, 'pending format parent has no executable acceptance check')
            self.assertEqual(check['status'], 'pending')
            self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='global:format'").fetchone()[0],
                             'pending', 'planning alone must never close a parent')


if __name__ == '__main__':
    unittest.main()
