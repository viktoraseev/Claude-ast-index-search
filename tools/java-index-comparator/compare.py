#!/usr/bin/env python3
"""Compare ast-index.sqlite with mcp-index.sqlite into comparison.sqlite."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable

from common import (
    SCHEMA_VERSION,
    ToolError,
    canonical_json,
    connect,
    now_ms,
    source_snapshot,
    stable_id,
)


TYPE_KINDS = {"CLASS", "INTERFACE", "ENUM", "RECORD", "ANNOTATION"}

COVERAGE = [
    ("file", "direct", "file inventory"),
    ("symbol", "direct", "declaration inventory"),
    ("class", "direct", "type declaration inventory"),
    ("outline", "derived", "declarations grouped by file"),
    ("refs", "direct", "definition + references"),
    ("usages", "direct", "references"),
    ("implementations", "direct", "implementations"),
    ("hierarchy", "direct", "type hierarchy"),
    ("callers", "direct", "MCP-resolved references versus ast-index Java grep call sites"),
    ("call-tree", "not-comparable", "ast-index has text-only grep tree; MCP returns semantic method tree"),
    ("search", "derived", "file + symbol primitives"),
    ("explore", "derived", "search + refs + source ranking"),
    ("api", "derived", "public declaration filter"),
    ("unused-symbols", "derived", "declarations + references"),
    ("map", "derived", "declarations grouped by directory"),
    ("conventions", "derived", "heuristics over indexed primitives"),
    ("imports", "direct", "import references"),
    ("todo", "lexical", "not a Java semantic operation"),
    ("annotations", "lexical", "not a Java semantic operation"),
    ("deprecated", "lexical", "not a Java semantic operation"),
    ("suppress", "lexical", "not a Java semantic operation"),
    ("provides", "lexical", "not a Java semantic operation"),
    ("inject", "lexical", "not a Java semantic operation"),
    ("deeplinks", "lexical", "not a Java semantic operation"),
    ("agrep", "external", "delegates to ast-grep"),
    ("module", "not-java-ast", "build model"),
    ("deps", "not-java-ast", "build model"),
    ("dependents", "not-java-ast", "build model"),
    ("module-route", "not-java-ast", "build model"),
    ("unused-deps", "not-java-ast", "build model"),
    ("xml-usages", "not-java-ast", "Android resource model"),
    ("resource-usages", "not-java-ast", "Android resource model"),
    ("suspend", "not-java", "Kotlin-only"),
    ("composables", "not-java", "Kotlin-only"),
    ("flows", "not-java", "Kotlin-only"),
    ("extensions", "not-java", "Kotlin-only"),
    ("previews", "not-java", "Kotlin/Android convention"),
    ("storyboard-usages", "not-java", "iOS-only"),
    ("asset-usages", "not-java", "iOS-only"),
    ("swiftui", "not-java", "Swift-only"),
    ("async-funcs", "not-java", "Swift-only"),
    ("publishers", "not-java", "Swift-only"),
    ("main-actor", "not-java", "Swift-only"),
    ("perl-exports", "not-java", "Perl-only"),
    ("perl-subs", "not-java", "Perl-only"),
    ("perl-pod", "not-java", "Perl-only"),
    ("perl-tests", "not-java", "Perl-only"),
    ("perl-imports", "not-java", "Perl-only"),
    ("rebuild", "collector", "snapshot creation"),
    ("update", "state-only", "freshness operation"),
    ("restore", "state-only", "lifecycle operation"),
    ("stats", "sanity", "database counts"),
    ("clear", "state-only", "lifecycle operation"),
    ("watch", "state-only", "freshness operation"),
    ("watch-status", "state-only", "freshness operation"),
    ("version", "external", "binary metadata"),
    ("changed", "not-java-ast", "VCS operation"),
    ("add-root", "configuration", "root configuration"),
    ("remove-root", "configuration", "root configuration"),
    ("list-roots", "configuration", "root configuration"),
    ("subtree", "configuration", "root configuration"),
    ("install-claude-plugin", "external", "agent installation"),
    ("install-codex-mcp", "external", "agent installation"),
    ("detect-stacks", "not-java-ast", "project marker detection"),
    ("install-git-hooks", "external", "VCS integration"),
    ("query", "introspection", "raw database access"),
    ("db-path", "introspection", "database location"),
    ("schema", "introspection", "database schema"),
]


COMPARISON_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE coverage (
    feature TEXT PRIMARY KEY,
    comparison_kind TEXT NOT NULL,
    oracle TEXT NOT NULL
);
CREATE TABLE feature_summary (
    feature TEXT PRIMARY KEY,
    subjects INTEGER NOT NULL,
    matches INTEGER NOT NULL,
    mismatches INTEGER NOT NULL,
    errors INTEGER NOT NULL
);
CREATE TABLE cases (
    id TEXT PRIMARY KEY,
    feature TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    verdict TEXT NOT NULL,
    missing_count INTEGER NOT NULL,
    unexpected_count INTEGER NOT NULL,
    oracle_json TEXT NOT NULL,
    ast_index_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    UNIQUE(feature, subject_key)
);
CREATE INDEX cases_feature_idx ON cases(feature, verdict);
"""


def metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def locations(items: Iterable[dict[str, Any]], *, include_name: bool = True) -> set[tuple[Any, ...]]:
    result = set()
    for item in items:
        file = item.get("file", item.get("path"))
        line = item.get("line")
        if file is None or line is None:
            continue
        result.add((item.get("name"), file, line) if include_name else (file, line))
    return result


def normalized_kind(value: Any) -> str:
    kind = str(value or "unknown").lower()
    return {
        "method": "function",
        "constructor": "function",
        "field": "property",
        "enum_constant": "property",
        "annotation_type": "annotation",
    }.get(kind, kind)


def declarations(items: Iterable[dict[str, Any]]) -> set[tuple[Any, ...]]:
    return {
        (
            item.get("name"),
            normalized_kind(item.get("kind")),
            item.get("file", item.get("path")),
            item.get("line"),
            item.get("qualified_name") or item.get("qualifiedName"),
        )
        for item in items
    }


JAVA_IDENTIFIER = re.compile(r"(?<![\w$])([_$\w]+)", re.UNICODE)


def caller_patterns(name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(name)
    caller = re.compile(
        rf"[.>]{escaped}\s*\(|\b{escaped}\s*\(|->{escaped}\s*\(|&{escaped}\s*\("
        rf"|this\.{escaped}\s*\(|super\.{escaped}\s*\(|\.{escaped}(?:\s|$)"
        rf"|:{escaped}\b|\b{escaped}\."
        rf"|\bawait\s+{escaped}\s*\(|\bawait\s+[\w.]+\.{escaped}\s*\("
        rf"|\breturn\s+{escaped}\s*\(|\breturn\s+[\w.]+\.{escaped}\s*\("
    )
    definition = re.compile(
        rf"\b(?:fun|func|sub)\s+{escaped}\s*[<({{\[]"
        rf"|\bdef\s+(?:self\.)?{escaped}\b"
        rf"|\b(?:(?:public|private|protected|static|final|abstract|synchronized|override)\s+)*"
        rf"(?:void|int|long|boolean|char|byte|short|float|double|[\w.]+(?:<[^{{;]*>)?(?:\[\])*)"
        rf"\s+{escaped}\s*\("
    )
    return caller, definition


def collect_java_call_sites(
    project_root: Path,
    relative_paths: Iterable[str],
    method_names: set[str],
) -> tuple[dict[str, set[tuple[str, int]]], dict[str, set[tuple[str, int]]]]:
    """Reproduce ast-index callers' line regex once for all Java methods."""
    shaped: dict[str, set[tuple[str, int]]] = defaultdict(set)
    actual: dict[str, set[tuple[str, int]]] = defaultdict(set)
    cache: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {}
    for relative in sorted(relative_paths):
        if not relative.endswith(".java"):
            continue
        with (project_root / relative).open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                candidates = set(JAVA_IDENTIFIER.findall(line)) & method_names
                for name in candidates:
                    caller, definition = cache.setdefault(name, caller_patterns(name))
                    if not caller.search(line):
                        continue
                    location = (relative, line_number)
                    shaped[name].add(location)
                    if not definition.search(line):
                        actual[name].add(location)
    return shaped, actual


class Writer:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])

    def compare(
        self,
        feature: str,
        subject: str,
        oracle: Any,
        actual: Any,
        expected_keys: set[Any],
        actual_keys: set[Any],
    ) -> None:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        verdict = "match" if not missing and not unexpected else "mismatch"
        counters = self.counts[feature]
        counters[0] += 1
        counters[1 if verdict == "match" else 2] += 1
        if verdict == "match":
            return
        case_id = stable_id({"feature": feature, "subject": subject})
        diff = {"missing": missing, "unexpected": unexpected}
        self.connection.execute(
            """
            INSERT INTO cases(
                id, feature, subject_key, verdict, missing_count,
                unexpected_count, oracle_json, ast_index_json, diff_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id, feature, subject, verdict, len(missing), len(unexpected),
                canonical_json(oracle), canonical_json(actual), canonical_json(diff),
            ),
        )

    def error(self, feature: str, subject: str, oracle: Any, message: str) -> None:
        counters = self.counts[feature]
        counters[0] += 1
        counters[3] += 1
        case_id = stable_id({"feature": feature, "subject": subject})
        self.connection.execute(
            """
            INSERT INTO cases VALUES (?, ?, ?, 'error', 0, 0, ?, 'null', ?)
            """,
            (case_id, feature, subject, canonical_json(oracle), canonical_json({"error": message})),
        )

    def finish(self) -> None:
        for feature, (subjects, matches, mismatches, errors) in self.counts.items():
            self.connection.execute(
                "INSERT INTO feature_summary VALUES (?, ?, ?, ?, ?)",
                (feature, subjects, matches, mismatches, errors),
            )


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def compare_inventory(writer: Writer, mcp: sqlite3.Connection, ast: sqlite3.Connection) -> None:
    expected_files = {row[0] for row in mcp.execute("SELECT path FROM mcp_files")}
    actual_files = {row[0] for row in ast.execute("SELECT path FROM files WHERE path LIKE '%.java'")}
    writer.compare("file", "Java file inventory", sorted(expected_files), sorted(actual_files), expected_files, actual_files)

    oracle_symbols = rows_as_dicts(mcp.execute(
        "SELECT name, qualified_name, kind, file, line, column_number FROM mcp_symbols"
    ))
    ast_symbols = rows_as_dicts(ast.execute(
        """
        SELECT s.name, s.qualified_name, s.kind, f.path AS file, s.line, s.signature
          FROM symbols s JOIN files f ON f.id = s.file_id
         WHERE f.path LIKE '%.java'
        """
    ))
    oracle_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    actual_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    oracle_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    actual_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in oracle_symbols:
        oracle_by_name[item["name"]].append(item)
        oracle_by_file[item["file"]].append(item)
    for item in ast_symbols:
        actual_by_name[item["name"]].append(item)
        actual_by_file[item["file"]].append(item)

    for name in sorted(set(oracle_by_name) | set(actual_by_name)):
        expected, actual = oracle_by_name[name], actual_by_name[name]
        writer.compare("symbol", name, expected, actual, declarations(expected), declarations(actual))

        expected_types = [item for item in expected if str(item["kind"]).upper() in TYPE_KINDS]
        actual_types = [item for item in actual if str(item["kind"]).lower() in {"class", "interface", "enum"}]
        if expected_types or actual_types:
            writer.compare(
                "class", name, expected_types, actual_types,
                declarations(expected_types), declarations(actual_types),
            )

    for file in sorted(set(oracle_by_file) | set(actual_by_file)):
        expected, actual = oracle_by_file[file], actual_by_file[file]
        writer.compare("outline", file, expected, actual, declarations(expected), declarations(actual))


def load_semantic_items(connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT item_json FROM semantic_items WHERE task_id = ? ORDER BY page_number, item_number",
            (task_id,),
        )
    ]


def ast_symbols_named(ast: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    return rows_as_dicts(ast.execute(
        """
        SELECT s.name, s.kind, f.path AS file, s.line, s.signature
          FROM symbols s JOIN files f ON f.id = s.file_id
         WHERE s.name = ? AND f.path LIKE '%.java'
        """,
        (name,),
    ))


def compare_semantics(
    writer: Writer,
    mcp: sqlite3.Connection,
    ast: sqlite3.Connection,
    caller_shapes: dict[str, set[tuple[str, int]]],
    actual_callers: dict[str, set[tuple[str, int]]],
) -> None:
    tasks = mcp.execute(
        """
        SELECT t.*, s.name, s.file AS symbol_file, s.line AS symbol_line
          FROM tasks t JOIN mcp_symbols s ON s.id = t.subject_key
         WHERE t.phase = 'semantics'
         ORDER BY t.operation, t.subject_key
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for task in tasks:
        grouped[(str(task["operation"]), str(task["name"]))].append(task)

    for (operation, name), group in grouped.items():
        failures = [
            str(task["last_error"] or task["status"])
            for task in group
            if task["status"] != "complete"
        ]
        if failures:
            message = canonical_json(sorted(set(failures)))
            if operation == "references":
                writer.error("refs", name, [], message)
                writer.error("usages", name, [], message)
                writer.error("imports", name, [], message)
                if mcp.execute(
                    "SELECT EXISTS(SELECT 1 FROM mcp_symbols WHERE name = ? AND kind = 'METHOD')",
                    (name,),
                ).fetchone()[0]:
                    writer.error("callers", name, [], message)
            else:
                writer.error(operation, name, [], message)
            continue

        items = [
            item
            for task in group
            for item in load_semantic_items(mcp, str(task["id"]))
        ]
        if operation == "references":
            java_items = [
                item for item in items
                if str(item.get("file", "")).endswith(".java")
            ]
            expected_imports = [
                item for item in java_items
                if str(item.get("type", "")).upper() == "IMPORT"
            ]
            expected = [
                item for item in java_items
                if str(item.get("type", "")).upper() != "IMPORT"
            ]
            actual = rows_as_dicts(ast.execute(
                """
                SELECT r.name, f.path AS file, r.line, r.context
                  FROM refs r JOIN files f ON f.id = r.file_id
                 WHERE r.name = ? AND f.path LIKE '%.java'
                """,
                (name,),
            ))
            expected_keys = locations(expected, include_name=False)
            actual_keys = locations(actual, include_name=False)
            writer.compare("usages", name, expected, actual, expected_keys, actual_keys)
            actual_imports = rows_as_dicts(ast.execute(
                """
                SELECT s.name, f.path AS file, s.line, s.qualified_name, s.signature
                  FROM symbols s JOIN files f ON f.id = s.file_id
                 WHERE s.name = ? AND s.kind = 'import' AND f.path LIKE '%.java'
                """,
                (name,),
            ))
            expected_import_keys = locations(expected_imports, include_name=False)
            actual_import_keys = locations(actual_imports, include_name=False)
            writer.compare(
                "imports", name, expected_imports, actual_imports,
                expected_import_keys, actual_import_keys,
            )
            is_method = mcp.execute(
                "SELECT EXISTS(SELECT 1 FROM mcp_symbols WHERE name = ? AND kind = 'METHOD')",
                (name,),
            ).fetchone()[0]
            if is_method:
                writer.compare(
                    "callers", name, expected, sorted(actual_callers.get(name, set())),
                    expected_keys & caller_shapes.get(name, set()),
                    actual_callers.get(name, set()),
                )

            expected_definitions = rows_as_dicts(mcp.execute(
                """
                SELECT name, kind, file, line, qualified_name
                  FROM mcp_symbols WHERE name = ?
                """,
                (name,),
            ))
            actual_definitions = ast_symbols_named(ast, name)
            expected_definition_keys = locations(expected_definitions, include_name=False)
            actual_definition_keys = locations(actual_definitions, include_name=False)
            writer.compare(
                "refs",
                name,
                {"definitions": expected_definitions, "usages": expected},
                {"definitions": actual_definitions, "usages": actual},
                {("definition", *value) for value in expected_definition_keys}
                | {("import", *value) for value in expected_import_keys}
                | {("usage", *value) for value in expected_keys},
                {("definition", *value) for value in actual_definition_keys}
                | {("import", *value) for value in actual_import_keys}
                | {("usage", *value) for value in actual_keys},
            )
        elif operation == "implementations":
            items = [item for item in items if str(item.get("file", "")).endswith(".java")]
            actual = rows_as_dicts(ast.execute(
                """
                SELECT s.name, s.kind, f.path AS file, s.line
                  FROM inheritance i
                  JOIN symbols s ON s.id = i.child_id
                  JOIN files f ON f.id = s.file_id
                 WHERE i.parent_name = ? AND f.path LIKE '%.java'
                """,
                (name,),
            ))
            writer.compare(
                "implementations", name, items, actual,
                locations(items, include_name=False), locations(actual, include_name=False),
            )
        elif operation == "type_hierarchy":
            oracles = items
            expected_parents = {
                item.get("name")
                for oracle in oracles
                for item in oracle.get("supertypes", [])
                if item.get("name")
            }
            expected_children = locations(
                [item for oracle in oracles for item in oracle.get("subtypes", [])],
                include_name=False,
            )
            target_ids = [row[0] for row in ast.execute(
                """
                SELECT s.id FROM symbols s JOIN files f ON f.id = s.file_id
                 WHERE s.name = ? AND f.path LIKE '%.java'
                """,
                (name,),
            )]
            actual_parents: set[str] = set()
            for target_id in target_ids:
                actual_parents.update(
                    row[0] for row in ast.execute(
                        "SELECT parent_name FROM inheritance WHERE child_id = ?", (target_id,)
                    )
                )
            actual_children_rows = rows_as_dicts(ast.execute(
                """
                SELECT f.path AS file, s.line, s.name
                  FROM inheritance i JOIN symbols s ON s.id = i.child_id
                  JOIN files f ON f.id = s.file_id
                 WHERE i.parent_name = ? AND f.path LIKE '%.java'
                """,
                (name,),
            ))
            actual_children = locations(actual_children_rows, include_name=False)
            writer.compare(
                "hierarchy",
                name,
                {"parents": sorted(expected_parents), "children": sorted(expected_children)},
                {"parents": sorted(actual_parents), "children": sorted(actual_children)},
                {("parent", value) for value in expected_parents}
                | {("child", *value) for value in expected_children},
                {("parent", value) for value in actual_parents}
                | {("child", *value) for value in actual_children},
            )


def require_complete_collection(mcp: sqlite3.Connection) -> None:
    for phase in ("inventory", "semantics"):
        total, incomplete = mcp.execute(
            """
            SELECT count(*), sum(CASE WHEN status != 'complete' THEN 1 ELSE 0 END)
              FROM tasks WHERE phase = ?
            """,
            (phase,),
        ).fetchone()
        if not total:
            raise ToolError(f"{phase} was not collected")
        if incomplete:
            raise ToolError(f"{phase} has {incomplete} incomplete tasks")


def build_comparison(
    mcp_path: Path,
    ast_path: Path,
    output_path: Path,
    *,
    require_complete: bool = True,
    verify_current_snapshot: bool = True,
) -> dict[str, Any]:
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()
    mcp = connect(mcp_path, read_only=True)
    ast = connect(ast_path, read_only=True)
    output = connect(temporary)
    try:
        output.executescript(COMPARISON_SCHEMA)
        mcp_snapshot = metadata(mcp, "source_snapshot_sha256")
        ast_snapshot = metadata(ast, "java_comparator_snapshot_sha256")
        if not mcp_snapshot or mcp_snapshot != ast_snapshot:
            raise ToolError("ast-index.sqlite and mcp-index.sqlite describe different source snapshots")
        if verify_current_snapshot:
            project_root = metadata(mcp, "project_root")
            if not project_root:
                raise ToolError("mcp-index.sqlite has no project_root metadata")
            current_snapshot, _ = source_snapshot(Path(project_root))
            if current_snapshot != mcp_snapshot:
                raise ToolError("Java sources changed during collection; comparison was not created")
        if require_complete:
            require_complete_collection(mcp)
        with output:
            output.executemany("INSERT INTO coverage VALUES (?, ?, ?)", COVERAGE)
            output.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("source_snapshot_sha256", mcp_snapshot),
                    ("created_at_ms", str(now_ms())),
                    ("ast_index_database", str(ast_path)),
                    ("mcp_index_database", str(mcp_path)),
                ],
            )
            writer = Writer(output)
            compare_inventory(writer, mcp, ast)
            method_names = {
                str(row[0])
                for row in mcp.execute(
                    "SELECT DISTINCT name FROM mcp_symbols WHERE kind = 'METHOD'"
                )
            }
            source_paths = [
                str(row[0]) for row in mcp.execute("SELECT path FROM source_files")
            ]
            project_root = Path(metadata(mcp, "project_root") or "")
            caller_shapes, actual_callers = collect_java_call_sites(
                project_root, source_paths, method_names
            )
            compare_semantics(writer, mcp, ast, caller_shapes, actual_callers)
            writer.finish()
        summary_rows = rows_as_dicts(output.execute("SELECT * FROM feature_summary ORDER BY feature"))
        mismatch_cases = output.execute(
            "SELECT count(*) FROM cases WHERE verdict = 'mismatch'"
        ).fetchone()[0]
        error_cases = output.execute(
            "SELECT count(*) FROM cases WHERE verdict = 'error'"
        ).fetchone()[0]
        summary = {
            "mismatch_cases": mismatch_cases,
            "error_cases": error_cases,
            "features": summary_rows,
        }
        output.execute(
            "INSERT INTO metadata VALUES ('summary_json', ?)", (canonical_json(summary),)
        )
        output.commit()
        output.close()
        output = None
        os.replace(temporary, output_path)
        return summary
    finally:
        mcp.close()
        ast.close()
        if output is not None:
            output.close()
        if temporary.exists():
            temporary.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ast-db", required=True)
    parser.add_argument("--mcp-db", required=True)
    parser.add_argument("--output-db", required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        arguments = parse_args()
        ast_path = Path(arguments.ast_db).expanduser().resolve()
        mcp_path = Path(arguments.mcp_db).expanduser().resolve()
        output_path = Path(arguments.output_db).expanduser().resolve()
        if not ast_path.is_file() or not mcp_path.is_file():
            raise ToolError("both input databases must exist")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        summary = build_comparison(
            mcp_path,
            ast_path,
            output_path,
            require_complete=not arguments.allow_incomplete,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ToolError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
