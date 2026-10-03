import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import SCHEMA
from common import ToolError, canonical_json, connect, source_snapshot
from replay import ArchiveOracle, StoredOracle, problem_batch, replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        self.root.mkdir()
        (self.root / "A.java").write_text("package p; class A {}\n")
        self.binary = self.directory / "binary"
        self.binary.write_bytes(b"original binary")
        self.evidence = self.directory / "evidence.sqlite"
        self.source = connect(self.evidence)
        self.addCleanup(self.source.close)
        self.source.executescript(SCHEMA)
        self.arguments = {"project_path": str(self.root), "query": "A", "language": "Java",
                          "matchMode": "exact", "scope": "project_files", "includeGenerated": False, "pageSize": 500}
        with self.source:
            self.source.executemany("INSERT INTO metadata VALUES (?,?)", {
                "project_root": str(self.root), "snapshot_sha256": source_snapshot(self.root)[0],
            }.items())
            self.source.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES ('case','class-qualified','A','complete','fail')")
            self.source.execute("INSERT INTO pages VALUES ('case',0,?,?,?)", (
                canonical_json(self.arguments), canonical_json({"classes": [{"name": "A", "file": "A.java", "line": 1, "qualifiedName": "p.A"}]}), "ide_find_class"))

    def test_scope_and_tool_are_bound_to_the_recorded_reference(self):
        with self.assertRaisesRegex(ToolError, "tool differs"):
            StoredOracle(self.source, "case").call("ide_find_symbol", self.arguments)
        with self.assertRaisesRegex(ToolError, "scope/query"):
            StoredOracle(self.source, "case").call("ide_find_class", {**self.arguments, "scope": "project_and_libraries"})

    def test_supplemental_archive_never_reuses_a_different_query_or_scope(self):
        from unittest.mock import Mock
        live = Mock()
        live.call.return_value = {'classes': []}
        oracle = ArchiveOracle(self.source, 'case', live)
        self.assertTrue(oracle.call('ide_find_class', self.arguments)['classes'])
        live.call.assert_not_called()
        query = {**self.arguments, 'query': 'Other'}
        oracle.call('ide_find_class', query)
        live.call.assert_called_once_with('ide_find_class', query)
        with self.assertRaisesRegex(ToolError, 'operation is missing'):
            ArchiveOracle(self.source, 'case').call('ide_find_class', {**self.arguments, 'scope': 'project_test_files'})

    def test_supplemental_snapshot_is_validated_before_build(self):
        archive = self.directory / 'archive.sqlite'
        other = connect(archive)
        other.executescript(SCHEMA)
        with other:
            other.execute("INSERT INTO metadata VALUES ('project_root','different')")
        other.close()
        with patch('replay.build_ast_index') as build:
            with self.assertRaisesRegex(ToolError, 'supplemental oracle target'):
                replay(self.evidence, self.root, self.binary, self.directory / 'replays', oracle_evidence=archive)
            build.assert_not_called()

    def test_failure_limit_does_not_drop_unsupported_or_error_contracts(self):
        with self.source:
            for subject, verdict in (("second", "fail"), ("unknown", "unsupported"), ("broken", "error")):
                self.source.execute("INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,'symbol',?,'complete',?)",
                                    (subject, subject, verdict))
        self.assertEqual([row["id"] for row in problem_batch(self.source, 1)], ["case", "unknown", "broken"])

    def test_replay_executes_fixture_and_does_not_reuse_old_actual_results(self):
        output = self.directory / "replays"
        with patch("replay.build_ast_index"), patch("audit.Fixture.cli", return_value={"items": [{"name": "A", "path": "A.java", "line": 1}]}) as cli:
            result = replay(self.evidence, self.root, self.binary, output)
            self.assertFalse(result["verified"])
            self.assertEqual(result["counts"], {"fail": 1})
            cli.assert_called_once_with("class", "A", "--limit", "1000000")
        self.binary.write_bytes(b"fixed binary")
        with patch("replay.build_ast_index"), patch("audit.Fixture.cli", return_value={"items": [{"name": "A", "path": "A.java", "line": 1, "qualified_name": "p.A"}]}):
            result = replay(self.evidence, self.root, self.binary, output)
            self.assertTrue(result["verified"])
            self.assertEqual(result["counts"], {"pass": 1})

    def test_replay_preserves_the_captured_java_scope(self):
        with self.source:
            self.source.execute("INSERT INTO metadata VALUES ('audit_scope','java')")
        with patch('replay.build_ast_index'), patch('audit.Fixture.cli', return_value={
                'items': [{'name': 'A', 'path': 'A.java', 'line': 1, 'qualified_name': 'p.A'}]}):
            result = replay(self.evidence, self.root, self.binary, self.directory / 'replays')
        self.assertTrue(result['verified'])
        state = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(state.execute("SELECT value FROM metadata WHERE key='audit_scope'").fetchone()[0], 'java')
        finally:
            state.close()

    def test_unused_recorded_operations_prevent_a_replay_pass(self):
        with self.source:
            self.source.execute("INSERT INTO pages VALUES ('case',1,?,?,?)", (
                canonical_json({"file": "A.java", "line": 1}), canonical_json({"references": []}), "ide_find_references"))
        with patch("replay.build_ast_index"), patch("audit.Fixture.cli", return_value={"items": [{"name": "A", "path": "A.java", "line": 1, "qualified_name": "p.A"}]}):
            result = replay(self.evidence, self.root, self.binary, self.directory / "replays")
        self.assertFalse(result["verified"])
        self.assertEqual(result["counts"], {"error": 1})

    def test_changed_sources_fail_before_native_build(self):
        (self.root / "A.java").write_text("package p; class Changed {}\n")
        with patch("replay.build_ast_index") as build:
            with self.assertRaisesRegex(ToolError, "source snapshot"):
                replay(self.evidence, self.root, self.binary, self.directory / "replays")
            build.assert_not_called()

    def test_revalidated_batch_preserves_pending_coverage(self):
        with self.source:
            self.source.execute("INSERT INTO coverage VALUES ('symbol:options','pending','missing contract')")
        with patch("replay.build_ast_index"), patch("audit.Fixture.cli", return_value={"items": []}):
            result = replay(self.evidence, self.root, self.binary, self.directory / "replays")
        state = connect(Path(result["verification"]), read_only=True)
        try:
            self.assertEqual(tuple(state.execute("SELECT * FROM coverage").fetchone()),
                             ('symbol:options', 'pending', 'missing contract'))
            metadata = dict(state.execute("SELECT key,value FROM metadata"))
            self.assertIn("fixture_sha256", metadata)
            self.assertEqual(metadata["original_evidence"], str(self.evidence))
        finally:
            state.close()

    def test_changed_descriptor_fails_before_build_even_with_preserved_stat(self):
        from mobile_contracts import inventory_snapshot
        for name in ('pom.xml', 'gradle.properties', 'libs.versions.toml', 'plugins/android.gradle',
                     'build/Generated.java'):
            with self.subTest(input=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('public-synthetic-one\n')
                with self.source:
                    self.source.execute("INSERT OR REPLACE INTO metadata VALUES ('inventory_sha256',?)",
                                        (inventory_snapshot(self.root),))
                stamp = path.stat()
                path.write_text('public-synthetic-two\n')
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(path.stat().st_size, stamp.st_size)
                self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
                with patch('replay.build_ast_index') as build:
                    with self.assertRaisesRegex(ToolError, 'inventory differs'):
                        replay(self.evidence, self.root, self.binary, self.directory / 'replays')
                    build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
