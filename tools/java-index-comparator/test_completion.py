"""Final readiness rejects stale, partial, scope-reduced and shape-only proof."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import JAVA_EXCLUDED_FEATURES, SCHEMA, required_features
from common import ToolError, adapter_digest, connect, file_sha256, source_snapshot
from completion import StaleEvidence, verify
import android_contracts
from mobile_contracts import inventory, inventory_snapshot


class CompletionTests(unittest.TestCase):
    def test_retained_mixed_cases_cannot_replace_missing_java_projection(self):
        from android_dependency_contracts import LEGACY_FEATURES, JAVA_FEATURES
        before = self.check()['checks']
        for feature in LEGACY_FEATURES:
            self.mutate("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,'complete','pass')",
                        ('legacy:' + feature, feature, 'retained-mixed'))
        self.assertEqual(self.check()['checks'], before)
        for feature in JAVA_FEATURES:
            self.mutate("UPDATE checks SET verdict='fail' WHERE feature=?", (feature,))
        with self.assertRaises(ToolError):
            self.check()
        self.mutate("UPDATE checks SET verdict='pass' WHERE feature IN (?,?)", tuple(sorted(JAVA_FEATURES)))
        self.assertEqual(self.check()['checks'], before)
        self.mutate('DELETE FROM coverage WHERE feature=?', (sorted(JAVA_FEATURES)[0],))
        with self.assertRaises(ToolError):
            self.check()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / 'Example.java').write_text('class Example {}')
        self.binary = self.directory / 'synthetic-binary'
        self.binary.write_bytes(b'synthetic fingerprint, never executed')
        self.evidence = self.directory / 'evidence.sqlite'
        self.state = connect(self.evidence)
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        metadata = {'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
                    'inventory_sha256': inventory_snapshot(self.root),
                    'binary_sha256': file_sha256(self.binary), 'fixture_sha256': adapter_digest(),
                    'audit_scope': 'java', 'java_files': '1'}
        with self.state:
            self.state.executemany('INSERT INTO metadata VALUES (?,?)', metadata.items())
            for feature in required_features() | JAVA_EXCLUDED_FEATURES | {'search:rank-presets'}:
                excluded = feature in JAVA_EXCLUDED_FEATURES
                self.state.execute('INSERT INTO coverage VALUES (?,?,?)',
                                   (feature, 'out-of-scope' if excluded else 'implemented', 'synthetic gate input'))
                if not excluded:
                    self.state.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,'complete','pass')",
                                       (feature, feature, 'synthetic-input'))

    def check(self):
        return verify(self.evidence, self.root, self.binary)

    def mutate(self, sql, parameters=()):
        with self.state:
            self.state.execute(sql, parameters)

    def test_complete_current_gate_input_passes_without_modifying_evidence(self):
        # These synthetic rows test the gate, not live-project MCP equivalence.
        changes = self.state.total_changes
        result = self.check()
        self.assertTrue(result['verified'])
        self.assertEqual(result['scope'], 'java')
        self.assertEqual(result['out_of_scope_features'], len(JAVA_EXCLUDED_FEATURES))
        self.assertEqual(self.state.total_changes, changes)

    def test_required_java_contract_cannot_be_dismissed_as_absent(self):
        self.mutate("UPDATE coverage SET status='inapplicable' WHERE feature='class'")
        self.mutate("DELETE FROM checks WHERE feature='class'")
        with self.assertRaises(ToolError):
            self.check()

    def test_android_target_requires_an_executed_absence_contract(self):
        self.mutate("UPDATE coverage SET status='inapplicable' WHERE feature='xml-usages:target'")
        self.mutate("DELETE FROM checks WHERE feature='xml-usages:target'")
        with self.assertRaises(ToolError):
            self.check()

    def prepare_android_absence(self):
        inventory(self.state, self.root)
        status, reason = android_contracts.applicability(self.state, self.root)
        self.assertEqual(status, 'inapplicable')
        for parent in ('xml-usages', 'resource-usages'):
            target = parent + ':target'
            self.mutate('UPDATE coverage SET status=?,reason=? WHERE feature=?', (status, reason, target))
            self.mutate('DELETE FROM checks WHERE feature=?', (target,))
            self.mutate("""INSERT OR REPLACE INTO checks(id,feature,subject,status,verdict,expected_json,actual_json)
                VALUES (?,?,'target-absence','complete','pass',?,?)""",
                        ('absence:' + parent, parent,
                         json.dumps({'source': android_contracts.REASON, 'samples': {'absence': True}}),
                         json.dumps({'absence': True})))

    def test_proven_android_absence_passes_the_read_only_final_gate(self):
        # Root files precede child files in the recorded inventory, unlike a
        # global path sort. Verify the actual inventory digest, not a new order.
        (self.root / 'z.txt').write_text('ordinary auxiliary file')
        (self.root / 'a').mkdir()
        (self.root / 'a/Helper.xml').write_text('<root/>')
        self.prepare_android_absence()
        changes = self.state.total_changes
        self.assertTrue(self.check()['verified'])
        self.assertEqual(self.state.total_changes, changes)

    def test_incomplete_or_shape_only_android_absence_proof_never_passes(self):
        mutations = (
            "DELETE FROM metadata WHERE key='android_applicability_sha256'",
            'DELETE FROM android_applicability',
            'UPDATE android_applicability SET marker=1',
            "UPDATE android_applicability SET sha256='changed'",
            'DELETE FROM file_inventory',
            "UPDATE checks SET subject='not-absence' WHERE id='absence:xml-usages'",
            "UPDATE checks SET expected_json=NULL WHERE id='absence:xml-usages'",
            "UPDATE checks SET actual_json='{}' WHERE id='absence:xml-usages'",
            "UPDATE checks SET actual_json='{\"absence\":1}' WHERE id='absence:xml-usages'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.prepare_android_absence()
                self.assertTrue(self.check()['verified'])
                self.mutate(mutation)
                with self.assertRaises(ToolError):
                    self.check()

    def test_every_stale_fingerprint_requires_a_fresh_audit(self):
        for key in ('project_root', 'snapshot_sha256', 'inventory_sha256', 'binary_sha256', 'fixture_sha256'):
            old = self.state.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()[0]
            with self.subTest(key=key):
                self.mutate('UPDATE metadata SET value=? WHERE key=?', ('stale', key))
                with self.assertRaises(StaleEvidence):
                    self.check()
                self.mutate('UPDATE metadata SET value=? WHERE key=?', (old, key))

    def test_real_source_change_is_detected_even_if_summary_was_green(self):
        (self.root / 'Example.java').write_text('class Changed {}')
        with self.assertRaises(StaleEvidence):
            self.check()

    def test_android_contract_change_invalidates_the_actual_final_gate(self):
        self.assertTrue(self.check()['verified'])
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed Android contract\n' if path.name == 'android_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            with self.assertRaises(StaleEvidence):
                self.check()

    def test_new_runtime_helpers_are_automatically_included_in_the_final_gate(self):
        directory = Path(__file__).resolve().parent
        future = directory / 'future_contracts.py'
        iterate, is_file, read, is_link = Path.iterdir, Path.is_file, Path.read_bytes, Path.is_symlink
        def files(path):
            yield from iterate(path)
            if path == directory:
                yield future
        with patch.object(Path, 'iterdir', files), \
                patch.object(Path, 'is_file', lambda path: path == future or is_file(path)), \
                patch.object(Path, 'read_bytes', lambda path: b'# new runtime contract\n' if path == future else read(path)):
            with self.assertRaises(StaleEvidence):
                self.check()
            with patch.object(Path, 'is_symlink', lambda path: path == future or is_link(path)):
                with self.assertRaisesRegex(ToolError, 'source link'):
                    self.check()

    def test_test_only_edits_do_not_change_the_execution_fingerprint(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed test only\n' if path.name.startswith('test_') else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertEqual(adapter_digest(), previous)

    def test_same_size_and_mtime_descriptor_edit_invalidates_actual_final_gate(self):
        path = self.root / 'pom.xml'
        path.write_text('<project><artifactId>one</artifactId></project>\n')
        self.mutate('UPDATE metadata SET value=? WHERE key=?',
                    (inventory_snapshot(self.root), 'inventory_sha256'))
        self.assertTrue(self.check()['verified'])
        stamp = path.stat()
        path.write_text('<project><artifactId>two</artifactId></project>\n')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(path.stat().st_size, stamp.st_size)
        self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
        with self.assertRaises(StaleEvidence):
            self.check()

    def test_known_pending_subcontracts_cannot_disappear_from_final_proof(self):
        for feature in ('module-route:budgets', 'detect-stacks:composition-budgets',
                        'android:syntax-resolution', 'xml-usages:target', 'resource-usages:target',
                        'call-tree:semantic-resolution', 'explore:ranking-budgets',
                        'graph:java-selection', 'graph:java-traversal', 'graph:lifecycle',
                        'graph:java-metrics', 'graph:java-top', 'graph:metrics-rendering',
                        'graph:java-path-pagination', 'graph:traversal-rendering',
                        'unused-deps:java-ownership', 'unused-deps:java-types', 'unused-deps:transitive',
                        'unused-deps:semantic-resolution', 'unused-deps:android-ownership', 'unused-deps:target'):
            with self.subTest(feature=feature):
                self.assertIn(feature, required_features())
                self.mutate('DELETE FROM coverage WHERE feature=?', (feature,))
                self.mutate('DELETE FROM checks WHERE feature=?', (feature,))
                with self.assertRaisesRegex(ToolError, 'omits required'):
                    self.check()
                self.mutate("INSERT INTO coverage VALUES (?,'implemented','synthetic gate input')", (feature,))
                self.mutate("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,'complete','pass')",
                            (feature, feature, 'synthetic-input'))

    def test_android_presence_inputs_cannot_change_under_a_green_final_gate(self):
        for name in ('gradle.properties', 'libs.versions.toml', 'plugins/android.gradle',
                     'build/Generated.java'):
            with self.subTest(input=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                before, after = (('class Generated { /* desktop.feature */ }\n',
                                  'class Generated { /* android.feature */ }\n') if path.suffix == '.java'
                                 else ('desktop.feature = true\n', 'android.feature = true\n'))
                path.write_text(before)
                self.mutate('UPDATE metadata SET value=? WHERE key=?',
                            (inventory_snapshot(self.root), 'inventory_sha256'))
                self.assertTrue(self.check()['verified'])
                stamp = path.stat()
                path.write_text(after)
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(path.stat().st_size, stamp.st_size)
                self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
                with self.assertRaises(StaleEvidence):
                    self.check()

    def test_unfinished_failure_error_unsupported_and_null_verdict_never_pass(self):
        for status, verdict in (('pending', None), ('running', None), ('complete', 'fail'),
                                ('complete', 'unsupported'), ('complete', 'error'), ('complete', None)):
            with self.subTest(status=status, verdict=verdict):
                self.mutate('UPDATE checks SET status=?,verdict=? WHERE id=?', (status, verdict, 'class'))
                with self.assertRaises(ToolError):
                    self.check()
        self.mutate("DELETE FROM checks")
        with self.assertRaises(ToolError):
            self.check()

    def test_pending_deleted_or_unexecuted_contract_does_not_count_as_coverage(self):
        for mutation in ("UPDATE coverage SET status='pending' WHERE feature='class'",
                         "DELETE FROM coverage WHERE feature='class'",
                         "DELETE FROM checks WHERE feature='class'"):
            with self.subTest(mutation=mutation):
                self.mutate(mutation)
                with self.assertRaises(ToolError):
                    self.check()
                self.mutate("INSERT OR REPLACE INTO coverage VALUES ('class','implemented','synthetic gate input')")
                self.mutate("INSERT OR REPLACE INTO checks(id,feature,subject,status,verdict) VALUES ('class','class','synthetic-input','complete','pass')")

    def test_java_contract_cannot_be_relabelled_and_foreign_pass_cannot_count(self):
        self.mutate("UPDATE coverage SET status='out-of-scope' WHERE feature='class'")
        with self.assertRaises(ToolError):
            self.check()
        self.mutate("UPDATE coverage SET status='implemented' WHERE feature='class'")
        self.mutate("INSERT INTO checks(id,feature,subject,status,verdict) VALUES ('foreign','composables','sample','complete','pass')")
        with self.assertRaises(ToolError):
            self.check()

    def test_legacy_foreign_pending_and_error_rows_remain_durable_without_counting(self):
        before = self.check()['checks']
        self.mutate("INSERT INTO checks(id,feature,subject,status,verdict) VALUES ('foreign-pending','xml-usages:syntax','legacy','pending',NULL)")
        self.mutate("INSERT INTO checks(id,feature,subject,status,verdict) VALUES ('foreign-error','composables','legacy','complete','error')")
        self.assertEqual(self.check()['checks'], before)


if __name__ == '__main__':
    unittest.main()
