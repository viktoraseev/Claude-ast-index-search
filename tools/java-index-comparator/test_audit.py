import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit import Fixture, InvocationOracle, SCHEMA, Unsupported, declaration_keys, plan, type_keys
from common import connect


class LiveFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = connect(self.root / "evidence.sqlite")
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        plan(self.state, [{"path": "A.java"}], "  class  Find classes\n  refs  Find references")
        self.client = Mock()
        self.fixture = Fixture(self.root, Path("/binary"), self.root / "index.sqlite", self.state, self.client)

    def test_missing_qualified_name_is_a_failure_not_an_identity_pass(self):
        item = {"name": "A", "file": "A.java", "line": 1, "qualifiedName": "p.A"}
        self.client.call.return_value = {"classes": [item]}
        self.fixture.cli = Mock(return_value={"items": [{"name": "A", "path": "A.java", "line": 1}]})
        check = self.state.execute("SELECT * FROM checks WHERE feature='class-qualified'").fetchone()
        self.fixture.evaluate(check)
        row = self.state.execute("SELECT * FROM checks WHERE id=?", (check["id"],)).fetchone()
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(json.loads(row["diff_json"])["missing"], [["A", "A.java", 1, "p.A"]])
        self.fixture.cli.assert_called_once_with("class", "A", "--limit", "1000000")

    def test_truncated_native_result_never_passes(self):
        self.client.call.return_value = {"classes": []}
        self.fixture.cli = Mock(return_value={"items": [], "pagination": {"has_more": True}})
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        self.fixture.evaluate(check)
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id=?", (check["id"],)).fetchone()[0], "unsupported")

    def test_pagination_is_followed_and_each_page_is_persisted(self):
        check = self.state.execute("SELECT * FROM checks LIMIT 1").fetchone()
        self.client.call.side_effect = [{"classes": [{"name": "A"}], "nextCursor": "next"}, {"classes": [{"name": "B"}]}]
        items = self.fixture.paginated(check["id"], "ide_find_class", {"query": "A"}, "classes")
        self.assertEqual(len(items), 2)
        self.assertEqual(self.state.execute("SELECT count(*) FROM pages").fetchone()[0], 2)
        self.assertEqual(self.client.call.call_args.args[1]["cursor"], "next")

    def test_repeated_cursor_is_not_complete_evidence(self):
        check = self.state.execute("SELECT * FROM checks LIMIT 1").fetchone()
        self.client.call.return_value = {"classes": [], "nextCursor": "next"}
        with self.assertRaisesRegex(Unsupported, "repeated cursor"):
            self.fixture.paginated(check["id"], "ide_find_class", {}, "classes")

    def test_text_collection_cap_is_not_complete_evidence(self):
        check = self.state.execute("SELECT * FROM checks LIMIT 1").fetchone()
        self.client.call.return_value = {"matches": [{"file": "A.java", "line": 1}] * 5000,
                                         "hasMore": False, "totalCollected": 5000}
        with self.assertRaisesRegex(Unsupported, "collection cap"):
            self.fixture.paginated(check["id"], "ide_search_text", {"query": "e"}, "matches")

    def test_reference_usages_response_is_paginated_without_losing_imports(self):
        check = self.state.execute("SELECT * FROM checks LIMIT 1").fetchone()
        self.client.call.side_effect = [
            {"usages": [{"file": "A.java", "line": 1, "type": "IMPORT"}], "nextCursor": "next"},
            {"usages": [{"file": "A.java", "line": 2, "type": "REFERENCE"}], "totalIsExact": True},
        ]
        items = self.fixture.paginated(check["id"], "ide_find_references", {}, "references")
        self.assertEqual([item["type"] for item in items], ["IMPORT", "REFERENCE"])

    def test_external_paths_are_not_silently_compared_as_local(self):
        with self.assertRaises(Unsupported):
            type_keys([{"name": "A", "file": "/elsewhere/A.java", "line": 1}], self.root, "A")

    def test_database_path_compares_symlink_identity_not_spelling(self):
        database = self.root / "index.sqlite"
        database.touch()
        alias = self.root / "alias.sqlite"
        alias.symlink_to(database)
        self.fixture.text_cli = Mock(return_value=str(alias))
        self.fixture.evaluate(self.state.execute("SELECT * FROM checks WHERE feature='db-path'").fetchone())
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE feature='db-path'").fetchone()[0], "pass")

    def test_imports_use_only_mcp_confirmed_code_anchors_and_utf16_columns(self):
        prefix = '@Label("😀") package p; '
        source = prefix + 'import static a.b.C.*;\nimport unconfirmed.Name;\n'
        (self.root / "A.java").write_text(source)
        self.client.call.return_value = {"matches": [{"file": "A.java", "line": 1,
            "column": len(prefix.encode("utf-16-le")) // 2 + 1}]}
        self.fixture.text_cli = Mock(return_value="Imports in A.java:\n  static a.b.C.*\nTotal: 1 imports\n")
        check = self.state.execute("SELECT * FROM checks WHERE feature='imports' AND subject='A.java'").fetchone()
        self.fixture.evaluate(check)
        outcome = self.state.execute("SELECT verdict,expected_json FROM checks WHERE id=?", (check["id"],)).fetchone()
        self.assertEqual(outcome["verdict"], "pass")
        self.assertEqual(json.loads(outcome["expected_json"]), ["static a.b.C.*"])
        arguments = self.client.call.call_args.args[1]
        self.assertEqual(arguments["context"], "code")
        self.assertEqual(arguments["paths"], ["A.java"])

    def test_complete_read_only_requests_reuse_disk_cache_within_invocation(self):
        self.client.call.return_value = {"symbols": [{"name": "A"}], "hasMore": False}
        oracle = InvocationOracle(self.client, self.state)
        request = {"project_path": str(self.root), "query": "A", "scope": "project_files"}
        self.assertEqual(oracle.call("ide_find_symbol", request), oracle.call("ide_find_symbol", request))
        self.client.call.assert_called_once()
        oracle.call("ide_find_symbol", {**request, "scope": "project_test_files"})
        self.assertEqual(self.client.call.call_count, 2)

    def test_partial_and_stale_answers_are_not_cached(self):
        oracle = InvocationOracle(self.client, self.state)
        for response in ({"symbols": [], "nextCursor": "next"}, {"symbols": [], "stale": True}):
            self.client.call.return_value = response
            oracle.call("ide_find_symbol", {"query": "A"})
            oracle.call("ide_find_symbol", {"query": "A"})
        self.assertEqual(self.client.call.call_count, 4)
        self.assertEqual(self.state.execute("SELECT count(*) FROM invocation_cache").fetchone()[0], 0)

    def test_symbol_queries_share_complete_short_query_without_extra_declarations(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        items = [{"name": name, "kind": "METHOD", "file": "A.java", "line": 1}
                 for name in ("run", "read", "other")]
        self.client.call.return_value = {"symbols": items}
        self.fixture.client = InvocationOracle(self.client, self.state)
        self.assertEqual([i["name"] for i in self.fixture.oracle_symbols(check, "run")], ["run"])
        self.assertEqual([i["name"] for i in self.fixture.oracle_symbols(check, "read")], ["read"])
        self.client.call.assert_called_once()
        self.assertEqual(self.client.call.call_args.args[1]["query"], "r")
        self.assertEqual([json.loads(row[0])["query"] for row in self.state.execute("SELECT request_json FROM pages ORDER BY page")],
                         ["r", "r"])

    def test_symbol_collection_cap_refines_query_and_keeps_both_oracle_operations(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        item = {"name": "run", "kind": "METHOD", "file": "A.java", "line": 1}
        self.client.call.side_effect = [{"symbols": [item] * 500}, {"symbols": [item]}]
        self.assertEqual(self.fixture.oracle_symbols(check, "run"), [item])
        self.assertEqual([call.args[1]["query"] for call in self.client.call.call_args_list], ["r", "ru"])
        self.assertEqual(self.state.execute("SELECT count(*) FROM pages").fetchone()[0], 2)

    def test_stale_short_query_is_not_refined_into_an_apparent_success(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        self.client.call.return_value = {"symbols": [], "stale": True}
        with self.assertRaisesRegex(Unsupported, "stale"):
            self.fixture.oracle_symbols(check, "run")
        self.client.call.assert_called_once()

    def test_navigation_mismatch_is_confirmed_with_the_full_name_query(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        broad = [{"name": "run", "kind": "METHOD", "file": "A.java", "line": 1}]
        self.client.call.return_value = {"symbols": []}
        self.assertEqual(self.fixture.validate_navigation(check, "run", broad, Counter()), [])
        self.assertEqual(self.client.call.call_args.args[1]['query'], 'run')

    def test_coarse_oracle_kinds_do_not_turn_enum_constants_into_types(self):
        identity = {"name": "OPEN", "file": "Status.java", "line": 2, "qualifiedName": "p.Status.OPEN"}
        expected = declaration_keys([{**identity, "kind": "CLASS"}], self.root, "OPEN")
        actual = declaration_keys([{**identity, "kind": "constant"}], self.root, "OPEN")
        self.assertEqual(expected, actual)
        self.assertEqual(declaration_keys([{**identity, "kind": "SYMBOL"}], self.root, "OPEN"), actual)

    def test_all_operations_in_a_check_are_retained_for_replay(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='class'").fetchone()
        self.client.call.side_effect = [{"symbols": []}, {"references": []}]
        self.fixture.paginated(check["id"], "ide_find_symbol", {"query": "A"}, "symbols")
        self.fixture.paginated(check["id"], "ide_find_references", {"file": "A.java", "line": 1}, "references")
        self.assertEqual([tuple(row) for row in self.state.execute('SELECT page,tool FROM pages ORDER BY page')],
                         [(0, "ide_find_symbol"), (1, "ide_find_references")])

    def test_semantic_checks_are_scheduled_only_for_confirmed_declarations(self):
        plan(self.state, [{"path": "A.java"}], "  class  Find classes", candidates=["A", "keyword"])
        self.assertFalse(self.state.execute("SELECT 1 FROM checks WHERE feature='usages'").fetchone())
        self.client.call.return_value = {"symbols": [{"name": "A", "kind": "METHOD", "file": "A.java", "line": 1}]}
        self.fixture.cli = Mock(return_value={"items": []})
        check = self.state.execute("SELECT * FROM checks WHERE feature='symbol' AND subject='A'").fetchone()
        self.fixture.evaluate(check)
        self.assertEqual([row[0] for row in self.state.execute("SELECT feature FROM checks WHERE subject='A' AND feature IN ('refs','usages','callers') ORDER BY feature")],
                         ["callers", "refs", "usages"])
        self.assertFalse(self.state.execute("SELECT 1 FROM checks WHERE feature='usages' AND subject='keyword'").fetchone())

    def test_normal_method_qualified_names_are_still_compared(self):
        (self.root / "A.java").write_text("package example;\nclass A { void run() {} }\n")
        expected = {"name": "run", "kind": "METHOD", "file": "A.java", "line": 2, "qualifiedName": "example.A.run"}
        actual = {"name": "run", "kind": "function", "path": "A.java", "line": 2}
        self.assertNotEqual(declaration_keys([expected], self.root, "run"), declaration_keys([actual], self.root, "run"))

    def test_same_line_overloads_are_not_collapsed_into_one_declaration(self):
        (self.root / "A.java").write_text("package example;\nclass A { void run() {} void run(int x) {} }\n")
        plan(self.state, [{"path": "A.java"}], "  class  Find classes", candidates=["run"])
        item = {"name": "run", "kind": "METHOD", "file": "A.java", "line": 2,
                "qualifiedName": "example.A.run"}
        self.client.call.return_value = {"symbols": [item, item]}
        self.fixture.cli = Mock(return_value={"items": [{**item, "kind": "function"}]})
        check = self.state.execute("SELECT * FROM checks WHERE feature='symbol' AND subject='run'").fetchone()
        self.fixture.evaluate(check)
        outcome = self.state.execute("SELECT verdict,diff_json FROM checks WHERE id=?", (check["id"],)).fetchone()
        self.assertEqual(outcome["verdict"], "fail")
        self.assertEqual(len(json.loads(outcome["diff_json"])["missing"]), 1)


if __name__ == "__main__":
    unittest.main()
