#!/usr/bin/env python3
"""Collect ast-index and IntelliJ MCP snapshots into two SQLite databases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Iterable

from common import (
    SCHEMA_VERSION,
    StreamableHttpMcpClient,
    ToolError,
    canonical_json,
    connect,
    discover_mcp_url,
    java_identifier_candidates,
    now_ms,
    source_snapshot,
    stable_id,
)


REQUIRED_TOOLS = {
    "ide_find_file",
    "ide_find_symbol",
    "ide_find_references",
    "ide_find_implementations",
    "ide_type_hierarchy",
}
TYPE_KINDS = {"CLASS", "INTERFACE", "ENUM", "RECORD", "ANNOTATION"}
INVENTORY_PAGE_SIZE = 10
EXACT_PAGE_SIZE = 500


MCP_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_names (
    candidate_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY(candidate_kind, name)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    operation TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    UNIQUE(operation, subject_key)
);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(phase, status, operation);
CREATE TABLE IF NOT EXISTS query_pages (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    PRIMARY KEY(task_id, page_number)
);
CREATE TABLE IF NOT EXISTS mcp_files (
    path TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    directory TEXT,
    discovered_by_task TEXT NOT NULL REFERENCES tasks(id),
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_files_task_idx ON mcp_files(discovered_by_task);
CREATE TABLE IF NOT EXISTS mcp_symbols (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    qualified_name TEXT,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    column_number INTEGER,
    container_name TEXT,
    language TEXT,
    discovered_by_task TEXT NOT NULL REFERENCES tasks(id),
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_symbols_name_idx ON mcp_symbols(name);
CREATE INDEX IF NOT EXISTS mcp_symbols_file_idx ON mcp_symbols(file, line);
CREATE INDEX IF NOT EXISTS mcp_symbols_task_idx ON mcp_symbols(discovered_by_task);
CREATE TABLE IF NOT EXISTS semantic_items (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    item_number INTEGER NOT NULL,
    item_json TEXT NOT NULL,
    PRIMARY KEY(task_id, page_number, item_number)
);
"""


def metadata_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def metadata_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (key, value if isinstance(value, str) else canonical_json(value)),
    )


def ensure_output_is_safe(project_root: Path, output_dir: Path) -> None:
    try:
        output_dir.relative_to(project_root)
    except ValueError:
        return
    raise ToolError("output directory must be outside the scanned project")


def initialize_database(
    database: Path, project_root: Path, snapshot_sha256: str, files: list[dict[str, Any]]
) -> sqlite3.Connection:
    connection = connect(database)
    connection.executescript(MCP_SCHEMA)
    previous = metadata_get(connection, "source_snapshot_sha256")
    if previous is not None and previous != snapshot_sha256:
        raise ToolError("Java sources changed since mcp-index.sqlite was created; use a new output directory")
    with connection:
        metadata_set(connection, "schema_version", str(SCHEMA_VERSION))
        metadata_set(connection, "project_root", str(project_root))
        metadata_set(connection, "source_snapshot_sha256", snapshot_sha256)
        metadata_set(connection, "source_file_count", str(len(files)))
        connection.executemany(
            "INSERT OR REPLACE INTO source_files(path, size, sha256) VALUES (:path, :size, :sha256)",
            files,
        )
        connection.execute("UPDATE tasks SET status = 'pending' WHERE status = 'running'")
        # Older collector builds scheduled ide_call_hierarchy as a callers
        # oracle.  Its results are caller-method nodes, while ast-index callers
        # emits call-site locations, so those tasks are not comparable.
        connection.execute(
            "DELETE FROM tasks WHERE phase = 'semantics' AND operation = 'callers'"
        )
    return connection


def resolve_binary(value: str) -> str:
    if os.sep in value:
        path = Path(value).resolve()
        if not path.is_file():
            raise ToolError(f"binary does not exist: {path}")
        return str(path)
    resolved = shutil.which(value)
    if not resolved:
        raise ToolError(f"binary is not on PATH: {value}")
    return resolved


def build_ast_index(
    ast_binary: str,
    project_root: Path,
    database: Path,
    snapshot_sha256: str,
    threads: int,
    rebuild: bool,
) -> None:
    if database.exists() and not rebuild:
        connection = connect(database)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'java_comparator_snapshot_sha256'"
            ).fetchone()
            if row and row[0] == snapshot_sha256:
                return
        except sqlite3.Error:
            pass
        finally:
            connection.close()
    environment = os.environ.copy()
    environment["AST_INDEX_DB_PATH"] = str(database)
    command = [
        ast_binary,
        "rebuild",
        "--force",
        "--max-files",
        "0",
        "--threads",
        str(threads),
    ]
    print("running:", " ".join(command), file=sys.stderr)
    completed = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolError(
            "ast-index rebuild failed:\n" + completed.stdout + "\n" + completed.stderr
        )
    connection = connect(database)
    try:
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                ("java_comparator_snapshot_sha256", snapshot_sha256),
            )
    finally:
        connection.close()


def add_task(
    connection: sqlite3.Connection,
    phase: str,
    operation: str,
    subject_key: str,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    task_id = stable_id({"operation": operation, "subject": subject_key})
    connection.execute(
        """
        INSERT INTO tasks(
            id, phase, operation, subject_key, tool, arguments_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operation, subject_key) DO UPDATE SET
            tool = excluded.tool,
            arguments_json = excluded.arguments_json
        WHERE tasks.status IN ('pending', 'error', 'running')
        """,
        (task_id, phase, operation, subject_key, tool, canonical_json(arguments), now_ms()),
    )


def partition_names(names: set[str], bucket_size: int) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(prefix: str, values: set[str]) -> None:
        if len(values) <= bucket_size:
            result.append(("prefix", prefix))
            return
        if prefix in values:
            result.append(("exact", prefix))
        longer = {value for value in values if len(value) > len(prefix)}
        for character in sorted({value[len(prefix)] for value in longer}):
            child = prefix + character
            visit(child, {value for value in longer if value.startswith(child)})

    visit("", names)
    return result


def schedule_inventory(
    connection: sqlite3.Connection,
    project_root: Path,
    files: list[dict[str, Any]],
    bucket_size: int,
) -> None:
    base = {
        "scope": "project_files",
        "pageSize": INVENTORY_PAGE_SIZE,
        "project_path": str(project_root),
    }
    file_names = {Path(item["path"]).name.removesuffix(".java") for item in files}
    identifiers = java_identifier_candidates(project_root)
    file_queries = partition_names(file_names, min(bucket_size, 500))
    symbol_queries = partition_names(identifiers, bucket_size)
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO candidate_names VALUES ('file', ?)",
            ((name,) for name in sorted(file_names)),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO candidate_names VALUES ('symbol', ?)",
            ((name,) for name in sorted(identifiers)),
        )
        for mode, value in file_queries:
            operation = f"file_{mode}"
            query = f"{value}.java" if mode == "exact" else f"{value}*.java"
            add_task(
                connection,
                "inventory",
                operation,
                value,
                "ide_find_file",
                {
                    **base,
                    "pageSize": EXACT_PAGE_SIZE,
                    "query": query,
                    "includeGenerated": False,
                },
            )
        for mode, value in symbol_queries:
            operation = f"symbol_{mode}"
            query = value if mode == "exact" else f"{value}*"
            add_task(
                connection,
                "inventory",
                operation,
                value,
                "ide_find_symbol",
                {
                    **base,
                    "pageSize": EXACT_PAGE_SIZE if mode == "exact" else INVENTORY_PAGE_SIZE,
                    "query": query,
                    "language": "Java",
                    "includeGenerated": False,
                },
            )
    print(
        f"scheduled inventory: {len(file_queries)} file buckets, {len(symbol_queries)} symbol buckets",
        file=sys.stderr,
    )


def schedule_inventory_reconciliation(
    connection: sqlite3.Connection,
    project_root: Path,
    ast_database: Path,
) -> None:
    base = {
        "scope": "project_files",
        "pageSize": INVENTORY_PAGE_SIZE,
        "project_path": str(project_root),
    }
    file_prefixes_pending = connection.execute(
        "SELECT count(*) FROM tasks WHERE operation = 'file_prefix' AND status != 'complete'"
    ).fetchone()[0]
    if not file_prefixes_pending:
        missing_file_names = {
            Path(row[0]).name
            for row in connection.execute(
                """
                SELECT path FROM source_files
                EXCEPT
                SELECT path FROM mcp_files
                """
            )
        }
        with connection:
            for filename in sorted(missing_file_names):
                stem = filename.removesuffix(".java")
                add_task(
                    connection,
                    "inventory",
                    "file_reconcile_exact",
                    stem,
                    "ide_find_file",
                    {
                        **base,
                        "pageSize": EXACT_PAGE_SIZE,
                        "query": filename,
                        "includeGenerated": False,
                    },
                )

    symbol_prefixes_pending = connection.execute(
        "SELECT count(*) FROM tasks WHERE operation = 'symbol_prefix' AND status != 'complete'"
    ).fetchone()[0]
    if not symbol_prefixes_pending:
        ast = connect(ast_database, read_only=True)
        try:
            ast_names = {
                str(row[0])
                for row in ast.execute(
                    """
                    SELECT DISTINCT s.name
                      FROM symbols s JOIN files f ON f.id = s.file_id
                     WHERE f.path LIKE '%.java'
                    """
                )
            }
        finally:
            ast.close()
        oracle_names = {str(row[0]) for row in connection.execute("SELECT DISTINCT name FROM mcp_symbols")}
        with connection:
            for name in sorted(ast_names - oracle_names):
                add_task(
                    connection,
                    "inventory",
                    "symbol_reconcile_exact",
                    name,
                    "ide_find_symbol",
                    {
                        **base,
                        "pageSize": INVENTORY_PAGE_SIZE,
                        "query": name,
                        "language": "Java",
                        "includeGenerated": False,
                    },
                )


def symbol_identity(item: dict[str, Any]) -> str:
    return stable_id(
        {
            "name": item.get("name"),
            "qualifiedName": item.get("qualifiedName"),
            "kind": item.get("kind"),
            "file": item.get("file"),
            "line": item.get("line"),
            "column": item.get("column"),
        }
    )


def response_items(operation: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    if operation.startswith("file_"):
        return list(response.get("files", []))
    if operation.startswith("symbol_"):
        return list(response.get("symbols", []))
    if operation == "definition":
        return [] if "file" not in response else [response]
    if operation == "references":
        return list(response.get("usages", []))
    if operation == "implementations":
        return list(response.get("implementations", []))
    if operation == "type_hierarchy":
        return [response]
    if operation == "super_methods":
        return list(response.get("superMethods", response.get("methods", [])))
    if operation in {"callers", "callees"}:
        return [response]
    return []


def store_page(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    page_number: int,
    request_arguments: dict[str, Any],
    response: dict[str, Any],
) -> None:
    items = response_items(str(task["operation"]), response)
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO query_pages(
                task_id, page_number, request_json, response_json, item_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                page_number,
                canonical_json(request_arguments),
                canonical_json(response),
                len(items),
            ),
        )
        operation = str(task["operation"])
        if operation.startswith("file_"):
            prefix = str(task["subject_key"])
            for item in items:
                name = str(item.get("name", ""))
                matches = (
                    name == f"{prefix}.java"
                    if operation.endswith("_exact")
                    else name.endswith(".java") and name.removesuffix(".java").startswith(prefix)
                )
                if not matches:
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO mcp_files(
                        path, name, directory, discovered_by_task, raw_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["path"], item["name"], item.get("directory"),
                        task["id"], canonical_json(item),
                    ),
                )
        elif operation.startswith("symbol_"):
            prefix = str(task["subject_key"])
            for item in items:
                name = str(item.get("name", ""))
                matches = name == prefix if operation.endswith("_exact") else name.startswith(prefix)
                if not matches or str(item.get("language", "")).lower() != "java":
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO mcp_symbols(
                        id, name, qualified_name, kind, file, line, column_number,
                        container_name, language, discovered_by_task, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol_identity(item), item["name"], item.get("qualifiedName"),
                        item.get("kind", "UNKNOWN"), item["file"], item["line"],
                        item.get("column"), item.get("containerName"), item.get("language"),
                        task["id"], canonical_json(item),
                    ),
                )
        else:
            for index, item in enumerate(items):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO semantic_items(
                        task_id, page_number, item_number, item_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task["id"], page_number, index, canonical_json(item)),
                )


def clear_task_results(connection: sqlite3.Connection, task: sqlite3.Row) -> None:
    with connection:
        connection.execute("DELETE FROM query_pages WHERE task_id = ?", (task["id"],))
        connection.execute("DELETE FROM semantic_items WHERE task_id = ?", (task["id"],))
        if str(task["operation"]).startswith("file_"):
            connection.execute("DELETE FROM mcp_files WHERE discovered_by_task = ?", (task["id"],))
        elif str(task["operation"]).startswith("symbol_"):
            connection.execute("DELETE FROM mcp_symbols WHERE discovered_by_task = ?", (task["id"],))
        connection.execute(
            "UPDATE tasks SET status = 'running', attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (task["id"],),
        )


def split_overflowing_prefix(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    initial_arguments: dict[str, Any],
) -> bool:
    operation = str(task["operation"])
    if operation != "symbol_prefix":
        return False
    candidate_kind = "symbol"
    prefix = str(task["subject_key"])
    candidates = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM candidate_names WHERE candidate_kind = ?",
            (candidate_kind,),
        )
        if str(row[0]).startswith(prefix)
    }
    longer = {name for name in candidates if len(name) > len(prefix)}
    children = sorted({prefix + name[len(prefix)] for name in longer})
    if not children and prefix not in candidates:
        return False

    with connection:
        if prefix in candidates:
            exact_operation = f"{candidate_kind}_split_exact"
            query = f"{prefix}.java" if candidate_kind == "file" else prefix
            add_task(
                connection,
                "inventory",
                exact_operation,
                prefix,
                "ide_find_file" if candidate_kind == "file" else "ide_find_symbol",
                {
                    **initial_arguments,
                    "pageSize": EXACT_PAGE_SIZE,
                    "query": query,
                },
            )
        for child in children:
            query = f"{child}*.java" if candidate_kind == "file" else f"{child}*"
            add_task(
                connection,
                "inventory",
                operation,
                child,
                "ide_find_file" if candidate_kind == "file" else "ide_find_symbol",
                {
                    **initial_arguments,
                    "pageSize": INVENTORY_PAGE_SIZE,
                    "query": query,
                },
            )
    return True


def run_task(client: StreamableHttpMcpClient, connection: sqlite3.Connection, task: sqlite3.Row) -> None:
    clear_task_results(connection, task)
    initial_arguments = json.loads(task["arguments_json"])
    arguments = initial_arguments
    page_number = 0
    strict_items = 0
    saw_fuzzy_item = False
    prefix_overflow = False
    try:
        while True:
            response = client.call(str(task["tool"]), arguments)
            store_page(connection, task, page_number, arguments, response)
            cursor = response.get("nextCursor") if isinstance(response, dict) else None
            operation = str(task["operation"])
            if operation.startswith("file_"):
                values = list(response.get("files", []))
                prefix = str(task["subject_key"])
                matching = [
                    item for item in values
                    if (
                        item.get("name") == f"{prefix}.java"
                        if operation.endswith("_exact")
                        else str(item.get("name", "")).endswith(".java")
                        and str(item.get("name", "")).removesuffix(".java").startswith(prefix)
                    )
                ]
                if len(matching) < len(values):
                    saw_fuzzy_item = True
                    cursor = None
                strict_items += len(matching)
            elif operation.startswith("symbol_"):
                values = list(response.get("symbols", []))
                prefix = str(task["subject_key"])
                matching = [
                    item for item in values
                    if (
                        item.get("name") == prefix
                        if operation.endswith("_exact")
                        else str(item.get("name", "")).startswith(prefix)
                    )
                ]
                if len(matching) < len(values):
                    saw_fuzzy_item = True
                    cursor = None
                elif int(response.get("totalCount", 0)) >= 500:
                    prefix_overflow = True
                    cursor = None
                strict_items += len(matching)
            if not cursor:
                break
            arguments = {
                "cursor": cursor,
                "pageSize": initial_arguments.get("pageSize", 500),
                "project_path": initial_arguments["project_path"],
            }
            page_number += 1
        if prefix_overflow or (strict_items >= 500 and not saw_fuzzy_item):
            split_overflowing_prefix(connection, task, initial_arguments)
        with connection:
            connection.execute(
                "UPDATE tasks SET status = 'complete', completed_at_ms = ? WHERE id = ?",
                (now_ms(), task["id"]),
            )
    except Exception as error:
        with connection:
            connection.execute(
                "UPDATE tasks SET status = 'error', last_error = ? WHERE id = ?",
                (str(error), task["id"]),
            )
        raise


def run_pending(
    client: StreamableHttpMcpClient,
    connection: sqlite3.Connection,
    phase: str,
    max_tasks: int | None,
    operation_pattern: str | None = None,
    progress_every: int = 1000,
) -> int:
    completed = 0
    failed = 0
    retry_rounds = 0
    while max_tasks is None or completed < max_tasks:
        task = connection.execute(
            """
            SELECT * FROM tasks
             WHERE phase = ? AND status IN ('pending', 'error') AND attempts < 3
               AND (? IS NULL OR operation LIKE ?)
             ORDER BY operation, subject_key
             LIMIT 1
            """,
            (phase, operation_pattern, operation_pattern),
        ).fetchone()
        if task is None:
            exhausted_errors = connection.execute(
                """
                SELECT count(*) FROM tasks
                 WHERE phase = ? AND status = 'error' AND attempts >= 3
                   AND (? IS NULL OR operation LIKE ?)
                """,
                (phase, operation_pattern, operation_pattern),
            ).fetchone()[0]
            if exhausted_errors and max_tasks is None and retry_rounds < 2:
                with connection:
                    connection.execute(
                        """
                        UPDATE tasks SET attempts = 0
                         WHERE phase = ? AND status = 'error' AND attempts >= 3
                           AND (? IS NULL OR operation LIKE ?)
                        """,
                        (phase, operation_pattern, operation_pattern),
                    )
                retry_rounds += 1
                continue
            break
        try:
            run_task(client, connection, task)
        except Exception:
            failed += 1
        completed += 1
        if progress_every and completed % progress_every == 0:
            remaining = connection.execute(
                "SELECT count(*) FROM tasks WHERE phase = ? AND status != 'complete'",
                (phase,),
            ).fetchone()[0]
            print(
                f"{phase}: processed {completed} this run, {remaining} remaining, "
                f"{failed} failed attempts",
                file=sys.stderr,
            )
    return completed


def schedule_semantics(connection: sqlite3.Connection, project_root: Path) -> None:
    pending = connection.execute(
        "SELECT count(*) FROM tasks WHERE phase = 'inventory' AND status != 'complete'"
    ).fetchone()[0]
    if pending:
        raise ToolError(f"inventory is incomplete: {pending} tasks remain")
    symbols = connection.execute("SELECT * FROM mcp_symbols ORDER BY file, line, column_number").fetchall()
    with connection:
        for symbol in symbols:
            base = {
                "file": symbol["file"],
                "line": symbol["line"],
                "column": symbol["column_number"] or 1,
                "project_path": str(project_root),
            }
            add_task(
                connection,
                "semantics",
                "references",
                symbol["id"],
                "ide_find_references",
                {**base, "scope": "project_files", "includeGenerated": False, "pageSize": 500},
            )
            kind = str(symbol["kind"]).upper()
            if kind in TYPE_KINDS:
                add_task(
                    connection,
                    "semantics",
                    "implementations",
                    symbol["id"],
                    "ide_find_implementations",
                    {**base, "scope": "project_files", "includeGenerated": False, "pageSize": 500},
                )
            if kind in TYPE_KINDS:
                add_task(
                    connection,
                    "semantics",
                    "type_hierarchy",
                    symbol["id"],
                    "ide_type_hierarchy",
                    {**base, "scope": "project_files", "includeGenerated": False},
                )
    print(f"scheduled semantic tasks for {len(symbols)} symbols", file=sys.stderr)


def task_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT phase, status, count(*) AS count FROM tasks GROUP BY phase, status"
    ).fetchall()
    return {
        "tasks": [dict(row) for row in rows],
        "mcp_files": connection.execute("SELECT count(*) FROM mcp_files").fetchone()[0],
        "mcp_symbols": connection.execute("SELECT count(*) FROM mcp_symbols").fetchone()[0],
    }


def require_completed_phase(connection: sqlite3.Connection, phase: str) -> None:
    remaining = connection.execute(
        "SELECT count(*) FROM tasks WHERE phase = ? AND status != 'complete'",
        (phase,),
    ).fetchone()[0]
    if remaining:
        raise ToolError(f"{phase} finished with {remaining} incomplete tasks")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ast-index", default="ast-index")
    parser.add_argument("--mcp-name", default="intellij-index")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--phase", choices=("snapshot", "inventory", "semantics", "all"), default="all")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--inventory-bucket-size", type=int, default=5000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--rebuild-ast", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        arguments = parse_args()
        project_root = Path(arguments.project_root).expanduser().resolve()
        output_dir = Path(arguments.output_dir).expanduser().resolve()
        if not project_root.is_dir():
            raise ToolError(f"project root does not exist: {project_root}")
        ensure_output_is_safe(project_root, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ast_database = output_dir / "ast-index.sqlite"
        mcp_database = output_dir / "mcp-index.sqlite"

        snapshot_sha256, files = source_snapshot(project_root)
        connection = initialize_database(
            mcp_database, project_root, snapshot_sha256, files
        )
        try:
            ast_binary = resolve_binary(arguments.ast_index)
            build_ast_index(
                ast_binary,
                project_root,
                ast_database,
                snapshot_sha256,
                arguments.threads,
                arguments.rebuild_ast,
            )
            if arguments.phase == "snapshot":
                print(canonical_json(task_summary(connection)))
                return 0

            url = discover_mcp_url(arguments.mcp_name, arguments.codex)
            client = StreamableHttpMcpClient(url, arguments.timeout)
            server = client.initialize()
            tools = client.tools()
            missing = sorted(REQUIRED_TOOLS - set(tools))
            if missing:
                raise ToolError(f"MCP server is missing tools: {missing}")
            with connection:
                metadata_set(connection, "mcp_name", arguments.mcp_name)
                metadata_set(connection, "mcp_url", url)
                metadata_set(connection, "mcp_server", server)
                metadata_set(connection, "mcp_tools", sorted(tools))

            if arguments.phase in {"inventory", "all"}:
                schedule_inventory(
                    connection, project_root, files, arguments.inventory_bucket_size
                )
                if arguments.max_tasks is None:
                    run_pending(
                        client, connection, "inventory", None, "file_%",
                        arguments.progress_every,
                    )
                    schedule_inventory_reconciliation(
                        connection, project_root, ast_database
                    )
                    run_pending(
                        client, connection, "inventory", None, "file_%",
                        arguments.progress_every,
                    )
                    run_pending(
                        client, connection, "inventory", None, "symbol_%",
                        arguments.progress_every,
                    )
                    schedule_inventory_reconciliation(
                        connection, project_root, ast_database
                    )
                    run_pending(
                        client, connection, "inventory", None, "symbol_%",
                        arguments.progress_every,
                    )
                    require_completed_phase(connection, "inventory")
                else:
                    schedule_inventory_reconciliation(
                        connection, project_root, ast_database
                    )
                    run_pending(
                        client, connection, "inventory", arguments.max_tasks,
                        progress_every=arguments.progress_every,
                    )
            if arguments.phase in {"semantics", "all"}:
                schedule_semantics(connection, project_root)
                run_pending(
                    client, connection, "semantics", arguments.max_tasks,
                    progress_every=arguments.progress_every,
                )
                if arguments.max_tasks is None:
                    require_completed_phase(connection, "semantics")
            print(json.dumps(task_summary(connection), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        finally:
            connection.close()
    except (ToolError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
