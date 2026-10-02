#!/usr/bin/env python3
"""Resumable, live differential checks against Index MCP Server.

One fixture executes the actual ast-index CLI for each planned check. Every
round has an isolated rebuilt index and a durable evidence database. Reference
results are consumed only after complete pagination; unknown contracts prevent
the round from being declared successful.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any

from common import (
    StreamableHttpMcpClient, ToolError, canonical_json, connect,
    discover_mcp_url, java_identifier_candidates, now_ms, source_snapshot, stable_id,
)
from build_index import build_ast_index


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coverage(
    feature TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks(
    id TEXT PRIMARY KEY, feature TEXT NOT NULL, subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', verdict TEXT,
    expected_json TEXT, actual_json TEXT, diff_json TEXT,
    error TEXT, completed_at INTEGER,
    UNIQUE(feature,subject)
);
CREATE INDEX IF NOT EXISTS checks_status ON checks(status, feature, subject);
CREATE TABLE IF NOT EXISTS pages(
    check_id TEXT NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    page INTEGER NOT NULL, request_json TEXT NOT NULL, response_json TEXT NOT NULL,
    tool TEXT NOT NULL,
    PRIMARY KEY(check_id,page)
);
"""


class Unsupported(ToolError):
    pass


def run_command(command: list[str], root: Path, environment: dict[str, str], timeout: int = 120) -> str:
    result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Project data may be present in stdout; keep it in evidence, not logs.
        raise ToolError(f"command failed ({result.returncode}): {command[1:3]}")
    return result.stdout


def relative_path(value: Any, root: Path) -> str:
    if not isinstance(value, str) or not value:
        raise Unsupported("missing file path")
    path = Path(value)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError as error:
            raise Unsupported("result is outside the target root") from error
    if ".." in path.parts:
        raise Unsupported("result path traverses outside target")
    return path.as_posix()


def type_keys(items: list[dict[str, Any]], root: Path, name: str, *, qualified: bool = False) -> set[tuple[Any, ...]]:
    result = set()
    for item in items:
        if item.get("name") != name:
            continue
        file = relative_path(item.get("file", item.get("path")), root)
        if not file.endswith(".java"):
            continue
        line = item.get("line")
        if not isinstance(line, int) or line < 1:
            raise Unsupported("invalid declaration line")
        # IntelliJ reports records, enums and interfaces as CLASS in this
        # endpoint. Class navigation checks identity, not enum spelling.
        key = (name, file, line)
        if qualified:
            key += (item.get("qualifiedName", item.get("qualified_name")) or "",)
        result.add(key)
    return result


def declaration_keys(items: list[dict[str, Any]], root: Path, name: str) -> set[tuple[Any, ...]]:
    kinds = {
        "class": "type", "interface": "type", "enum": "type", "record": "type",
        "method": "method", "function": "method", "constructor": "method",
        "field": "field", "property": "field", "enum_constant": "field",
        "constant": "field", "symbol": "declaration",
    }
    result = set()
    for item in items:
        if item.get("name") != name:
            continue
        file = relative_path(item.get("file", item.get("path")), root)
        if not file.endswith(".java"):
            continue
        kind = str(item.get("kind", "")).lower()
        if kind not in kinds:
            raise Unsupported(f"unknown Java declaration kind: {kind}")
        line = item.get("line")
        if not isinstance(line, int) or line < 1:
            raise Unsupported("invalid declaration line")
        qualified = item.get("qualifiedName", item.get("qualified_name")) or ""
        # Go to Symbol reports enum constants as CLASS and ordinary fields as
        # SYMBOL. Its kind is not an authoritative Java declaration category.
        # Class navigation has its own contract; here compare declaration identity.
        result.add((name, "declaration", file, line, qualified))
    return result


class Fixture:
    def __init__(self, root: Path, binary: Path, database: Path, state: sqlite3.Connection, client: Any):
        self.root, self.binary, self.state, self.client = root, binary, state, client
        self.environment = {
            **os.environ, "AST_INDEX_DB_PATH": str(database),
            "AST_INDEX_CACHE_DIR": str(database.parent / "cache"), "NO_COLOR": "1",
        }

    def cli(self, *arguments: str) -> Any:
        value = run_command([str(self.binary), "--format", "json", *arguments], self.root, self.environment)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise Unsupported("command does not provide the expected JSON contract") from error
        return parsed

    def paginated(self, check_id: str, tool: str, arguments: dict[str, Any], field: str) -> list[dict[str, Any]]:
        items = []
        cursors = set()
        page = 0
        while True:
            response = self.client.call(tool, arguments)
            with self.state:
                self.state.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
                    (check_id, page, canonical_json(arguments), canonical_json(response), tool),
                )
            if not isinstance(response, dict) or not isinstance(response.get(field), list):
                raise Unsupported(f"MCP {tool} did not return {field}")
            if response.get("stale") or response.get("truncated"):
                raise Unsupported("MCP pagination snapshot is stale or truncated")
            if any(not isinstance(item, dict) for item in response[field]):
                raise Unsupported(f"MCP {field} contains a non-object")
            items.extend(response[field])
            cursor = response.get("nextCursor")
            if response.get("hasMore") and not cursor:
                raise Unsupported("MCP hasMore=true without nextCursor")
            if not cursor:
                # This plugin has a hard 500-result search collection cap.
                if tool in {"ide_find_class", "ide_find_symbol", "ide_find_file"} and len(items) >= 500:
                    raise Unsupported("search reached server collection cap; query needs partitioning")
                break
            if cursor in cursors:
                raise Unsupported("MCP returned a repeated cursor")
            cursors.add(cursor)
            page += 1
            arguments = {"project_path": str(self.root), "pageSize": 500, "cursor": cursor}
        return items

    def class_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        subject = check["subject"]
        expected = self.paginated(check["id"], "ide_find_class", {
            "project_path": str(self.root), "query": subject, "language": "Java",
            "matchMode": "exact", "scope": "project_files", "includeGenerated": False,
            "pageSize": 500,
        }, "classes")
        actual = self.cli("class", subject, "--limit", "1000000")
        if not isinstance(actual, dict) or not isinstance(actual.get("items"), list):
            raise Unsupported("class JSON has no items")
        pagination = actual.get("pagination", {})
        if pagination.get("has_more") or pagination.get("hasMore") or pagination.get("truncated"):
            raise Unsupported("ast-index class results are truncated")
        qualified = check["feature"] == "class-qualified"
        return expected, actual, type_keys(expected, self.root, subject, qualified=qualified), type_keys(actual["items"], self.root, subject, qualified=qualified)

    def symbol_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        subject = check["subject"]
        expected = self.paginated(check["id"], "ide_find_symbol", {
            "project_path": str(self.root), "query": subject, "language": "Java",
            "scope": "project_files", "includeGenerated": False, "pageSize": 500,
        }, "symbols")
        actual = self.cli("symbol", subject, "--limit", "1000000")
        if not isinstance(actual, dict) or not isinstance(actual.get("items"), list):
            raise Unsupported("symbol JSON has no items")
        if any(actual.get("pagination", {}).get(key) for key in ("has_more", "hasMore", "truncated")):
            raise Unsupported("ast-index symbol results are truncated")
        return expected, actual, declaration_keys(expected, self.root, subject), declaration_keys(actual["items"], self.root, subject)

    def file_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        subject = check["subject"]
        expected = self.paginated(check["id"], "ide_find_file", {
            "project_path": str(self.root), "query": subject,
            "scope": "project_files", "includeGenerated": False, "pageSize": 500,
        }, "files")
        actual = self.cli("file", subject, "--limit", "1000000")
        if not isinstance(actual, list) or any(not isinstance(path, str) for path in actual):
            raise Unsupported("file JSON must be a string array")
        expected_keys = {relative_path(item.get("path"), self.root) for item in expected if item.get("name") == subject}
        actual_keys = {relative_path(path, self.root) for path in actual if Path(path).name == subject}
        return expected, actual, expected_keys, actual_keys

    def evaluate(self, check: sqlite3.Row) -> None:
        with self.state:
            self.state.execute("DELETE FROM pages WHERE check_id=?", (check["id"],))
            self.state.execute("UPDATE checks SET status='running' WHERE id=?", (check["id"],))
        try:
            handler = {"class": self.class_check, "class-qualified": self.class_check, "symbol": self.symbol_check, "file": self.file_check}.get(check["feature"])
            if handler is None:
                raise Unsupported(f"no live handler for {check['feature']}")
            expected, actual, expected_keys, actual_keys = handler(check)
            missing, unexpected = sorted(expected_keys - actual_keys), sorted(actual_keys - expected_keys)
            verdict = "fail" if missing or unexpected else "pass"
            with self.state:
                self.state.execute(
                    """UPDATE checks SET status='complete',verdict=?,expected_json=?,actual_json=?,
                    diff_json=?,error=NULL,completed_at=? WHERE id=?""",
                    (verdict, canonical_json(expected), canonical_json(actual),
                     canonical_json({"missing": missing, "unexpected": unexpected}), now_ms(), check["id"]),
                )
        except (ToolError, OSError, subprocess.TimeoutExpired) as error:
            verdict = "unsupported" if isinstance(error, Unsupported) else "error"
            with self.state:
                self.state.execute(
                    "UPDATE checks SET status='complete',verdict=?,error=?,completed_at=? WHERE id=?",
                    (verdict, str(error), now_ms(), check["id"]),
                )


def plan(state: sqlite3.Connection, source_files: list[dict[str, Any]], help_text: str, candidates: Any = ()) -> None:
    features = set(re.findall(r"^  ([a-z][a-z-]+)\s{2,}\S", help_text, re.MULTILINE))
    # The human help template omits some actual clap commands. Use the enum
    # shipped with this repository too, so a new command cannot disappear.
    source = Path(__file__).resolve().parents[2] / "src/main.rs"
    if source.is_file():
        body = source.read_text().split("enum Commands {", 1)[1].split("\n}", 1)[0]
        for variant in re.findall(r"^    ([A-Z][A-Za-z0-9]*)\s*(?:\{|,)", body, re.MULTILINE):
            name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", variant)).lower()
            features.add(name)
    if not {"class", "symbol", "file"}.issubset(features):
        raise Unsupported("cannot enumerate required CLI commands")
    features.update({"global:format", "global:walk-up", "global:subtree", "global:local"})
    with state:
        for feature in sorted(features):
            state.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?)", (
                feature, "implemented" if feature in {"class", "symbol", "file"} else "pending",
                "live MCP identity" if feature in {"class", "symbol", "file"} else "comparison contract not implemented yet",
            ))
        for name in sorted({Path(entry["path"]).stem for entry in source_files}):
            for feature in ("class", "class-qualified"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted(candidates):
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({"feature": "symbol", "subject": name}), "symbol", name,
            ))
        for name in sorted({Path(entry["path"]).name for entry in source_files}):
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({"feature": "file", "subject": name}), "file", name,
            ))


def scan(arguments: argparse.Namespace) -> dict[str, Any]:
    root = Path(arguments.project_root).expanduser().resolve()
    output = Path(arguments.output_dir).expanduser().resolve()
    if output == root or root in output.parents:
        raise ToolError("artifact directory must be outside the target project")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "scan.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ToolError("another audit is already running in this artifact directory") from error
        return scan_locked(arguments)


def scan_locked(arguments: argparse.Namespace) -> dict[str, Any]:
    root = Path(arguments.project_root).expanduser().resolve()
    binary = Path(arguments.ast_index).expanduser().resolve()
    if not root.is_dir() or not binary.is_file():
        raise ToolError("target root or ast-index binary does not exist")
    output = Path(arguments.output_dir).expanduser().resolve()
    if output == root or root in output.parents:
        raise ToolError("artifact directory must be outside the target project")
    snapshot, source_files = source_snapshot(root)
    if not source_files:
        raise Unsupported("target has no Java source files; language contract needed")
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    contract = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    epoch = stable_id({"root": str(root), "snapshot": snapshot, "binary": binary_hash, "contract": contract})[:20]
    directory = output / epoch
    directory.mkdir(parents=True, exist_ok=True)
    state = connect(directory / "evidence.sqlite")
    try:
        state.executescript(SCHEMA)
        metadata = {
            "project_root": str(root), "snapshot_sha256": snapshot,
            "binary_sha256": binary_hash, "java_files": str(len(source_files)),
        }
        with state:
            state.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", metadata.items())
            state.execute("UPDATE checks SET status='pending' WHERE status='running'")
        url = arguments.mcp_url or discover_mcp_url(arguments.mcp_name)
        client = StreamableHttpMcpClient(url, arguments.timeout)
        server = client.initialize()
        tools = client.tools()
        if "ide_find_class" not in tools:
            raise Unsupported("Index MCP Server lacks ide_find_class")
        if "ide_project_status" in tools:
            status = client.call("ide_project_status", {})
            if not any(Path(p.get("path", "")).resolve() == root and p.get("open") for p in status.get("projects", [])):
                raise ToolError("target project is not open in Index MCP Server")
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('mcp_server',?)", (canonical_json(server),))
        database = directory / "index.sqlite"
        build_ast_index(str(binary), root, database, snapshot, 4, False)
        fixture = Fixture(root, binary, database, state, client)
        help_text = run_command([str(binary), "--help"], root, fixture.environment)
        plan(state, source_files, help_text, java_identifier_candidates(root))
        limit = arguments.case_limit
        processed = 0
        problems = state.execute("SELECT count(*) FROM checks WHERE verdict='fail'").fetchone()[0]
        while problems < arguments.problem_limit and (limit is None or processed < limit):
            check = state.execute("SELECT * FROM checks WHERE status='pending' ORDER BY feature,subject LIMIT 1").fetchone()
            if check is None:
                break
            fixture.evaluate(check)
            processed += 1
            problems = state.execute("SELECT count(*) FROM checks WHERE verdict='fail'").fetchone()[0]
        # Source changes invalidate evidence rather than manufacturing defects.
        if source_snapshot(root)[0] != snapshot or hashlib.sha256(binary.read_bytes()).hexdigest() != binary_hash:
            raise ToolError("target sources or binary changed while scanning; evidence is invalid")
        counts = {row[0]: row[1] for row in state.execute(
            "SELECT verdict,count(*) FROM checks WHERE status='complete' GROUP BY verdict"
        )}
        remaining = state.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0]
        pending_features = state.execute("SELECT count(*) FROM coverage WHERE status='pending'").fetchone()[0]
        summary = {
            "java_files": len(source_files), "processed_this_run": processed,
            "counts": counts, "remaining_checks": remaining,
            "unimplemented_features": pending_features,
            "complete": remaining == 0 and pending_features == 0 and not any(counts.get(key, 0) for key in ("fail", "unsupported", "error")),
            "evidence": str(directory / "evidence.sqlite"),
        }
        if arguments.case_limit is not None and arguments.case_limit <= 10:
            rows = state.execute("SELECT id,feature,subject,verdict,error FROM checks WHERE status='complete' ORDER BY feature,subject LIMIT ?", (arguments.case_limit,)).fetchall()
            summary["first_checks"] = [dict(row) for row in rows]
        return summary
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ast-index", default="target/release/ast-index")
    parser.add_argument("--mcp-name", default="intellij-index")
    parser.add_argument("--mcp-url")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--case-limit", type=int)
    parser.add_argument("--problem-limit", type=int, default=100)
    arguments = parser.parse_args()
    if arguments.case_limit is not None and arguments.case_limit < 1 or arguments.problem_limit < 1:
        parser.error("limits must be positive")
    try:
        summary = scan(arguments)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["complete"] else 1
    except (ToolError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
