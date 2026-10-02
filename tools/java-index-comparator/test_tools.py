import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import collect as collector
import common
import compare as comparator
import case_fixture


class ToolTests(unittest.TestCase):
    def test_case_fixture_supports_known_case_and_rejects_unknown_shape(self):
        supported = case_fixture.DatabaseCase(
            case_id="case-1",
            feature="usages",
            subject="Thing",
            verdict="mismatch",
            missing_count=1,
            unexpected_count=0,
            oracle=[{"file": "src/Thing.java", "line": 3}],
            actual=[],
            diff={"missing": [["src/Thing.java", 3]], "unexpected": []},
        )
        supported_outcome = case_fixture.process_case(supported)
        self.assertTrue(supported_outcome.supported)
        self.assertEqual(supported_outcome.assertion_count, 1)

        unsupported = case_fixture.DatabaseCase(
            case_id="case-2",
            feature="usages",
            subject="Thing",
            verdict="mismatch",
            missing_count=1,
            unexpected_count=0,
            oracle=[{"file": "src/Thing.java", "line": 3}],
            actual=[],
            diff={"missing": [["src/Thing.java", 3, "unknown"]], "unexpected": []},
        )
        unsupported_outcome = case_fixture.process_case(unsupported)
        self.assertFalse(unsupported_outcome.supported)
        self.assertIn("must be [file, line]", unsupported_outcome.reason)

    def test_case_fixture_streams_first_cases_in_stable_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "comparison.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(comparator.COMPARISON_SCHEMA)
            for case_id in ("b", "a"):
                connection.execute(
                    """
                    INSERT INTO cases VALUES (?, 'file', ?, 'mismatch', 1, 0, ?, ?, ?)
                    """,
                    (
                        case_id, case_id,
                        json.dumps(["Expected.java"]),
                        json.dumps([]),
                        json.dumps({"missing": ["Expected.java"], "unexpected": []}),
                    ),
                )
            connection.commit()
            connection.close()

            outcomes = case_fixture.run_fixture(database, limit=1)
            self.assertEqual([outcome.case_id for outcome in outcomes], ["a"])
            self.assertTrue(outcomes[0].supported)

    def test_case_fixture_supports_every_comparison_feature_contract(self):
        declaration_oracle = [{"name": "A", "kind": "CLASS", "file": "A.java", "line": 1}]
        declaration_diff = ["A", "class", "A.java", 1, "p.A"]
        location_oracle = [{"file": "A.java", "line": 2}]
        location_diff = ["A.java", 2]
        cases = [
            ("file", ["A.java"], [], "A.java"),
            ("symbol", declaration_oracle, [], declaration_diff),
            ("class", declaration_oracle, [], declaration_diff),
            ("outline", declaration_oracle, [], declaration_diff),
            ("usages", location_oracle, [], location_diff),
            ("imports", location_oracle, [], location_diff),
            ("implementations", location_oracle, [], location_diff),
            ("callers", location_oracle, [], location_diff),
            (
                "refs",
                {"definitions": declaration_oracle, "usages": location_oracle},
                {"definitions": [], "usages": []},
                ["usage", "A.java", 2],
            ),
            (
                "hierarchy",
                {"parents": ["Base"], "children": []},
                {"parents": [], "children": []},
                ["parent", "Base"],
            ),
        ]
        outcomes = []
        for index, (feature, oracle, actual, atom) in enumerate(cases):
            outcomes.append(case_fixture.process_case(case_fixture.DatabaseCase(
                case_id=f"case-{index}",
                feature=feature,
                subject="A",
                verdict="mismatch",
                missing_count=1,
                unexpected_count=0,
                oracle=oracle,
                actual=actual,
                diff={"missing": [atom], "unexpected": []},
            )))
        self.assertEqual(len(outcomes), 10)
        self.assertTrue(all(outcome.supported for outcome in outcomes), outcomes)

    def test_java_lexer_discards_comments_and_literals(self):
        source = '''
            class Kept {
                // HiddenComment
                String value = "HiddenString";
                String block = """HiddenTextBlock""";
                char marker = 'H';
                void alsoKept() {}
            }
        '''
        code = common.java_code_without_literals(source)
        self.assertIn("Kept", code)
        self.assertIn("alsoKept", code)
        self.assertNotIn("HiddenComment", code)
        self.assertNotIn("HiddenString", code)
        self.assertNotIn("HiddenTextBlock", code)

    def test_partition_names_keeps_identifier_that_is_also_a_prefix(self):
        queries = collector.partition_names({"A", "Ab", "Ac"}, bucket_size=1)
        self.assertEqual(queries, [("exact", "A"), ("prefix", "Ab"), ("prefix", "Ac")])

    def test_streamable_http_client_initializes_lists_and_calls(self):
        class Headers(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        class Response:
            def __init__(self, body):
                self.body = body
                self.headers = Headers({"Content-Type": "application/json"})

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body

        def urlopen(http_request, timeout):
            message = json.loads(http_request.data)
            if "id" not in message:
                return Response(b"")
            if message["method"] == "initialize":
                result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}}
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": "ide_find_symbol"}]}
            else:
                result = {
                    "content": [{"type": "text", "text": json.dumps({"symbols": [{"name": "A"}]})}],
                    "isError": False,
                }
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            return Response(body)

        with mock.patch.object(common.urllib_request, "urlopen", side_effect=urlopen):
            client = common.StreamableHttpMcpClient("http://127.0.0.1/mcp", timeout=2)
            self.assertEqual(client.initialize()["serverInfo"]["name"], "fake")
            self.assertIn("ide_find_symbol", client.tools())
            self.assertEqual(client.call("ide_find_symbol", {"query": "A"})["symbols"][0]["name"], "A")

    def test_store_page_keeps_only_exact_symbol_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "mcp.sqlite"
            connection = common.connect(database)
            connection.executescript(collector.MCP_SCHEMA)
            collector.add_task(
                connection,
                "inventory",
                "symbol_reconcile_exact",
                "Exact",
                "ide_find_symbol",
                {"query": "Exact"},
            )
            connection.commit()
            task = connection.execute("SELECT * FROM tasks").fetchone()
            collector.store_page(
                connection,
                task,
                0,
                {"query": "Exact"},
                {
                    "symbols": [
                        {
                            "name": "Exact", "qualifiedName": "p.Exact", "kind": "CLASS",
                            "file": "src/Exact.java", "line": 1, "column": 7, "language": "Java",
                        },
                        {
                            "name": "ExactMore", "qualifiedName": "p.ExactMore", "kind": "CLASS",
                            "file": "src/ExactMore.java", "line": 1, "column": 7, "language": "Java",
                        },
                        {
                            "name": "Inexact", "qualifiedName": "p.Inexact", "kind": "CLASS",
                            "file": "src/Inexact.java", "line": 1, "column": 7, "language": "Java",
                        },
                    ]
                },
            )
            names = [row[0] for row in connection.execute("SELECT name FROM mcp_symbols")]
            self.assertEqual(names, ["Exact"])
            connection.close()

    def test_overflowing_prefix_is_split_into_child_tasks(self):
        class Client:
            def call(self, _tool, _arguments):
                return {
                    "symbols": [
                        {
                            "name": "Alpha", "qualifiedName": "p.Alpha", "kind": "CLASS",
                            "file": "src/Alpha.java", "line": 1, "language": "Java",
                        }
                    ],
                    "totalCount": 500,
                    "nextCursor": "ignored-after-overflow",
                }

        with tempfile.TemporaryDirectory() as temporary:
            connection = common.connect(Path(temporary) / "mcp.sqlite")
            connection.executescript(collector.MCP_SCHEMA)
            connection.executemany(
                "INSERT INTO candidate_names VALUES ('symbol', ?)",
                (("Alpha",), ("Alpine",), ("Beta",)),
            )
            collector.add_task(
                connection,
                "inventory",
                "symbol_prefix",
                "Al",
                "ide_find_symbol",
                {"query": "Al*", "pageSize": 10, "project_path": "/project"},
            )
            connection.commit()
            task = connection.execute("SELECT * FROM tasks WHERE subject_key = 'Al'").fetchone()
            collector.run_task(Client(), connection, task)
            children = {
                row[0]
                for row in connection.execute(
                    "SELECT subject_key FROM tasks WHERE operation = 'symbol_prefix'"
                )
            }
            self.assertEqual(children, {"Al", "Alp"})
            connection.close()

    def test_java_call_sites_reproduce_definition_line_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Calls.java"
            source.write_text(
                "void target() {}\n"
                "void wrapper() { target(); }\n"
                "void target(int value) { target(); }\n",
                encoding="utf-8",
            )
            shaped, actual = comparator.collect_java_call_sites(
                root, ["Calls.java"], {"target"}
            )
            self.assertEqual(shaped["target"], {("Calls.java", 1), ("Calls.java", 2), ("Calls.java", 3)})
            self.assertEqual(actual["target"], {("Calls.java", 2)})

    def test_comparison_database_contains_mismatch_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mcp_path = root / "mcp.sqlite"
            ast_path = root / "ast.sqlite"
            output_path = root / "comparison.sqlite"

            mcp = common.connect(mcp_path)
            mcp.executescript(collector.MCP_SCHEMA)
            mcp.execute("INSERT INTO metadata VALUES ('source_snapshot_sha256', 'same')")
            mcp.execute(
                """
                INSERT INTO tasks(
                    id, phase, operation, subject_key, tool, arguments_json,
                    status, created_at_ms
                ) VALUES ('task', 'inventory', 'symbol_search', 'A',
                          'ide_find_symbol', '{}', 'complete', 0)
                """
            )
            mcp.execute(
                "INSERT INTO mcp_files VALUES (?, ?, ?, ?, ?)",
                ("src/A.java", "A.java", "src", "task", "{}"),
            )
            mcp.execute(
                """
                INSERT INTO mcp_symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("symbol", "A", "p.A", "CLASS", "src/A.java", 1, 7, None, "Java", "task", "{}"),
            )
            mcp.commit()
            mcp.close()

            ast = common.connect(ast_path)
            ast.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);
                CREATE TABLE symbols(
                    id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT,
                    qualified_name TEXT, kind TEXT, line INTEGER, signature TEXT
                );
                CREATE TABLE refs(id INTEGER, file_id INTEGER, name TEXT, line INTEGER, context TEXT);
                CREATE TABLE inheritance(id INTEGER, child_id INTEGER, parent_name TEXT, kind TEXT);
                INSERT INTO metadata VALUES ('java_comparator_snapshot_sha256', 'same');
                INSERT INTO files VALUES (1, 'src/A.java');
                INSERT INTO symbols VALUES (1, 1, 'Wrong', NULL, 'class', 1, 'class Wrong');
                """
            )
            ast.commit()
            ast.close()

            summary = comparator.build_comparison(
                mcp_path,
                ast_path,
                output_path,
                require_complete=False,
                verify_current_snapshot=False,
            )

            self.assertGreater(summary["mismatch_cases"], 0)
            result = common.connect(output_path, read_only=True)
            self.assertEqual(
                result.execute("SELECT count(*) FROM cases WHERE verdict='mismatch'").fetchone()[0],
                summary["mismatch_cases"],
            )
            result.close()


if __name__ == "__main__":
    unittest.main()
