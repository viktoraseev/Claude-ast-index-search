#!/usr/bin/env python3
"""Resumable, live differential checks against Index MCP Server.

One fixture executes the actual ast-index CLI for each planned check. Every
round has an isolated rebuilt index and a durable evidence database. Reference
results are consumed only after complete pagination; unknown contracts prevent
the round from being declared successful.
"""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import fnmatch
from functools import lru_cache
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
    java_code_without_literals, adapter_digest,
    file_sha256, java_files,
)
from build_index import build_ast_index, freeze_binary
from java_structure import structure_server


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
CREATE TABLE IF NOT EXISTS source_structures(
    path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL, entries_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invocation_cache(
    request_key TEXT PRIMARY KEY, response_json TEXT NOT NULL
);
"""


class Unsupported(ToolError):
    pass


class SearchCollectionCap(Unsupported):
    pass


class InvocationOracle:
    READ_ONLY = {"ide_find_class", "ide_find_symbol", "ide_find_file", "ide_find_references",
                 "ide_find_implementations", "ide_type_hierarchy", "ide_search_text"}

    def __init__(self, client: Any, state: sqlite3.Connection):
        self.client, self.state = client, state

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in self.READ_ONLY or "cursor" in arguments:
            return self.client.call(tool, arguments)
        key = stable_id({"tool": tool, "arguments": arguments})
        row = self.state.execute("SELECT response_json FROM invocation_cache WHERE request_key=?", (key,)).fetchone()
        if row is not None:
            return json.loads(row[0])
        response = self.client.call(tool, arguments)
        # Cursor snapshots may expire independently. Reuse single-page answers;
        # paginated() still rejects the search collection cap on every use.
        if isinstance(response, dict) and not any(response.get(flag) for flag in ("stale", "truncated", "hasMore", "nextCursor")):
            with self.state:
                self.state.execute("INSERT OR REPLACE INTO invocation_cache VALUES (?,?)", (key, canonical_json(response)))
        return response


LIVE_FEATURES = {"class", "class-qualified", "symbol", "file", "outline", "imports",
                 "search", "implementations", "hierarchy", "refs", "usages", "callers",
                 "stats", "query", "schema", "db-path", "outline:constructors", "search:files", "search:content", "annotations", "symbol:options", "class:options", "symbol:qualified-pattern", "class:qualified-pattern", "search:references", "search:ranking", "todo", "deprecated"}


def coverage_sources(state: sqlite3.Connection) -> dict[str, int]:
    """Keep independent and hybrid checks out of pure MCP coverage totals."""
    prefixes = ('live MCP', 'hybrid MCP/JDK', 'independent JDK',
                'live CLI against database state', 'internal CLI')
    return {prefix: state.execute(
        "SELECT count(*) FROM coverage WHERE status='implemented' AND reason LIKE ?",
        (prefix + '%',),
    ).fetchone()[0] for prefix in prefixes}


def location_keys(items: list[dict[str, Any]], root: Path) -> set[tuple[str, int]]:
    result = set()
    for item in items:
        file = relative_path(item.get("file", item.get("path")), root)
        if not file.endswith(".java"):
            continue
        line = item.get("line")
        if not isinstance(line, int) or line < 1:
            raise Unsupported("invalid reference line")
        result.add((file, line))
    return result


def complete_items(value: Any, field: str = "items") -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get(field), list):
        raise Unsupported(f"CLI JSON has no {field} array")
    pagination = value.get("pagination", {})
    if not isinstance(pagination, dict):
        raise Unsupported("invalid CLI pagination metadata")
    if field != "items":
        pagination = pagination.get(field, {})
    if not isinstance(pagination, dict):
        raise Unsupported("invalid CLI section pagination")
    if any(pagination.get(key) for key in ("has_more", "hasMore", "truncated")):
        raise Unsupported("ast-index results are truncated")
    if any(not isinstance(item, dict) for item in value[field]):
        raise Unsupported("CLI result contains a non-object")
    return value[field]


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
        if not isinstance(item, dict):
            raise Unsupported("declaration result contains a non-object")
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


@lru_cache(maxsize=16)
def java_callable_context(path: Path, modified: int, size: int) -> dict[tuple[str, int], tuple[bool, bool]]:
    """Identify constructor and anonymous-member scope without changing source.

    This also distinguishes invocations from method references in the caller
    contract. Source declarations never become oracle results. Cache at most sixteen files,
    keyed by their stat values so replays cannot reuse stale source context.
    """
    code = java_code_without_literals(path.read_text(encoding="utf-8"))
    raw_tokens = list(re.finditer(r"[\w$]+|[^\s]", code))
    tokens = []
    index = 0
    while index < len(raw_tokens):
        if raw_tokens[index].group() != "@" or (index + 1 < len(raw_tokens) and raw_tokens[index + 1].group() == "interface"):
            tokens.append(raw_tokens[index])
            index += 1
            continue
        # Annotation arguments can contain array initializer braces. They are
        # not Java declaration scopes and must not reset the member header.
        index += 2
        while index + 1 < len(raw_tokens) and raw_tokens[index].group() == ".":
            index += 2
        if index < len(raw_tokens) and raw_tokens[index].group() == "(":
            depth = 1
            index += 1
            while index < len(raw_tokens) and depth:
                depth += (raw_tokens[index].group() == "(") - (raw_tokens[index].group() == ")")
                index += 1
    contexts = {}
    scopes: list[tuple[bool, str | None]] = []
    boundary = 0
    line, previous = 1, 0
    for index, token in enumerate(tokens):
        line += code.count("\n", previous, token.start())
        previous = token.start()
        value = token.group()
        if value == "{":
            header = " ".join(t.group() for t in tokens[boundary:index])
            owner = re.search(r"\b(?:class|interface|enum|record)\s+([\w$]+)", header)
            anonymous = bool(re.search(r"\bnew\b", header)) and header.endswith(")")
            scopes.append((anonymous, owner.group(1) if owner else None))
            boundary = index + 1
        elif value == "}":
            if scopes:
                scopes.pop()
            boundary = index + 1
        elif value == ";":
            boundary = index + 1
        elif index + 1 < len(tokens) and tokens[index + 1].group() == "(" and re.fullmatch(r"[\w$]+", value):
            header = " ".join(t.group() for t in tokens[boundary:index])
            # A constructor has no return type and is immediately inside its
            # named type body. A legal method named after its class stays in scope.
            prefix = re.sub(r"@\s*[\w$]+(?:\s*\.\s*[\w$]+)*(?:\s*\([^)]*\))?", "", header)
            prefix = re.sub(r"\b(?:public|protected|private)\b", "", prefix).strip()
            prefix = re.sub(r"^<[^>]+>\s*", "", prefix).strip()
            constructor = bool(scopes and scopes[-1][1] == value and not prefix)
            contexts[(value, line)] = (constructor, any(scope[0] for scope in scopes))
    return contexts


def callable_context(root: Path, file: str, name: str, line: int) -> tuple[bool, bool]:
    path = root / file
    if not path.is_file():
        return False, False
    stat = path.stat()
    return java_callable_context(path, stat.st_mtime_ns, stat.st_size).get((name, line), (False, False))


def declaration_keys(items: list[dict[str, Any]], root: Path, name: str) -> Counter:
    kinds = {
        "class": "type", "interface": "type", "enum": "type", "record": "type",
        "method": "method", "function": "method", "constructor": "method",
        "field": "field", "property": "field", "enum_constant": "field",
        "constant": "field", "symbol": "declaration",
    }
    result = Counter()
    for item in items:
        if not isinstance(item, dict):
            raise Unsupported("declaration result contains a non-object")
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
        _, anonymous = callable_context(root, file, name, line)
        qualified = item.get("qualifiedName", item.get("qualified_name")) or ""
        if anonymous and kinds[kind] == "method":
            # Anonymous types have no Java FQN. IntelliJ attributes their
            # methods to a named outer type; compare name/file/line here.
            qualified = ""
        # Go to Symbol reports enum constants as CLASS and ordinary fields as
        # SYMBOL. Its kind is not an authoritative Java declaration category.
        # Class navigation has its own contract; here compare declaration identity.
        result[(name, "declaration", file, line, qualified)] += 1
    return result


class Fixture:
    def __init__(self, root: Path, binary: Path, database: Path, state: sqlite3.Connection, client: Any,
                 *, schedule_followups: bool = True):
        self.root, self.binary, self.state, self.client = root, binary, state, client
        self.database = database
        structure_digest = file_sha256(Path(__file__).with_name('JavaStructure.java'))
        old_structure = state.execute("SELECT value FROM metadata WHERE key='structure_sha256'").fetchone()
        if old_structure is None or old_structure[0] != structure_digest:
            with state:
                state.execute('DELETE FROM source_structures')
                state.execute("INSERT OR REPLACE INTO metadata VALUES ('structure_sha256',?)", (structure_digest,))
        self.schedule_followups = schedule_followups
        self.environment = {
            **os.environ, "AST_INDEX_DB_PATH": str(database),
            "AST_INDEX_CACHE_DIR": str(database.parent / "cache"), "NO_COLOR": "1",
        }

    def structure(self, file: str) -> list[dict[str, Any]]:
        path = self.root / relative_path(file, self.root)
        if not path.is_file():
            return []
        stat = path.stat()
        cached = self.state.execute("SELECT entries_json FROM source_structures WHERE path=? AND modified=? AND size=?",
                                    (file, stat.st_mtime_ns, stat.st_size)).fetchone()
        if cached:
            return json.loads(cached[0])
        entries = structure_server(self.database.parent).read(path)
        with self.state:
            self.state.execute("INSERT OR REPLACE INTO source_structures VALUES (?,?,?,?)",
                               (file, stat.st_mtime_ns, stat.st_size, canonical_json(entries)))
        return entries

    def native_navigation_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        excluded = Counter()
        for file in {relative_path(item.get("path", item.get("file")), self.root) for item in items}:
            if file.endswith('.java'):
                excluded.update((file, entry['name'], entry['line']) for entry in self.structure(file)
                                if entry['kind'] in {'constructor', 'accessor'})
        result = []
        for item in items:
            key = (relative_path(item.get('path', item.get('file')), self.root), item['name'], item['line'])
            if item.get('kind') == 'import':
                continue
            if item.get('kind') == 'function' and excluded[key]:
                excluded[key] -= 1
            else:
                result.append(item)
        return result

    def structure_check(self, check: sqlite3.Row):
        file = relative_path(check['subject'], self.root)
        if not (self.root / file).is_file():
            raise ToolError('source file disappeared during structure verification')
        entries = self.structure(file)
        annotations = {'@' + name for name in (
            'RestController Controller Service Repository Component Entity Table Configuration Bean GetMapping '
            'PostMapping PutMapping DeleteMapping PatchMapping RequestMapping Autowired Override Transactional '
            'SpringBootApplication EnableAutoConfiguration Test BeforeEach AfterEach BeforeAll AfterAll Inject '
            'Singleton Provides Binds Module Data Value Builder AllArgsConstructor NoArgsConstructor Getter Setter Slf4j Log4j2'
        ).split()}
        special = {(entry['name'], entry['line']) for entry in entries
                   if entry['kind'] in {'constructor', 'component', 'accessor'}}
        expected = Counter()
        for entry in entries:
            kind, name = entry['kind'], entry['name']
            if kind == 'parent':
                continue
            if kind == 'annotation':
                if name in annotations:
                    expected[(name, kind, entry['line'])] += 1
            elif kind in {'constructor', 'component', 'accessor', 'method'} and (name, entry['line']) in special:
                expected[(name, 'property' if kind == 'component' else 'function', entry['line'])] += 1
        actual = {'outline': self.cli('outline', file, '--full'),
                  'index': self.cli('symbol', '--pattern', '*', '--in-file', file, '--limit', '1000000')}
        expected_keys = Counter()
        actual_keys = Counter()
        for section, field in (('outline', 'symbols'), ('index', 'items')):
            for key, count in expected.items():
                # Java outlines intentionally omit annotation usages; symbol lookup includes them.
                if section != 'outline' or key[1] != 'annotation':
                    expected_keys[(section, *key)] += count
            for item in complete_items(actual[section], field):
                if section == 'index' and relative_path(item['path'], self.root) != file:
                    continue
                if item['kind'] == 'annotation' or (item['kind'] in {'function', 'property'}
                                                   and (item['name'], item['line']) in special):
                    actual_keys[(section, item['name'], item['kind'], item['line'])] += 1
        return entries, actual, expected_keys, actual_keys

    def cli(self, *arguments: str) -> Any:
        value = run_command([str(self.binary), "--format", "json", *arguments], self.root, self.environment)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise Unsupported("command does not provide the expected JSON contract") from error
        return parsed

    def text_cli(self, *arguments: str) -> str:
        return run_command([str(self.binary), *arguments], self.root, self.environment)

    def paginated(self, check_id: str, tool: str, arguments: dict[str, Any], field: str) -> list[dict[str, Any]]:
        items = []
        cursors = set()
        # Several oracle operations (e.g. overloads) share one check. Preserve
        # every operation, in call order, for exact request-bound replay.
        page = self.state.execute("SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?", (check_id,)).fetchone()[0]
        while True:
            response = self.client.call(tool, arguments)
            with self.state:
                self.state.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
                    (check_id, page, canonical_json(arguments), canonical_json(response), tool),
                )
            # Current Index MCP uses `usages`; older captures use `references`.
            response_field = field
            if tool == "ide_find_references" and isinstance(response, dict) and field not in response:
                response_field = "usages"
            if not isinstance(response, dict) or not isinstance(response.get(response_field), list):
                raise Unsupported(f"MCP {tool} did not return {field}")
            if response.get("stale") or response.get("truncated"):
                raise Unsupported("MCP pagination snapshot is stale or truncated")
            if any(not isinstance(item, dict) for item in response[response_field]):
                raise Unsupported(f"MCP {field} contains a non-object")
            items.extend(response[response_field])
            cursor = response.get("nextCursor")
            if response.get("hasMore") and not cursor:
                raise Unsupported("MCP hasMore=true without nextCursor")
            if not cursor:
                # This plugin has a hard 500-result search collection cap.
                if tool in {"ide_find_class", "ide_find_symbol", "ide_find_file"} and len(items) >= 500:
                    raise SearchCollectionCap("search reached server collection cap; query needs partitioning")
                if tool == "ide_search_text" and len(items) >= 5000:
                    raise SearchCollectionCap("text search reached server collection cap; query needs partitioning")
                if tool == "ide_find_references" and response.get("totalIsExact") is False:
                    raise Unsupported("MCP reference total is not exact")
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
        expected = self.oracle_symbols(check, subject)
        actual = self.cli("symbol", subject, "--limit", "1000000")
        if not isinstance(actual, dict) or not isinstance(actual.get("items"), list):
            raise Unsupported("symbol JSON has no items")
        if any(actual.get("pagination", {}).get(key) for key in ("has_more", "hasMore", "truncated")):
            raise Unsupported("ast-index symbol results are truncated")
        actual_keys = declaration_keys(self.native_navigation_items(actual["items"]), self.root, subject)
        expected = self.validate_navigation(check, subject, expected, actual_keys)
        return expected, actual, declaration_keys(expected, self.root, subject), actual_keys

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

    def oracle_symbols(self, check: sqlite3.Row, name: str, *, types: bool = False) -> list[dict[str, Any]]:
        arguments = {
            "project_path": str(self.root), "query": name, "language": "Java",
            "scope": "project_files", "includeGenerated": False, "pageSize": 500,
        }
        if types:
            arguments["matchMode"] = "exact"
            items = self.paginated(check["id"], "ide_find_class", arguments, "classes")
        else:
            # Complete broad symbol searches contain the exact-name subset.
            # Reuse short queries across checks; refine only the collection cap,
            # never stale pages, invalid responses, or real transport errors.
            for width in range(1, len(name) + 1):
                try:
                    items = self.paginated(check["id"], "ide_find_symbol", {**arguments, "query": name[:width]}, "symbols")
                    break
                except SearchCollectionCap:
                    if width == len(name):
                        raise
        return [item for item in items if item.get("name") == name
                and relative_path(item.get("file", item.get("path")), self.root).endswith(".java")]

    def validate_navigation(self, check, name, expected, actual_keys):
        """Confirm a broad-query mismatch with Go-to-Symbol's full-name query."""
        if declaration_keys(expected, self.root, name) == actual_keys:
            return expected
        items = self.paginated(check['id'], 'ide_find_symbol', {
            'project_path': str(self.root), 'query': name, 'language': 'Java',
            'scope': 'project_files', 'includeGenerated': False, 'pageSize': 500,
        }, 'symbols')
        return [item for item in items if item.get('name') == name
                and relative_path(item.get('file', item.get('path')), self.root).endswith('.java')]

    def outline_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        file = relative_path(check["subject"], self.root)
        code = java_code_without_literals((self.root / file).read_text(encoding="utf-8"))
        candidates = sorted(set(re.findall(r"\b[\w$]+\b", code)))
        expected = []
        for name in candidates:
            expected.extend(item for item in self.oracle_symbols(check, name)
                            if relative_path(item.get("file", item.get("path")), self.root) == file)
        actual = self.cli("outline", file, "--full")
        if not isinstance(actual, dict):
            raise Unsupported("unknown outline output")
        if actual.get("skipped"):
            raise Unsupported("Java outline was skipped")
        items = [{**item, "path": file} for item in complete_items(actual, "symbols")
                 if item.get("kind") != "annotation"]
        def keys(values):
            result = Counter()
            for name in {item["name"] for item in values}:
                for key, count in declaration_keys(values, self.root, name).items():
                    result[key[:4]] += count
            return result
        native = self.native_navigation_items(items)
        if keys(expected) != keys(native):
            refined = []
            for name in sorted({item['name'] for item in expected + native}):
                actual_name_keys = declaration_keys(native, self.root, name)
                confirmed = self.validate_navigation(check, name,
                    [item for item in expected if item['name'] == name], actual_name_keys)
                refined.extend(item for item in confirmed
                               if relative_path(item.get('file', item.get('path')), self.root) == file)
            expected = refined
        return expected, actual, keys(expected), keys(native)

    def imports_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        file = relative_path(check["subject"], self.root)
        references = self.oracle_text(check, {
            "project_path": str(self.root), "query": "import", "wholeWord": True,
            "context": "code", "filePattern": "*.java", "paths": [file], "pageSize": 500,
        })
        source = (self.root / file).read_text(encoding="utf-8")
        source_lines = source.splitlines(keepends=True)
        expected = []
        for reference in references:
            if relative_path(reference.get("file"), self.root) != file:
                raise Unsupported("import oracle returned a different file")
            line, column = reference.get("line"), reference.get("column")
            if not isinstance(line, int) or not isinstance(column, int) or not 1 <= line <= len(source_lines) or column < 1:
                raise Unsupported("invalid import oracle position")
            # IntelliJ columns count UTF-16 code units. Only server-confirmed
            # import anchors are rendered into the CLI's statement representation.
            row = source_lines[line - 1]
            units, character = 0, 0
            while units < column - 1 and character < len(row):
                units += 2 if ord(row[character]) > 0xffff else 1
                character += 1
            if units != column - 1:
                raise Unsupported("import column is not a character boundary")
            tail = row[character:] + "".join(source_lines[line:])
            end = tail.find(";")
            if end < 0 or not re.match(r"import\b", tail):
                raise Unsupported("import oracle anchor does not match the source snapshot")
            code = java_code_without_literals(tail[:end + 1])
            match = re.fullmatch(r"import\s+(static\s+)?([\w$.*\s]+?)\s*;", code)
            if not match:
                raise Unsupported("unrecognized server-confirmed import statement")
            expected.append(("static " if match[1] else "") + re.sub(r"\s+", "", match[2]))
        output = self.text_cli("imports", file)
        lines = output.splitlines()
        if not lines or lines[0] != f"Imports in {file}:":
            raise Unsupported("unrecognized import output")
        actual = []
        for line in lines[1:]:
            value = line.strip()
            if not value or value == "No imports found." or re.fullmatch(r"Total: \d+ imports", value):
                continue
            if not re.fullmatch(r"(?:static\s+)?[\w$.*]+;?", value):
                raise Unsupported("unrecognized Java import entry")
            actual.append(value.rstrip(";"))
        return expected, actual, set(expected), set(actual)

    def search_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        expected = self.oracle_symbols(check, check["subject"])
        actual = self.cli("search", check["subject"], "--limit", "1000000")
        items = complete_items(actual, "symbols")
        actual_keys = declaration_keys(self.native_navigation_items(items), self.root, check['subject'])
        expected = self.validate_navigation(check, check['subject'], expected, actual_keys)
        return expected, actual, declaration_keys(expected, self.root, check['subject']), actual_keys

    def search_files_check(self, check: sqlite3.Row):
        name = check['subject']
        expected = self.paginated(check['id'], 'ide_find_file', {
            'project_path': str(self.root), 'query': name, 'scope': 'project_files',
            'includeGenerated': False, 'pageSize': 500,
        }, 'files')
        actual = self.cli('search', name, '--limit', '1000000')
        def paths(items, *, oracle=False):
            return {relative_path(item['path'], self.root) for item in items
                    if item['path'].endswith('.java') and (not oracle or name.lower() in item['path'].lower())}
        if not isinstance(actual, dict) or not isinstance(actual.get('files'), list):
            raise Unsupported('search JSON has no files array')
        pagination = actual.get('pagination', {})
        if not isinstance(pagination, dict) or not isinstance(pagination.get('files', {}), dict):
            raise Unsupported('invalid search file pagination')
        page = pagination.get('files', {})
        if any(page.get(key) for key in ('has_more', 'hasMore', 'truncated')):
            raise Unsupported('search file results are truncated')
        if any(not isinstance(path, str) for path in actual['files']):
            raise Unsupported('search file result contains a non-path')
        return expected, actual, paths(expected, oracle=True), paths([{'path': path} for path in actual['files']])

    def oracle_text(self, check: sqlite3.Row, arguments: dict[str, Any]):
        try:
            return self.paginated(check['id'], 'ide_search_text', arguments, 'matches')
        except SearchCollectionCap:
            if 'paths' in arguments:
                # A single file still reaching the cap is incomplete evidence.
                raise
            items = []
            for path in java_files(self.root):
                relative = path.relative_to(self.root).as_posix()
                items.extend(self.paginated(check['id'], 'ide_search_text',
                                            {**arguments, 'paths': [relative]}, 'matches'))
            return items

    def text_search_check(self, check: sqlite3.Row):
        annotation = check['feature'] == 'annotations'
        query = '@' + check['subject'].lstrip('@') if annotation else check['subject']
        expected = self.oracle_text(check, {
            'project_path': str(self.root), 'query': query, 'caseSensitive': True,
            'context': 'all', 'filePattern': '*.java', 'pageSize': 500,
        })
        if annotation:
            actual = self.text_cli('annotations', check['subject'], '--limit', '1000000')
            lines = actual.splitlines()
            header = re.fullmatch(r'Classes with .+ \((\d+)\):', lines[0]) if lines else None
            if not header:
                raise Unsupported('unrecognized annotations output')
            items = [{'path': match[1], 'line': int(match[2])} for line in lines[1:]
                     if (match := re.fullmatch(r'  (\S.*):(\d+)', line))]
            if len(items) != int(header[1]) or len(items) >= 1000000:
                raise Unsupported('incomplete annotations output')
        else:
            actual = self.cli('search', query, '--limit', '1000000')
            items = complete_items(actual, 'content_matches')
        return expected, actual, location_keys(expected, self.root), location_keys(items, self.root)

    def semantic_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        feature, name = check["feature"], check["subject"]
        definitions = self.oracle_symbols(check, name, types=feature in {"implementations", "hierarchy"})
        items, parents, children = [], set(), set()
        for definition in definitions:
            location_keys([definition], self.root)
            file = relative_path(definition.get("file", definition.get("path")), self.root)
            anchors = [item for item in self.structure(file) if item['name'] == name
                       and item.get('line') == definition['line'] and item['kind'] not in {'parent', 'usage', 'import', 'accessor'}]
            column = (anchors[0].get('column') if len(anchors) == 1 else None) or definition.get('column') or 1
            arguments = {
                "project_path": str(self.root), "file": file,
                "line": definition["line"], "column": column,
                "scope": "project_files", "includeGenerated": False, "pageSize": 500,
            }
            if feature == "hierarchy":
                response = self.client.call("ide_type_hierarchy", {k: v for k, v in arguments.items() if k != "pageSize"})
                page = self.state.execute("SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?", (check["id"],)).fetchone()[0]
                with self.state:
                    self.state.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (check["id"], page, canonical_json({k: v for k, v in arguments.items() if k != "pageSize"}), canonical_json(response), "ide_type_hierarchy"))
                if not isinstance(response, dict) or any(not isinstance(response.get(field), list) for field in ("supertypes", "subtypes")):
                    raise Unsupported("unknown hierarchy response")
                if any(response.get(key) for key in ("stale", "truncated", "hasMore", "nextCursor")):
                    raise Unsupported("incomplete hierarchy response")
                # The CLI exposes explicit parents, including external types, while
                # project-only IDE hierarchy omits libraries and adds implicit JVM parents.
                # Parse those source edges independently; retain MCP for child navigation.
                parents.update(entry['name'].rsplit('.', 1)[-1] for entry in self.structure(arguments['file'])
                               if entry['kind'] == 'parent' and entry['owner'] == name)
                children.update((item["name"].rsplit(".", 1)[-1], relative_path(item.get("file", item.get("path")), self.root))
                                for item in response["subtypes"])
            else:
                tool, field = ("ide_find_implementations", "implementations") if feature == "implementations" else ("ide_find_references", "references")
                page_start = self.state.execute('SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?', (check['id'],)).fetchone()[0]
                found = self.paginated(check["id"], tool, arguments, field)
                response = json.loads(self.state.execute('SELECT response_json FROM pages WHERE check_id=? AND page=?', (check['id'], page_start)).fetchone()[0])
                resolved = response.get('resolvedSymbol')
                if tool == 'ide_find_references' and resolved is not None:
                    if not isinstance(resolved, dict) or resolved.get('name') != name:
                        # Enum-constant positions can resolve the enum constructor
                        # even on the constant's identifier. Request its FQN member.
                        qualified = definition.get('qualifiedName')
                        if not isinstance(qualified, str) or not qualified:
                            raise Unsupported('reference oracle resolved a different declaration without an FQN')
                        member = any(anchor['kind'] in {'constant', 'property', 'method'} for anchor in anchors)
                        if member:
                            owner, separator, segment = qualified.rpartition('.')
                            if not separator or segment != name:
                                raise Unsupported('cannot form a qualified reference member')
                            qualified = owner + '#' + segment
                        request = {key: value for key, value in arguments.items() if key not in {'file', 'line', 'column'}}
                        request.update(language='Java', symbol=qualified)
                        page_start = self.state.execute('SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?', (check['id'],)).fetchone()[0]
                        found = self.paginated(check['id'], tool, request, field)
                        response = json.loads(self.state.execute('SELECT response_json FROM pages WHERE check_id=? AND page=?', (check['id'], page_start)).fetchone()[0])
                        if not isinstance(response.get('resolvedSymbol'), dict) or response['resolvedSymbol'].get('name') != name:
                            raise Unsupported('qualified reference oracle resolved a different declaration')
                items.extend(found)
        if feature == "hierarchy":
            actual = self.text_cli("hierarchy", name, "--limit", "1000000")
            actual_parents, actual_children = set(), set()
            for line in actual.splitlines():
                value = line.strip()
                parent = re.fullmatch(r"(.+) \((?:extends|implements)\)", value)
                child = re.fullmatch(r"(.+) \[[\w_]+\]: (.+)", value)
                if parent:
                    actual_parents.add(parent[1].rsplit(".", 1)[-1])
                elif child:
                    actual_children.add((child[1].rsplit(".", 1)[-1], relative_path(child[2], self.root)))
                elif not value or value.startswith(("Hierarchy for '", "Parents:", "Children (")):
                    continue
                elif value == f"Class '{name}' not found.":
                    actual_parents.add("<missing target>")
                else:
                    raise Unsupported("unrecognized hierarchy output")
            return {"parents": sorted(parents), "children": sorted(children)}, actual, {("parent", p) for p in parents} | {("child", *c) for c in children}, {("parent", p) for p in actual_parents} | {("child", *c) for c in actual_children}
        actual = self.cli(feature, name, "--limit", "1000000")
        if feature == "implementations":
            return items, actual, location_keys(items, self.root), location_keys(complete_items(actual), self.root)
        syntax = []
        if feature in {'refs', 'usages', 'callers'}:
            for path in java_files(self.root):
                file = path.relative_to(self.root).as_posix()
                syntax.extend({**entry, 'file': file} for entry in self.structure(file)
                              if entry['kind'] != 'parent' and entry['name'] == name)
        imports = [item for item in items if str(item.get("type", "")).upper() == "IMPORT"]
        usages = [item for item in items if str(item.get("type", "")).upper() != "IMPORT"]
        # Name-only CLI references span all Java namespaces. The syntax oracle
        # supplies lexical sites (including same-name external members), while
        # the IDE still confirms project declarations and their semantic sites.
        lexical_locations = {(entry['file'], entry['line']) for entry in syntax if entry['kind'] == 'usage'}
        # IDE enum constructor edges can point at constants where the type's
        # identifier never appears. Name-only CLI references require an explicit
        # syntactic mention, so retain semantic sites only within that scope.
        usages = [item for item in usages if location_keys([item], self.root) <= lexical_locations]
        usages.extend(entry for entry in syntax if entry['kind'] == 'usage')
        imports.extend(entry for entry in syntax if entry['kind'] == 'import')
        definitions = definitions + [{**entry, 'kind': 'function'} for entry in syntax if entry['kind'] in {'constructor', 'accessor'}]
        expected = {"definitions": definitions, "imports": imports, "usages": usages}
        if feature == "refs":
            def keys(value):
                declarations = declaration_keys(value["definitions"], self.root, name)
                return {("definition", d[2], d[3]) for d in declarations} | {("import", *loc) for loc in location_keys(value["imports"], self.root)} | {("usage", *loc) for loc in location_keys(value["usages"], self.root)}
            for field in expected:
                complete_items(actual, field)
            return expected, actual, keys(expected), keys(actual)
        expected_locations = location_keys(usages, self.root)
        if feature == "callers":
            # Callers is a lexical call-site command. Semantic references such
            # as method references (obj::run) do not meet that CLI contract.
            calls = set()
            for file, line in expected_locations:
                path = self.root / file
                stat = path.stat()
                if (name, line) in java_callable_context(path, stat.st_mtime_ns, stat.st_size):
                    calls.add((file, line))
            expected_locations = calls
        return expected, actual, expected_locations, location_keys(complete_items(actual), self.root)

    def grep_check(self, check: sqlite3.Row):
        feature = check['feature']
        patterns = {
            'todo': r'//.*(TODO|FIXME|HACK)|#.*(TODO|FIXME|HACK)',
            'deprecated': r'@Deprecated|@Obsolete|@available\s*\([^)]*deprecated|#\[deprecated|#.*DEPRECATED|=head.*DEPRECATED|@deprecated|\[\[deprecated',
        }
        expected = self.oracle_text(check, {
            'project_path': str(self.root), 'query': patterns[feature], 'regex': True,
            'caseSensitive': True, 'context': 'all', 'filePattern': '*.java', 'pageSize': 500,
        })
        actual = self.text_cli(feature, '--limit', '1000000')
        if any(re.search(r'\.\.\. and \d+ more', line) for line in actual.splitlines()):
            raise Unsupported('grep output omits collected locations')
        items = [{'path': match[1], 'line': int(match[2])} for line in actual.splitlines()
                 if (match := re.fullmatch(r'  (\S.*):(\d+)', line))]
        return expected, actual, location_keys(expected, self.root), location_keys(items, self.root)

    def option_check(self, check: sqlite3.Row):
        file = relative_path(check['subject'], self.root)
        command = check['feature'].split(':')[0]
        qualified_contract = check['feature'].endswith(':qualified-pattern')
        kind_map = {'constructor': 'function', 'method': 'function', 'accessor': 'function',
                    'component': 'property'}
        significant = set(('RestController Controller Service Repository Component Entity Table Configuration Bean '
            'GetMapping PostMapping PutMapping DeleteMapping PatchMapping RequestMapping Autowired Override Transactional '
            'SpringBootApplication EnableAutoConfiguration Test BeforeEach AfterEach BeforeAll AfterAll Inject Singleton '
            'Provides Binds Module Data Value Builder AllArgsConstructor NoArgsConstructor Getter Setter Slf4j Log4j2').split())
        entries = []
        for path in java_files(self.root):
            relative = path.relative_to(self.root).as_posix()
            if file not in relative:
                continue
            for item in self.structure(relative):
                if item['kind'] in {'parent', 'usage'} or (item['kind'] == 'annotation' and item['name'].lstrip('@') not in significant):
                    continue
                kind = kind_map.get(item['kind'], item['kind'])
                if command == 'class' and kind not in {'class', 'interface', 'enum'}:
                    continue
                entries.append({**item, 'kind': kind, 'path': relative})
        expected, actual = Counter(), Counter()
        outputs = {}
        def record(label, arguments, selected, *, content=False):
            value = self.cli(command, *arguments, '--in-file', file, '--limit', '1000000')
            outputs[label] = value
            items = complete_items(value)
            for item in selected:
                expected[(label, item['name'], item['kind'], item['path'], item['line'])] += 1
            for item in items:
                actual[(label, item['name'], item['kind'], relative_path(item['path'], self.root), item['line'])] += 1
            if qualified_contract:
                for item in selected:
                    expected[(label, 'qualified', item['name'], item['kind'], item['path'], item['line'], item.get('qualified_name'))] += 1
                for item in items:
                    actual[(label, 'qualified', item['name'], item['kind'], relative_path(item['path'], self.root), item['line'], item.get('qualified_name'))] += 1
            pagination = value.get('pagination', {})
            expected[(label, 'total', len(selected))] += 1
            actual[(label, 'total', pagination.get('total'))] += 1
            if content:
                spans = {(i['path'], i['name'], i['kind'], i['line']): i for i in selected
                         if i['kind'] != 'annotation' and i.get('end_line', i['line']) >= i['line']}
                for item in items:
                    key = (relative_path(item['path'], self.root), item['name'], item['kind'], item['line'])
                    oracle = spans.get(key)
                    if oracle is None:
                        continue
                    lines = (self.root / key[0]).read_text(encoding='utf-8').splitlines()
                    start, end = oracle['line'], oracle['end_line']
                    # Source display has a documented 60-line window. Compare
                    # source lines and range, rather than merely checking JSON keys.
                    snippet = item.get('content')
                    rendered = []
                    if isinstance(snippet, str):
                        for row in snippet.splitlines():
                            match = re.fullmatch(r'\s*(\d+)\t(.*)', row)
                            if not match:
                                raise Unsupported('invalid numbered source body')
                            rendered.append((int(match[1]), match[2]))
                    expected_rows = list(enumerate(lines[start - 1:min(end, start + 59)], start))
                    expected[(label, 'body', *key, canonical_json(expected_rows), end, end >= start + 60)] += 1
                    actual[(label, 'body', *key, canonical_json(rendered), item.get('end_line'), item.get('truncated'))] += 1
        if qualified_contract:
            # Qualified patterns must use independent syntax names, not names
            # read from the same native index that the CLI searches.
            patterns = {'*.*'}
            for entry in entries:
                qualified = entry.get('qualified_name')
                if qualified and '.' in qualified:
                    patterns.add(qualified.rsplit('.', 1)[0] + '.*')
                    break
            for pattern in sorted(patterns):
                record('qualified:' + pattern, ['--pattern', pattern], [entry for entry in entries
                    if fnmatch.fnmatchcase((entry.get('qualified_name') or entry['name']).lower(), pattern.lower())])
            if command == 'symbol' and entries:
                seed = entries[0]['name']
                pattern = next((value for value in sorted(patterns) if value != '*.*'), '*.*')
                for kind in sorted({entry['kind'] for entry in entries}):
                    record('qualified-kind:' + kind, ['--pattern', pattern, '--type', kind], [entry for entry in entries
                        if entry['kind'] == kind and fnmatch.fnmatchcase(
                            (entry.get('qualified_name') or entry['name']).lower(), pattern.lower())])
                    record('fuzzy-kind:' + kind, [seed, '--fuzzy', '--type', kind], [entry for entry in entries
                        if entry['kind'] == kind and seed.lower() in entry['name'].lower()])
            return entries, outputs, expected, actual
        record('pattern', ['--pattern', '*'], entries)
        if entries:
            seed = next((i['name'] for i in entries if i['kind'] in {'class', 'interface', 'enum'}), entries[0]['name'])
            pattern = seed[:max(1, len(seed) // 2)] + '*'
            selected = [i for i in entries if fnmatch.fnmatchcase(i['name'].lower(), pattern.lower())]
            record('glob', ['--pattern', pattern], selected)
            record('fuzzy', [seed, '--fuzzy'], [i for i in entries if seed.lower() in i['name'].lower()])
        # A negative scope still executes the full command and must return no declarations.
        record('module', ['--pattern', '*', '--module', '__audit_absent_module__'], [])
        if command == 'symbol':
            for kind in sorted({i['kind'] for i in entries}):
                selected = [i for i in entries if i['kind'] == kind]
                record('kind:' + kind, ['--pattern', '*', '--type', kind, '--with-content'], selected, content=True)
        return entries, outputs, expected, actual

    def search_aggregation_check(self, check: sqlite3.Row):
        query = check['subject']
        source = connect(self.database, read_only=True)
        try:
            counts = Counter()
            for row in source.execute('SELECT name FROM refs'):
                if row[0].lower().startswith(query.lower()):
                    counts[row[0]] += 1
        finally:
            source.close()
        expected = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        actual = self.cli('search', query, '--limit', '1000000')
        items = complete_items(actual, 'references')
        actual_keys = {(index, item['name'], item['usage_count']) for index, item in enumerate(items)}
        return expected, actual, {(index, *item) for index, item in enumerate(expected)}, actual_keys

    def search_ranking_check(self, check: sqlite3.Row):
        query = check['subject']
        full = self.cli('search', query, '--fuzzy', '--limit', '1000000')
        items = complete_items(full, 'symbols')
        files = full.get('files')
        if not isinstance(files, list):
            raise Unsupported('search has no file ranking')
        # Relevance tiers are public search semantics: exact matches precede
        # other fuzzy matches. Compare identities and limited-page stability too.
        expected, actual = set(), set()
        for section, values in [('symbols', items), ('files', files)]:
            tiers = []
            for value in values:
                name = value['name'] if section == 'symbols' else Path(value).name
                tiers.append(0 if name.lower() == query.lower() else 1)
            expected.add((section, 'tiers', tuple(sorted(tiers))))
            actual.add((section, 'tiers', tuple(tiers)))
        outputs = {'full': full}
        for limit in (1, 3):
            page = self.cli('search', query, '--fuzzy', '--limit', str(limit))
            outputs[str(limit)] = page
            for section in ('symbols', 'files', 'references', 'content_matches'):
                values = page.get(section)
                if not isinstance(values, list):
                    raise Unsupported('missing search page section')
                expected.add((section, limit, canonical_json(full[section][:limit])))
                actual.add((section, limit, canonical_json(values)))
                metadata = page.get('pagination', {}).get(section, {})
                expected.add((section, limit, 'total', len(full[section])))
                actual.add((section, limit, 'total', metadata.get('total')))
                expected.add((section, limit, 'more', len(full[section]) > limit))
                actual.add((section, limit, 'more', metadata.get('truncated')))
        return full, outputs, expected, actual

    def introspection_check(self, check: sqlite3.Row) -> tuple[Any, Any, set[Any], set[Any]]:
        feature = check["feature"]
        if feature == "db-path":
            expected = str(self.database.resolve())
            actual = self.text_cli("db-path").strip()
            resolved = str(Path(actual).resolve()) if Path(actual).is_absolute() else actual
            return expected, actual, {expected}, {resolved}
        source = connect(self.database, read_only=True)
        try:
            if feature == "query":
                sql = "SELECT path FROM files ORDER BY path"
                expected = [row[0] for row in source.execute(sql)]
                actual = self.cli("query", sql, "--limit", "1000000")
                if not isinstance(actual, dict) or not isinstance(actual.get("rows"), list):
                    raise Unsupported("unknown query output")
                return expected, actual, set(expected), {row["path"] for row in actual["rows"]}
            if feature == "stats":
                tables = {"file_count": "files", "symbol_count": "symbols", "refs_count": "refs",
                          "module_count": "modules", "xml_usages_count": "xml_usages", "resources_count": "resources",
                          "storyboard_usages_count": "storyboard_usages", "ios_assets_count": "ios_assets"}
                expected = {key: source.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for key, table in tables.items()}
                actual = self.cli("stats")
                if not isinstance(actual, dict) or not isinstance(actual.get("stats"), dict):
                    raise Unsupported("unknown statistics output")
                return expected, actual, set(expected.items()), set(actual["stats"].items())
            expected = {}
            for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name"):
                table = row[0]
                quoted = '"' + table.replace('"', '""') + '"'
                expected[table] = {
                    "columns": [{"name": column[1], "type": column[2], "not_null": bool(column[3]), "primary_key": bool(column[5])}
                                for column in source.execute(f"PRAGMA table_info({quoted})")],
                    "row_count": source.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0],
                }
            actual = self.cli("schema")
            if not isinstance(actual, dict):
                raise Unsupported("unknown schema output")
            return expected, actual, {(key, canonical_json(value)) for key, value in expected.items()}, {(key, canonical_json(value)) for key, value in actual.items()}
        finally:
            source.close()

    def evaluate(self, check: sqlite3.Row) -> None:
        with self.state:
            self.state.execute("DELETE FROM pages WHERE check_id=?", (check["id"],))
            self.state.execute("UPDATE checks SET status='running' WHERE id=?", (check["id"],))
        try:
            handler = {"class": self.class_check, "class-qualified": self.class_check, "symbol": self.symbol_check,
                       "file": self.file_check, "outline": self.outline_check, "outline:constructors": self.structure_check, "imports": self.imports_check,
                       "search": self.search_check, "search:files": self.search_files_check,
                       "symbol:options": self.option_check, "class:options": self.option_check,
                       "symbol:qualified-pattern": self.option_check, "class:qualified-pattern": self.option_check,
                       "todo": self.grep_check, "deprecated": self.grep_check,
                       "search:references": self.search_aggregation_check, "search:ranking": self.search_ranking_check,
                       "search:content": self.text_search_check, "annotations": self.text_search_check, **dict.fromkeys(("implementations", "hierarchy", "refs", "usages", "callers"), self.semantic_check)}.get(check["feature"])
            if check["feature"] in {"stats", "query", "schema", "db-path"}:
                handler = self.introspection_check
            if handler is None:
                raise Unsupported(f"no live handler for {check['feature']}")
            expected, actual, expected_keys, actual_keys = handler(check)
            missing_keys, unexpected_keys = expected_keys - actual_keys, actual_keys - expected_keys
            missing = sorted(missing_keys.elements() if isinstance(missing_keys, Counter) else missing_keys)
            unexpected = sorted(unexpected_keys.elements() if isinstance(unexpected_keys, Counter) else unexpected_keys)
            verdict = "fail" if missing or unexpected else "pass"
            with self.state:
                # Lexical candidates include keywords, locals and external
                # symbols. Only confirmed project declarations have semantic
                # reference contracts; do not manufacture empty oracle scopes.
                followups = []
                if self.schedule_followups and check["feature"] == "symbol" and any(item.get("name") == check["subject"] for item in expected):
                    followups.extend(("refs", "usages"))
                    if any(item.get("name") == check["subject"] and str(item.get("kind", "")).lower() in {"method", "function"} for item in expected):
                        followups.append("callers")
                if self.schedule_followups and check["feature"] == "class" and any(item.get("name") == check["subject"] for item in expected):
                    followups.extend(("implementations", "hierarchy"))
                for feature in followups:
                    self.state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                        stable_id({"feature": feature, "subject": check["subject"]}), feature, check["subject"],
                    ))
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


def plan(state: sqlite3.Connection, source_files: list[dict[str, Any]], help_text: str, candidates: Any = (), root: Path | None = None) -> None:
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
    features.update(LIVE_FEATURES)
    # A base navigation handler does not establish coverage of source bodies,
    # search ranking, or constructor/annotation entries omitted by Go-to-Symbol.
    pending_contracts = {
        "search:rank-presets": "history/graph ranking presets and test exclusion contracts not implemented yet",
    }
    type_names = {Path(entry["path"]).stem for entry in source_files}
    annotation_names = set()
    if root is not None:
        for entry in source_files:
            code = java_code_without_literals((root / entry["path"]).read_text(encoding="utf-8"))
            annotation_names.update(re.findall(r"@([\w$]+)", code))
            type_names.update(re.findall(r"\b(?:class|interface|enum|record)\s+([\w$]+)", code))
    with state:
        for feature in sorted(features):
            state.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?)", (
                feature, "implemented" if feature in LIVE_FEATURES else "pending",
                ("independent JDK syntax against outline and indexed symbols" if feature == "outline:constructors" else
                 "live MCP text locations" if feature in {"annotations", "search:content", "todo", "deprecated"} else
                 "independent JDK syntax: qualified patterns and combined fuzzy/kind filters" if feature in {"symbol:qualified-pattern", "class:qualified-pattern"} else
                 "independent JDK syntax: patterns, filters, fuzzy lookup and source bodies" if feature in {"symbol:options", "class:options"} else
                 "internal CLI/DB reference aggregation and ordering; not MCP equivalence" if feature == "search:references" else
                 "internal CLI relevance tiers, limited-page stability and totals; not MCP equivalence" if feature == "search:ranking" else
                 "hybrid MCP/JDK declarations/semantic sites plus independent name-only lexical scope" if feature in {"refs", "usages", "callers"} else
                 "hybrid MCP/JDK child navigation and explicit source parent edges" if feature == "hierarchy" else
                 "live MCP code anchors rendered as import statements" if feature == "imports" else
                 "live CLI against database state" if feature in {"stats", "query", "schema", "db-path"} else
                 "live MCP navigation identity") if feature in LIVE_FEATURES else "comparison contract not implemented yet",
            ))
        for feature, reason in pending_contracts.items():
            state.execute("INSERT OR IGNORE INTO coverage VALUES (?,'pending',?)", (feature, reason))
        for name in sorted(type_names):
            for feature in ("class", "class-qualified"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted(candidates):
            for feature in ("symbol", "search", "search:content", "search:references", "search:ranking"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted({Path(entry["path"]).name for entry in source_files}):
            for feature in ('file', 'search:files'):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted(annotation_names):
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({"feature": 'annotations', "subject": name}), 'annotations', name,
            ))
        for entry in source_files:
            for feature in ("outline", "imports", "outline:constructors", "symbol:options", "class:options", "symbol:qualified-pattern", "class:qualified-pattern"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": entry["path"]}), feature, entry["path"],
                ))
        for feature in ("stats", "query", "schema", "db-path", "todo", "deprecated"):
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({"feature": feature, "subject": "index-state"}), feature, "index-state",
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
    binary_hash = file_sha256(binary)
    contract = adapter_digest()
    epoch = stable_id({"root": str(root), "snapshot": snapshot, "binary": binary_hash, "contract": contract})[:20]
    directory = output / epoch
    directory.mkdir(parents=True, exist_ok=True)
    binary = freeze_binary(binary, directory, binary_hash)
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
            # Keep case evidence, not cached answers from an earlier IDE session.
            state.execute("DELETE FROM invocation_cache")
        url = arguments.mcp_url or discover_mcp_url(arguments.mcp_name)
        client = StreamableHttpMcpClient(url, arguments.timeout)
        server = client.initialize()
        tools = client.tools()
        required = {"ide_find_class", "ide_find_symbol", "ide_find_file", "ide_find_references",
                    "ide_find_implementations", "ide_type_hierarchy", "ide_search_text"}
        missing = required - tools.keys()
        if missing:
            raise Unsupported("Index MCP Server lacks tools required by live contracts: " + ", ".join(sorted(missing)))
        if "ide_project_status" in tools:
            status = client.call("ide_project_status", {})
            if not any(Path(p.get("path", "")).resolve() == root and p.get("open") for p in status.get("projects", [])):
                raise ToolError("target project is not open in Index MCP Server")
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('mcp_server',?)", (canonical_json(server),))
        database = directory / "index.sqlite"
        build_ast_index(str(binary), root, database, snapshot, 4, False)
        fixture = Fixture(root, binary, database, state, InvocationOracle(client, state))
        help_text = run_command([str(binary), "--help"], root, fixture.environment)
        plan(state, source_files, help_text, java_identifier_candidates(root), root)
        limit = arguments.case_limit
        processed = 0
        problems = state.execute("SELECT count(*) FROM checks WHERE verdict IN ('fail','unsupported')").fetchone()[0]
        while problems < arguments.problem_limit and (limit is None or processed < limit):
            check = state.execute("SELECT * FROM checks WHERE status='pending' ORDER BY feature,subject LIMIT 1").fetchone()
            if check is None:
                break
            fixture.evaluate(check)
            processed += 1
            problems = state.execute("SELECT count(*) FROM checks WHERE verdict IN ('fail','unsupported')").fetchone()[0]
            if state.execute("SELECT verdict FROM checks WHERE id=?", (check["id"],)).fetchone()[0] == "error":
                break
        # Source changes invalidate evidence rather than manufacturing defects.
        if source_snapshot(root)[0] != snapshot or file_sha256(binary) != binary_hash:
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
            "coverage_sources": coverage_sources(state),
            "complete": remaining == 0 and pending_features == 0 and not any(counts.get(key, 0) for key in ("fail", "unsupported", "error")),
            "evidence": str(directory / "evidence.sqlite"),
        }
        if arguments.case_limit is not None and arguments.case_limit <= 10:
            rows = state.execute("SELECT id,feature,subject,verdict,error FROM checks WHERE status='complete' ORDER BY feature,subject LIMIT ?", (arguments.case_limit,)).fetchall()
            summary["first_checks"] = [{"feature": row["feature"], "verdict": row["verdict"]} for row in rows]
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
