"""Final readiness rejects stale, partial, scope-reduced and shape-only proof."""
from pathlib import Path
import tempfile
import unittest

from audit import JAVA_EXCLUDED_FEATURES, SCHEMA, required_features
from common import ToolError, adapter_digest, connect, file_sha256, source_snapshot
from completion import StaleEvidence, verify
from mobile_contracts import inventory_snapshot


class CompletionTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
