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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
import time
import tomllib
from typing import Any

from common import (
    StreamableHttpMcpClient, ToolError, McpRemoteError, canonical_json, connect,
    discover_mcp_url, java_identifier_candidates, now_ms, source_snapshot, stable_id,
    java_code_without_literals, adapter_digest,
    file_sha256, java_files,
)
from build_index import build_ast_index, capture_binary, freeze_binary
from java_structure import structure_server
from oracle_store import Metrics, OracleStore, Reply, ReplyCache, SCHEMA as ORACLE_SCHEMA
import mobile_contracts
import perl_contracts
import annotation_contracts
import text_snapshot
import lifecycle_contracts
import root_contracts
import module_contracts
import install_contracts
import profile_contracts
import delegate_contracts
import route_contracts
import android_contracts
import android_syntax_contracts
import java_resource_contracts
import vcs_contracts
import rank_contracts
import stack_contracts
import context_contracts
import explore_contracts
import graph_contracts
import java_receiver_contracts
import java_exception_scope_contracts
import java_local_interface_contracts
import graph_mcp_contracts
import java_pattern_scope_contracts
import java_type_binding_contracts
import java_type_access_contracts
import java_inherited_type_contracts
import java_parent_contracts
import unused_dep_contracts
import java_dependency_contracts
import android_dependency_contracts
import format_contracts
import navigation_format_contracts
import file_view_contracts
import file_scope_contracts
import navigation_scope_contracts
import caller_scope_contracts
import caller_format_contracts
import search_format_contracts
import module_format_contracts
import insight_scope_contracts
import analysis_scope_contracts
import exploration_format_contracts
import management_format_contracts
import lifecycle_format_contracts
import mutation_format_contracts
import project_format_contracts
import module_scope_contracts
import module_root_contracts
import module_alias_contracts
import graph_root_contracts
import graph_directory_contracts
import graph_ambiguity_contracts
import call_hierarchy_contracts


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
CREATE INDEX IF NOT EXISTS checks_verdict ON checks(verdict);
CREATE INDEX IF NOT EXISTS checks_schedule ON checks(
    status,(feature='outline' OR feature GLOB 'outline:*'),feature,subject
);
CREATE INDEX IF NOT EXISTS checks_outline_blockers ON checks(id)
    WHERE NOT (feature='outline' OR feature GLOB 'outline:*')
      AND (status!='complete' OR verdict IS NOT 'pass');
CREATE TABLE IF NOT EXISTS source_structures(
    path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL, entries_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_injection_targets(
    name TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
    PRIMARY KEY(name,path,line)
);
""" + ORACLE_SCHEMA + mobile_contracts.SCHEMA + text_snapshot.SCHEMA + android_contracts.SCHEMA + call_hierarchy_contracts.SCHEMA + graph_mcp_contracts.SCHEMA


class Unsupported(ToolError):
    pass


class SearchCollectionCap(Unsupported):
    pass


def next_check(state: sqlite3.Connection):
    """Defer outline until all other applicable contracts actually pass.

    The expression/partial indexes keep this O(log N), rather than repeatedly
    scanning all deferred files or all completed checks on large projects.
    """
    check = state.execute("""SELECT * FROM checks WHERE status='pending'
        ORDER BY (feature='outline' OR feature GLOB 'outline:*'),feature,subject LIMIT 1""").fetchone()
    if check is None:
        return None
    if check['feature'] == 'outline' or check['feature'].startswith('outline:'):
        if state.execute("SELECT 1 FROM coverage WHERE status='pending' LIMIT 1").fetchone():
            return None
        if state.execute("""SELECT 1 FROM checks
            WHERE NOT (feature='outline' OR feature GLOB 'outline:*')
              AND (status!='complete' OR verdict IS NOT 'pass') LIMIT 1""").fetchone():
            return None
    return check


class InvocationOracle:
    READ_ONLY = {"ide_find_class", "ide_find_symbol", "ide_find_file", "ide_find_references",
                 "ide_find_implementations", "ide_type_hierarchy", "ide_search_text", "ide_call_hierarchy"}

    def __init__(self, client: Any, state: sqlite3.Connection, *, metrics=None,
                 max_entries=256, max_bytes=16 * 1024 * 1024):
        self.client, self.state = client, state
        self.metrics = metrics or Metrics(state)
        self.store = OracleStore(state, self.metrics)
        self.memory = ReplyCache(max_entries, max_bytes)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        reusable = tool in self.READ_ONLY and "cursor" not in arguments
        key = stable_id({"tool": tool, "arguments": arguments}) if reusable else None
        if reusable:
            cached = self.memory.get(key)
            if cached is not None:
                self.metrics.record('oracle.memory_hit')
                return cached
            row = self.state.execute("""SELECT r.* FROM oracle_cache c JOIN oracle_responses r
                ON r.id=c.response_id WHERE c.request_key=?""", (key,)).fetchone()
            if row is not None:
                started = time.perf_counter()
                cached = Reply(json.loads(row['response_json']), self.state, row['id'], tool,
                               row['request_json'], len(row['response_json'].encode()))
                self.metrics.record('oracle.disk_decode', time.perf_counter() - started)
                self.memory.put(key, cached)
                return cached
        try:
            response = self._network_call(tool, arguments)
        except McpRemoteError as error:
            self._capture_failure(tool, arguments, error)
            raise
        return self._capture_reply(tool, arguments, response, key)

    def _capture_failure(self, tool, arguments, error):
        # This runs on the SQLite-owning thread, never on a network worker.
        with self.state:
            self.store.capture_failure(tool, arguments, error)
            self.metrics.flush()

    def _network_call(self, tool, arguments):
        started = time.perf_counter()
        try:
            return self.client.call(tool, arguments)
        finally:
            self.metrics.record('mcp.tool.' + tool, time.perf_counter() - started)

    def _capture_reply(self, tool, arguments, response, key):
        # Usable cursor pages expire independently and cannot be reused. A
        # bounded prefix proof is different: paginated() rejects it on every
        # use, before following its cursor, and refines the query instead.
        with self.state:
            capture = self.store.capture(tool, arguments, response)
            field = {'ide_find_class': 'classes', 'ide_find_symbol': 'symbols', 'ide_find_file': 'files'}.get(tool)
            rows = capture.get(field) if isinstance(capture, Reply) and field else None
            bounded_prefix = (isinstance(rows, list) and len(rows) >= 500
                              and all(isinstance(row, dict) for row in rows)
                              and not (capture.get('hasMore') and not capture.get('nextCursor')))
            if key is not None and isinstance(capture, Reply) and not any(capture.get(flag) for flag in ('stale', 'truncated')) and \
                    (bounded_prefix or not any(capture.get(flag) for flag in ('hasMore', 'nextCursor'))):
                self.state.execute('INSERT OR REPLACE INTO oracle_cache VALUES (?,?)', (key, capture.response_id))
                self.memory.put(key, capture)
            self.metrics.flush()
        return capture if isinstance(capture, Reply) else response

    def prefetch(self, tool, requests, *, workers=4):
        """Network workers never touch SQLite; pages remain demand-recorded."""
        if tool not in self.READ_ONLY or not 1 <= workers <= 4:
            raise ToolError('prefetch requires a read-only tool and 1..4 workers')
        if getattr(self.client, 'parallel_safe', False) is not True:
            workers = 1
        started = time.perf_counter()

        def uncached():
            for arguments in requests:
                if 'cursor' in arguments:
                    raise ToolError('cursor pages cannot be prefetched or reused')
                key = stable_id({'tool': tool, 'arguments': arguments})
                if self.memory.get(key) is None and not self.state.execute('SELECT 1 FROM oracle_cache WHERE request_key=?', (key,)).fetchone():
                    yield key, arguments

        if workers == 1:
            for _, arguments in uncached():
                response = self.call(tool, arguments)
                if isinstance(response, dict) and any(response.get(flag) for flag in ('stale', 'truncated')):
                    raise Unsupported('MCP prefetch snapshot is stale or truncated')
        else:
            iterator = iter(uncached())
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures, pending_keys = {}, set()
                first_error = None

                def refill():
                    # Consume lazily, with at most `workers` requests retained.
                    # A slow peer never prevents an idle worker taking work.
                    nonlocal first_error
                    while len(futures) < workers:
                        try:
                            key, arguments = next(iterator)
                        except StopIteration:
                            break
                        except ToolError as error:
                            first_error = first_error or error
                            break
                        if key not in pending_keys:
                            futures[executor.submit(self._network_call, tool, arguments)] = key, arguments
                            pending_keys.add(key)

                refill()
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        key, arguments = futures.pop(future)
                        pending_keys.remove(key)
                        try:
                            network_reply = future.result()
                        except McpRemoteError as error:
                            self._capture_failure(tool, arguments, error)
                            first_error = first_error or error
                            continue
                        except (ToolError, OSError, subprocess.TimeoutExpired) as error:
                            first_error = first_error or error
                            continue
                        response = self._capture_reply(tool, arguments, network_reply, key)
                        if isinstance(response, dict) and any(response.get(flag) for flag in ('stale', 'truncated')):
                            first_error = first_error or Unsupported('MCP prefetch snapshot is stale or truncated')
                    # On an error, stop scheduling, but drain the bounded set
                    # already in flight and durably capture all its outcomes.
                    if first_error is None:
                        refill()
                if first_error is not None:
                    raise first_error
        self.metrics.record('oracle.prefetch_wall', time.perf_counter() - started)


INTERNAL_FEATURES = {'unused-symbols', 'version', 'list-roots', 'subtree:list', 'map'}

LIVE_FEATURES = rank_contracts.FEATURES | vcs_contracts.FEATURES | INTERNAL_FEATURES | lifecycle_contracts.FEATURES | root_contracts.FEATURES | install_contracts.FEATURES | delegate_contracts.FEATURES | {"api", "class", "class-qualified", "symbol", "file", "outline", "imports",
                 "search", "implementations", "hierarchy", "refs", "usages", "callers",
                 "stats", "query", "schema", "db-path", "outline:constructors", "search:files", "search:content", "annotations", "symbol:options", "class:options", "symbol:qualified-pattern", "class:qualified-pattern", "search:references", "search:ranking", "todo", "deprecated", "deeplinks", "suppress", "inject"}

SUPPRESSION_PATTERN = r'@(?:[\w$]+:)?(?:[\w$]+\.)*Suppress(?:Warnings)?\b'


def grep_locations(output: str, root: Path, header_pattern: str, limit: int):
    """Validate counts as well as locations; a JSON/text shape is not coverage."""
    lines = output.splitlines()
    header = re.fullmatch(header_pattern, lines[0]) if lines else None
    if not header or any(re.search(r'\.\.\. and \d+ more', line) for line in lines):
        raise Unsupported('unrecognized or incomplete grep output')
    items = [{'path': match[1], 'line': int(match[2])} for line in lines[1:]
             if (match := re.fullmatch(r'  (\S.*):(\d+)', line))]
    if len(items) != int(header[1]) or len(items) > limit:
        raise Unsupported('grep result count does not match rendered locations')
    return location_keys(items, root)


def coverage_sources(state: sqlite3.Connection) -> dict[str, int]:
    """Keep independent and hybrid checks out of pure MCP coverage totals."""
    prefixes = ('live MCP', 'hybrid MCP/JDK', 'hybrid MCP/source', 'independent JDK',
                'live CLI against database state', 'internal CLI', 'independent source/state')
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
                 *, schedule_followups: bool = True, batch_text: bool = False,
                 symbol_initials: set[str] | None = None):
        self.root, self.binary, self.state, self.client = root, binary, state, client
        client_metrics = getattr(client, 'metrics', None)
        self.metrics = client_metrics if isinstance(client_metrics, Metrics) else Metrics(state)
        self.oracle_store = OracleStore(state, self.metrics)
        self.database = database
        structure_digest = file_sha256(Path(__file__).with_name('JavaStructure.java'))
        old_structure = state.execute("SELECT value FROM metadata WHERE key='structure_sha256'").fetchone()
        if old_structure is None or old_structure[0] != structure_digest:
            with state:
                state.execute('DELETE FROM source_structures')
                state.execute("INSERT OR REPLACE INTO metadata VALUES ('structure_sha256',?)", (structure_digest,))
        self.schedule_followups = schedule_followups
        self._injection_ready = False
        self._inventory_ready = False
        self._java_dependency_results = None
        self._android_dependency_results = None
        self.batch_text = batch_text
        self._text_batch_unavailable = False
        self._text_snapshot = None
        self.symbol_initials = symbol_initials
        self._symbol_prefetch_ready = False
        self._lifecycle_results = None
        self._lifecycle_error = None
        self._root_results = None
        self._root_error = None
        self._install_results = None
        self._install_error = None
        self._android_results = None
        self._android_syntax_results = None
        self._java_resource_results = None
        self._context_results = None
        self._explore_results = None
        self._unused_dep_results = None
        self._graph_results = None
        self._receiver_results = None
        self._exception_scope_results = None
        self._local_interface_results = None
        self._pattern_scope_results = None
        self._type_access_results = None
        self._inherited_type_results = None
        self._parent_results = None
        self._type_binding_results = None
        self._format_results = None
        self._navigation_format_results = None
        self._file_view_results = None
        self._file_scope_results = None
        self._navigation_scope_results = None
        self._caller_scope_results = None
        self._caller_format_results = None
        self._search_format_results = None
        self._insight_scope_results = None
        self._analysis_scope_results = None
        self._exploration_format_results = None
        self._management_format_results = None
        self._lifecycle_format_results = None
        self._mutation_format_results = None
        self._project_format_results = None
        self._module_format_results = None
        self._module_scope_results = None
        self._module_root_results = None
        self._module_alias_results = None
        self._graph_root_results = None
        self._graph_directory_results = None
        self._graph_ambiguity_results = None
        self._vcs_results = None
        self._vcs_budget_results = None
        self._rank_results = None
        self.environment = {
            **os.environ, "AST_INDEX_DB_PATH": str(database),
            "AST_INDEX_ROOT": str(root),
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
        started = time.perf_counter()
        try:
            value = run_command([str(self.binary), "--format", "json", *arguments], self.root, self.environment)
        finally:
            self.metrics.record('cli.' + arguments[0], time.perf_counter() - started)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise Unsupported("command does not provide the expected JSON contract") from error
        return parsed

    def text_cli(self, *arguments: str) -> str:
        started = time.perf_counter()
        try:
            return run_command([str(self.binary), *arguments], self.root, self.environment)
        finally:
            self.metrics.record('cli.' + arguments[0], time.perf_counter() - started)

    def call_hierarchy_check(self, check: sqlite3.Row):
        try:
            return call_hierarchy_contracts.exercise(self, check)
        except call_hierarchy_contracts.UnsupportedHierarchy as error:
            raise Unsupported(str(error)) from error

    def graph_mcp_check(self, check: sqlite3.Row):
        try:
            return graph_mcp_contracts.exercise(self, check)
        except graph_mcp_contracts.ScopeUnsupported as error:
            raise Unsupported(str(error)) from error

    def paginated(self, check_id: str, tool: str, arguments: dict[str, Any], field: str) -> list[dict[str, Any]]:
        items = []
        cursors = set()
        # Several oracle operations (e.g. overloads) share one check. Preserve
        # every operation, in call order, for exact request-bound replay.
        page = self.state.execute("SELECT coalesce(max(page)+1,0) FROM oracle_pages WHERE check_id=?", (check_id,)).fetchone()[0]
        while True:
            response = self.client.call(tool, arguments)
            self.oracle_store.page(check_id, page, tool, arguments, response)
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
            if cursor and cursor in cursors:
                raise Unsupported("MCP returned a repeated cursor")
            # A broad navigation answer at the existing collection bound is
            # unusable as complete truth even when it has a cursor. Refine the
            # query before fetching pages that will be discarded anyway; large
            # real-project prefixes can lose their cursor during collection.
            if tool in {"ide_find_class", "ide_find_symbol", "ide_find_file"} and len(items) >= 500:
                raise SearchCollectionCap("search reached navigation collection cap; query needs partitioning")
            if not cursor:
                # Text at the collection bound is not complete truth either.
                if tool == "ide_search_text" and len(items) >= 5000:
                    raise SearchCollectionCap("text search reached server collection cap; query needs partitioning")
                if tool == "ide_find_references" and response.get("totalIsExact") is False:
                    raise Unsupported("MCP reference total is not exact")
                break
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
            # Outline used to prime this shared cache first. Delaying outline
            # must not serialize the same expensive searches in search/refs.
            # Prime only when another check actually needs symbol navigation.
            if (not self._symbol_prefetch_ready and self.symbol_initials and
                    isinstance(self.client, InvocationOracle) and
                    getattr(self.client.client, 'parallel_safe', False) is True):
                self.client.prefetch('ide_find_symbol', ({**arguments, 'query': initial}
                                                       for initial in sorted(self.symbol_initials)))
                self._symbol_prefetch_ready = True
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
        # Keep legal dollar/Unicode identifiers, but never ask Go-to-Symbol
        # about numeric literals: Java declaration names cannot start with digits.
        candidates = sorted({name for name in re.findall(r"(?<![\w$])[_$\w]+", code)
                             if not name[0].isdigit()})
        if isinstance(self.client, InvocationOracle) and getattr(self.client.client, 'parallel_safe', False) is True:
            self.client.prefetch('ide_find_symbol', ({
                'project_path': str(self.root), 'query': initial, 'language': 'Java',
                'scope': 'project_files', 'includeGenerated': False, 'pageSize': 500,
            } for initial in sorted({name[0] for name in candidates})))
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
        arguments = {
            'project_path': str(self.root), 'query': query, 'caseSensitive': True,
            'context': 'all', 'filePattern': '*.java', 'pageSize': 500,
        }
        if self.batch_text and not self._text_batch_unavailable and text_snapshot.TextSnapshot.eligible(query):
            try:
                if self._text_snapshot is None:
                    self._text_snapshot = text_snapshot.TextSnapshot(self.root, self.state, self.client, self.metrics)
                expected = self._text_snapshot.search(check['id'], query)
            except text_snapshot.SnapshotUnavailable:
                self._text_batch_unavailable = True
                self.metrics.record('oracle.text_snapshot_fallback')
                expected = self.oracle_text(check, arguments)
        else:
            expected = self.oracle_text(check, arguments)
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
                              if entry['kind'] != 'parent' and entry['name'] == name
                              and not entry.get('implicit', False))
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
            'deeplinks': r'[Dd]eep[Ll]ink|@DeepLink|DeepLinkHandler|@AppLink|NavDeepLink|android:scheme|openURL|application\([^)]*open:|handleOpen|CFBundleURLSchemes|UniversalLink|NSUserActivity',
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

    def suppression_check(self, check: sqlite3.Row):
        query = json.loads(check['subject'])['query']
        pattern = SUPPRESSION_PATTERN
        if query:
            literal = re.escape(query)
            pattern = f'(?:{pattern}).*(?i:{literal})|(?i:{literal}).*(?:{pattern})'
        expected = self.oracle_text(check, {
            'project_path': str(self.root), 'query': pattern, 'regex': True,
            'caseSensitive': True, 'context': 'all', 'filePattern': '*.java', 'pageSize': 500,
        })
        arguments = [] if query is None else [query]
        actual = self.text_cli('suppress', *arguments, '--limit', '1000000')
        locations = grep_locations(actual, self.root, r'@Suppress annotations \((\d+)\):', 1000000)
        if len(locations) >= 1000000:
            raise Unsupported('suppression result reached CLI collection limit')
        return expected, actual, location_keys(expected, self.root), locations

    def mobile_text_check(self, check: sqlite3.Row):
        """Execute lexical searches; absent languages are independent evidence."""
        if not self._inventory_ready:
            mobile_contracts.inventory(self.state, self.root)
            self._inventory_ready = True
        feature = check['feature']
        contracts = perl_contracts if feature in perl_contracts.EXTENSIONS else mobile_contracts
        query = json.loads(check['subject'])['query']
        status, reason = contracts.applicability(self.state, feature)
        if status == 'pending':
            raise Unsupported(reason)
        expected = set()
        for row in contracts.applicable_paths(self.state, feature):
            file = row['path']
            path = self.root / file
            if file_sha256(path) != row['sha256']:
                raise ToolError('lexical source changed after inventory')
            pattern = contracts.query_pattern(feature, query, row['extension'])
            # Partition by inventory file before querying, so every pagination
            # chain has an explicit language/root scope and the Java fallback
            # cannot silently erase Kotlin/Swift matches at the collection cap.
            matches = self.paginated(check['id'], 'ide_search_text', {
                'project_path': str(self.root), 'query': pattern, 'regex': True,
                'caseSensitive': True, 'context': 'all',
                'filePattern': '*' + row['extension'], 'paths': [file], 'pageSize': 500,
            }, 'matches')
            locations = set()
            for match in matches:
                line = match.get('line')
                if relative_path(match.get('file', match.get('path')), self.root) != file or not isinstance(line, int) or line < 1:
                    raise Unsupported('lexical text oracle returned an invalid scope/location')
                locations.add(line)
            # Only one file is retained at a time, and oracle columns on the
            # same line collapse into the CLI's line-oriented identity.
            with path.open(encoding='utf-8') as source:
                for number, line in enumerate(source, 1):
                    if number not in locations:
                        continue
                    locations.remove(number)
                    if not re.search(pattern, line):
                        raise Unsupported('lexical oracle anchor differs from source snapshot')
                    if contracts.accepts(feature, query, line):
                        expected.add((file, number))
                        if len(expected) >= 1000000:
                            raise Unsupported('lexical search exceeds bounded CLI collection limit')
            if locations:
                raise Unsupported('lexical oracle line exceeds source snapshot')
        ordered = sorted(expected)
        if len(ordered) >= 1000000:
            raise Unsupported('lexical search exceeds bounded CLI collection limit')
        arguments = [] if query is None else [query]
        outputs, expected_keys, actual_keys = {}, set(), set()
        for limit in sorted({0, 1, 3, 1000000}):
            output = self.text_cli(feature, *arguments, '--limit', str(limit))
            outputs[str(limit)] = output
            actual = contracts.output_locations(feature, output, self.root, limit)
            expected_keys.update((limit, index, *location) for index, location in enumerate(ordered[:limit]))
            actual_keys.update((limit, index, *location) for index, location in enumerate(actual))
        return {'source': reason, 'locations': ordered}, outputs, expected_keys, actual_keys

    def annotation_function_check(self, check: sqlite3.Row):
        if not self._inventory_ready:
            mobile_contracts.inventory(self.state, self.root)
            self._inventory_ready = True
        feature = check['feature']
        query = json.loads(check['subject'])['query']
        status, reason = annotation_contracts.applicability(self.state, feature, self.root)
        if status == 'pending':
            raise Unsupported(reason)
        expected = {}
        pattern = annotation_contracts.pattern(feature)
        for row in annotation_contracts.applicable_paths(self.state, feature):
            if row['size'] > annotation_contracts.MAX_SOURCE_BYTES:
                raise Unsupported('annotation source exceeds bounded parser size')
            path = self.root / row['path']
            fingerprint = file_sha256(path)
            stat = path.stat()
            if stat.st_size != row['size'] or stat.st_mtime_ns != row['modified'] or (row['sha256'] and fingerprint != row['sha256']):
                raise ToolError('annotation source changed after inventory')
            matches = self.paginated(check['id'], 'ide_search_text', {
                'project_path': str(self.root), 'query': pattern, 'regex': True,
                'caseSensitive': True, 'context': 'all',
                'filePattern': '*' + row['extension'], 'paths': [row['path']], 'pageSize': 500,
            }, 'matches')
            anchors = set()
            for match in matches:
                line = match.get('line')
                if relative_path(match.get('file', match.get('path')), self.root) != row['path'] or type(line) is not int or line < 1:
                    raise Unsupported('annotation oracle returned an invalid scope/location')
                anchors.add(line)
            remaining = anchors.copy()
            with path.open(encoding='utf-8') as source:
                for number, line in enumerate(source, 1):
                    if number in remaining:
                        if not re.search(pattern, line):
                            raise Unsupported('annotation oracle anchor differs from source')
                        remaining.remove(number)
            if remaining:
                raise Unsupported('annotation oracle line exceeds source')
            try:
                for entry in annotation_contracts.declarations(self, row):
                    if entry['annotation'] not in annotation_contracts.ANNOTATIONS[feature]:
                        continue
                    if entry['anchor'] not in anchors:
                        raise Unsupported('independent declaration has no MCP annotation anchor')
                    accepts = ((entry['return_type'] is not None and entry['return_type'].endswith(query))
                               if feature == 'provides' else
                               (not query or query.lower() in entry['name'].lower()))
                    if accepts:
                        line = entry['anchor'] if feature == 'provides' else entry['line']
                        identity = (row['path'], line, entry['declaration'])
                        expected[identity] = ((row['path'], line) if feature == 'provides' else
                                              (row['path'], line, entry['name']))
                        if len(expected) >= 1000000:
                            raise Unsupported('annotation functions exceed bounded CLI limit')
            except annotation_contracts.UnresolvedSyntax as error:
                raise Unsupported(str(error)) from error
            if file_sha256(path) != fingerprint:
                raise ToolError('annotation source changed during comparison')
        ordered = [expected[identity] for identity in sorted(expected)]
        arguments = [] if query is None else [query]
        outputs, expected_keys, actual_keys = {}, set(), set()
        if annotation_contracts.java_scope(self.state):
            # Native provides has no language filter. Compare every Java result
            # from the full page; foreign rows must not consume the Java limit.
            full_limit = 1000000
            output = self.text_cli(feature, *arguments, '--limit', str(full_limit))
            outputs[str(full_limit)] = output
            full = annotation_contracts.output_locations(feature, output, self.root, full_limit)
            if len(full) >= full_limit:
                raise Unsupported('mixed-language annotation result reached CLI collection limit')
            java = [entry for entry in full if Path(entry[0]).suffix == '.java']
            expected_keys.update(('java', index, *entry) for index, entry in enumerate(ordered))
            actual_keys.update(('java', index, *entry) for index, entry in enumerate(java))
            for limit in (0, 1, 3):
                output = self.text_cli(feature, *arguments, '--limit', str(limit))
                outputs[str(limit)] = output
                actual = annotation_contracts.output_locations(feature, output, self.root, limit)
                expected_keys.update(('native-prefix', limit, index, *entry)
                                     for index, entry in enumerate(full[:limit]))
                actual_keys.update(('native-prefix', limit, index, *entry)
                                   for index, entry in enumerate(actual))
            return {'source': reason, 'locations': ordered}, outputs, expected_keys, actual_keys
        for limit in (0, 1, 3, 1000000):
            output = self.text_cli(feature, *arguments, '--limit', str(limit))
            outputs[str(limit)] = output
            actual = annotation_contracts.output_locations(feature, output, self.root, limit)
            expected_keys.update((limit, index, *location) for index, location in enumerate(ordered[:limit]))
            actual_keys.update((limit, index, *location) for index, location in enumerate(actual))
        return {'source': reason, 'locations': ordered}, outputs, expected_keys, actual_keys

    def injection_check(self, check: sqlite3.Row):
        name = check['subject']
        if not self._injection_ready:
            # Sources are frozen for an audit/replay invocation and validated
            # again at its end. Rebuild derived truth for every new fixture;
            # an interrupted build is never a reusable completeness marker.
            with self.state:
                self.state.execute('DELETE FROM source_injection_targets')
            for path in java_files(self.root):
                file = path.relative_to(self.root).as_posix()
                entries = self.structure(file)
                with self.state:
                    self.state.executemany('INSERT OR IGNORE INTO source_injection_targets VALUES (?,?,?)',
                        ((target['name'], file, target['line']) for entry in entries
                         if entry['kind'] == 'annotation' for target in entry.get('injection_targets', [])))
            self._injection_ready = True
        expected = [{'file': row['path'], 'line': row['line']} for row in self.state.execute(
            'SELECT path,line FROM source_injection_targets WHERE name=? ORDER BY path,line', (name,))]
        actual = self.text_cli('inject', name, '--limit', '1000000')
        locations = grep_locations(actual, self.root, r"Injection points for '.+' \((\d+)\):", 1000000)
        if len(locations) >= 1000000:
            raise Unsupported('injection result reached CLI collection limit')
        return expected, actual, location_keys(expected, self.root), locations

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

    def analysis_management_check(self, check: sqlite3.Row):
        """Validate read-only internal contracts; these do not claim MCP equivalence."""
        feature = check['feature']
        if feature == 'version':
            manifest = Path(__file__).resolve().parents[2] / 'Cargo.toml'
            expected = 'ast-index v' + tomllib.loads(manifest.read_text())['package']['version']
            actual = self.text_cli('version').strip()
            return expected, actual, {expected}, {actual}
        source = connect(self.database, read_only=True)
        try:
            if feature in {'list-roots', 'subtree:list'}:
                expected = [dict(row) for row in source.execute(
                    'SELECT name,canonical_path,original_path FROM subtrees ORDER BY name')]
                actual = self.cli('list-roots') if feature == 'list-roots' else self.cli('subtree', 'list')
                if not isinstance(actual, list):
                    raise Unsupported('root listing has no array')
                return expected, actual, {(canonical_json(expected),)}, {(canonical_json(actual),)}
            # The command promises potentially unused *indexed* symbols. This
            # checks its selection and limits, not correctness of the index.
            rows = source.execute('''SELECT s.name,s.kind,s.line,f.path FROM symbols s
                JOIN files f ON f.id=s.file_id
                WHERE s.kind IN ('class','interface','function','object','enum','protocol','struct')
                AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.name=s.name)
                AND NOT EXISTS (SELECT 1 FROM xml_usages x WHERE x.class_name=s.name)
                AND NOT EXISTS (SELECT 1 FROM storyboard_usages b WHERE b.class_name=s.name)
                ORDER BY f.path,s.line''')
            expected = [dict(row) for row in rows]
            outputs, expected_keys, actual_keys = {}, set(), set()
            modes = [('full', [], expected),
                     ('exports', ['--export-only'], [item for item in expected if re.match('[A-Z]', item['name'])]),
                     ('absent-module', ['--module', '__audit_absent_module__'], [])]
            # Exercise a real path scope and every configured module name too.
            if expected:
                prefix = str(Path(expected[0]['path']).parent)
                prefix = '' if prefix == '.' else prefix + '/'
                modes.append(('path-module', ['--module', prefix],
                              [item for item in expected if item['path'].startswith(prefix)]))
            for row in source.execute('SELECT name,path FROM modules ORDER BY name'):
                if not row['path']:
                    continue
                prefix = row['path'].rstrip('/') + '/'
                modes.append(('named-module:' + row['name'], ['--module', row['name']],
                              [item for item in expected if item['path'].startswith(prefix)]))
            def identity(item):
                return item['name'], item['kind'], relative_path(item['path'], self.root), item['line']
            for mode, arguments, selected in modes:
                for limit in (1, 3, 1000000):
                    output = self.cli('unused-symbols', *arguments, '--limit', str(limit))
                    if not isinstance(output, list):
                        raise Unsupported('unused-symbols has no result array')
                    outputs[mode + ':' + str(limit)] = output
                    expected_keys.update((mode, limit, index, *identity(item)) for index, item in enumerate(selected[:limit]))
                    actual_keys.update((mode, limit, index, *identity(item)) for index, item in enumerate(output))
            return expected, outputs, expected_keys, actual_keys
        finally:
            source.close()

    def map_check(self, check: sqlite3.Row):
        """Check presentation against indexed declarations, not MCP equivalence.

        Stream DB rows and retain at most twenty symbols per directory. Inheritance
        belongs to a symbol ID, even when several files declare the same name.
        """
        module = json.loads(check['subject'])['module']
        source = connect(self.database, read_only=True)
        try:
            file_count = source.execute('SELECT count(*) FROM files').fetchone()[0]
            module_count = source.execute('SELECT count(*) FROM modules').fetchone()[0]
            depth = 3 if file_count > 5000 else 2

            attached = {row[0] for row in source.execute('SELECT canonical_path FROM subtrees')}

            def directory(path, owner):
                parts = path.split('/')
                prefix = '/'.join(parts[:min(depth, len(parts) - 1)])
                if attached:
                    root = Path(owner) if owner else self.root
                    if root != self.root and str(root) not in attached:
                        raise Unsupported('map contains an unregistered owning root')
                    return str(root / prefix).rstrip('/') + '/'
                return prefix + '/' if prefix else '.'

            counts, kinds, symbols = Counter(), {}, {}
            for row in source.execute('SELECT path,root_path FROM files'):
                if module is None or row['path'].startswith(module):
                    counts[directory(row['path'], row['root_path'])] += 1
            # Order candidates before retaining each group's bounded top slice.
            rows = source.execute('''SELECT s.id,s.name,s.kind,s.line,f.path,f.root_path FROM symbols s
                JOIN files f ON f.id=s.file_id WHERE s.parent_id IS NULL
                AND s.kind IN ('class','interface','struct','enum','object','protocol','trait','actor','package')
                ORDER BY CASE s.kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1
                    WHEN 'protocol' THEN 1 WHEN 'trait' THEN 1 WHEN 'struct' THEN 2
                    WHEN 'enum' THEN 3 WHEN 'object' THEN 4 WHEN 'actor' THEN 5 ELSE 10 END,
                    s.name COLLATE BINARY,f.path COLLATE BINARY,s.line,s.id''')
            for row in rows:
                if module is not None and not row['path'].startswith(module):
                    continue
                group = directory(row['path'], row['root_path'])
                kinds.setdefault(group, Counter())[row['kind']] += 1
                selected = symbols.setdefault(group, [])
                if len(selected) < 20:
                    parents = [entry[0] for entry in source.execute(
                        'SELECT DISTINCT parent_name FROM inheritance WHERE child_id=? ORDER BY parent_name COLLATE BINARY',
                        (row['id'],))]
                    item = {'name': row['name'], 'kind': row['kind'], 'file': Path(row['path']).name}
                    if parents:
                        item['parents'] = parents
                    selected.append(item)
            ordered = sorted(counts if module is None else symbols, key=lambda path: (-counts[path], path))
            outputs, expected_keys, actual_keys = {}, set(), set()
            for limit in sorted({0, 1, 3, len(ordered)}):
                for per_dir in ([None] if module is None else [0, 1, 5, 20]):
                    arguments = ['--limit', str(limit)]
                    if module is not None:
                        arguments += ['--module', module, '--per-dir', str(per_dir)]
                    expected = {'file_count': file_count, 'module_count': module_count, 'groups': []}
                    for path in ordered[:limit]:
                        group = {'path': path, 'file_count': counts[path]}
                        if module is None:
                            if kinds.get(path):
                                group['kinds'] = dict(kinds[path])
                        else:
                            group['symbols'] = symbols[path][:per_dir]
                        expected['groups'].append(group)
                    if module is None:
                        expected.update(showing=min(limit, len(ordered)), total_dirs=len(ordered))
                    label = source.execute("SELECT value FROM metadata WHERE key='project_label'").fetchone()
                    if label:
                        expected['project'] = label[0]
                    actual = self.cli('map', *arguments)
                    key = (limit, per_dir if per_dir is not None else -1)
                    outputs[str(key)] = actual
                    expected_keys.add((*key, canonical_json(expected)))
                    actual_keys.add((*key, canonical_json(actual)))
            return {'module': module, 'directories': len(ordered)}, outputs, expected_keys, actual_keys
        finally:
            source.close()

    def api_check(self, check: sqlite3.Row):
        """Compare public declarations with javac within indexed Java file scope.

        File scope comes from the native index; visibility does not. This is
        independent syntax coverage, never MCP navigation equivalence.
        """
        module = json.loads(check['subject'])['module']
        prefix = module.rstrip('/') + '/' if module not in ('', '.') else ''
        source = connect(self.database, read_only=True)
        expected = set()
        try:
            for row in source.execute("SELECT path FROM files WHERE path LIKE '%.java' ORDER BY path"):
                file = row['path']
                if file.startswith(prefix):
                    expected.update((file, entry['line']) for entry in self.structure(file)
                                    if entry.get('public_api'))
        finally:
            source.close()
        outputs, expected_keys, actual_keys = {}, set(), set()
        ordered = sorted(expected)
        modules = [module or '.']
        dotted = module.strip('/').replace('/', '.')
        if '/' in module.strip('/') and not (self.root / dotted).exists():
            modules.append(dotted)
        for mode, limit in ((mode, limit) for mode in range(len(modules)) for limit in (0, 1, 3, 1000000)):
            output = self.text_cli('api', modules[mode], '--limit', str(limit))
            lines = output.splitlines()
            header = re.fullmatch(r"Public API of '.+' \((\d+)\):", lines[0]) if lines else None
            if not header:
                raise Unsupported('unrecognized public API output')
            locations = []
            for line in lines[1:]:
                if line.startswith('    ') or not line.strip() or line.strip() == 'No public API found.':
                    continue
                match = re.fullmatch(r'  (.+):(\d+)', line)
                if not match or int(match[2]) < 1:
                    raise Unsupported('unrecognized public API location')
                locations.append((relative_path(match[1], self.root), int(match[2])))
            if len(locations) != int(header[1]) or len(locations) > limit or len(set(locations)) != len(locations):
                raise Unsupported('public API count differs from unique rendered locations')
            java_locations = [item for item in locations if item[0].endswith('.java')]
            # Java declarations are ordered and selected before foreign results.
            # Other languages remain outside the syntax comparison.
            outputs[f'{mode}:{limit}'] = java_locations
            expected_keys.update((mode, limit, index, *item) for index, item in enumerate(ordered[:limit]))
            actual_keys.update((mode, limit, index, *item) for index, item in enumerate(java_locations))
        return {'source': 'independent javac; native indexed Java file scope', 'locations': ordered}, outputs, expected_keys, actual_keys

    def lifecycle_check(self, check: sqlite3.Row):
        if self._lifecycle_error is not None:
            raise self._lifecycle_error
        if self._lifecycle_results is None:
            try:
                self._lifecycle_results = lifecycle_contracts.exercise(self.binary, self.database.parent / 'lifecycle-fixtures')
            except (ToolError, OSError, subprocess.TimeoutExpired) as error:
                self._lifecycle_error = error
                raise
        expected, actual = (section[check['feature']] for section in self._lifecycle_results)
        return {'source': lifecycle_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def root_check(self, check: sqlite3.Row):
        if self._root_error is not None:
            raise self._root_error
        if self._root_results is None:
            try:
                self._root_results = root_contracts.exercise(self.binary, self.database.parent / 'root-fixtures')
            except (ToolError, OSError, subprocess.TimeoutExpired) as error:
                self._root_error = error
                raise
        expected, actual = (section[check['feature']] for section in self._root_results)
        return {'source': root_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def install_check(self, check: sqlite3.Row):
        if self._install_error is not None:
            raise self._install_error
        if self._install_results is None:
            try:
                self._install_results = install_contracts.exercise(self.binary, self.database.parent)
            except (ToolError, OSError, subprocess.TimeoutExpired) as error:
                self._install_error = error
                raise
        expected, actual = (section[check['feature']] for section in self._install_results)
        return {'source': install_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def module_check(self, check: sqlite3.Row):
        if not self._inventory_ready:
            mobile_contracts.inventory(self.state, self.root)
            self._inventory_ready = True
        return module_contracts.verify(self, check['feature'])

    def profile_check(self, check: sqlite3.Row):
        if not self._inventory_ready:
            mobile_contracts.inventory(self.state, self.root)
            self._inventory_ready = True
        return profile_contracts.verify(self, check['feature'])

    def delegate_check(self, check: sqlite3.Row):
        expected, actual = (section[check['feature']] for section in
                            delegate_contracts.exercise(self.binary, self.database.parent))
        return {'source': delegate_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def route_check(self, check: sqlite3.Row):
        expected, actual = route_contracts.exercise(self.binary, self.database.parent, check['feature'])
        return {'source': route_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def android_check(self, check: sqlite3.Row):
        if check['subject'] == 'target-absence':
            expected, actual = android_contracts.verify_absence(self, check['feature'])
        else:
            if self._android_results is None:
                self._android_results = android_contracts.exercise(self.binary, self.database.parent)
            expected, actual = (section[check['feature']] for section in self._android_results)
        return {'source': android_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def android_syntax_check(self, check: sqlite3.Row):
        if self._android_syntax_results is None:
            self._android_syntax_results = android_syntax_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._android_syntax_results)
        return {'source': android_syntax_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def java_resource_check(self, check: sqlite3.Row):
        if self._java_resource_results is None:
            self._java_resource_results = java_resource_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._java_resource_results)
        return {'source': java_resource_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def context_check(self, check: sqlite3.Row):
        if self._context_results is None:
            self._context_results = context_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._context_results)
        return {'source': context_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def explore_budget_check(self, check: sqlite3.Row):
        if self._explore_results is None:
            self._explore_results = explore_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._explore_results)
        return {'source': explore_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def unused_dep_check(self, check: sqlite3.Row):
        if check['feature'] == 'unused-deps:target':
            return unused_dep_contracts.verify_target(self)
        if self._unused_dep_results is None:
            self._unused_dep_results = unused_dep_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._unused_dep_results)
        return {'source': unused_dep_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def java_dependency_check(self, check: sqlite3.Row):
        if self._java_dependency_results is None:
            self._java_dependency_results = java_dependency_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._java_dependency_results)
        return {'source': java_dependency_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def android_dependency_check(self, check: sqlite3.Row):
        if self._android_dependency_results is None:
            self._android_dependency_results = android_dependency_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._android_dependency_results)
        return {'source': android_dependency_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def graph_check(self, check: sqlite3.Row):
        if self._graph_results is None:
            self._graph_results = graph_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._graph_results)
        return {'source': graph_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def caller_scope_check(self, check: sqlite3.Row):
        if self._caller_scope_results is None:
            self._caller_scope_results = caller_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._caller_scope_results)
        return {'source': caller_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def receiver_check(self, check: sqlite3.Row):
        if self._receiver_results is None:
            self._receiver_results = java_receiver_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._receiver_results)
        return {'source': java_receiver_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def exception_scope_check(self, check: sqlite3.Row):
        if self._exception_scope_results is None:
            self._exception_scope_results = java_exception_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._exception_scope_results)
        return {'source': java_exception_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def local_interface_check(self, check: sqlite3.Row):
        if self._local_interface_results is None:
            self._local_interface_results = java_local_interface_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._local_interface_results)
        return {'source': java_local_interface_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def parent_check(self, check: sqlite3.Row):
        if self._parent_results is None:
            self._parent_results = java_parent_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._parent_results)
        return {'source': java_parent_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def inherited_type_check(self, check: sqlite3.Row):
        if self._inherited_type_results is None:
            self._inherited_type_results = java_inherited_type_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._inherited_type_results)
        return {'source': java_inherited_type_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def type_access_check(self, check: sqlite3.Row):
        if self._type_access_results is None:
            self._type_access_results = java_type_access_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._type_access_results)
        return {'source': java_type_access_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def pattern_scope_check(self, check: sqlite3.Row):
        if self._pattern_scope_results is None:
            self._pattern_scope_results = java_pattern_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._pattern_scope_results)
        return {'source': java_pattern_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def type_binding_check(self, check: sqlite3.Row):
        if self._type_binding_results is None:
            self._type_binding_results = java_type_binding_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._type_binding_results)
        return {'source': java_type_binding_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def format_check(self, check: sqlite3.Row):
        if self._format_results is None:
            self._format_results = format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._format_results)
        return {'source': format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def navigation_format_check(self, check: sqlite3.Row):
        if self._navigation_format_results is None:
            self._navigation_format_results = navigation_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._navigation_format_results)
        return {'source': navigation_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def caller_format_check(self, check: sqlite3.Row):
        if self._caller_format_results is None:
            self._caller_format_results = caller_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._caller_format_results)
        return {'source': caller_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def search_format_check(self, check: sqlite3.Row):
        if self._search_format_results is None:
            self._search_format_results = search_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._search_format_results)
        return {'source': search_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def insight_scope_check(self, check: sqlite3.Row):
        if self._insight_scope_results is None:
            self._insight_scope_results = insight_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._insight_scope_results)
        return {'source': insight_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def exploration_format_check(self, check: sqlite3.Row):
        if self._exploration_format_results is None:
            self._exploration_format_results = exploration_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._exploration_format_results)
        return {'source': exploration_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def analysis_scope_check(self, check: sqlite3.Row):
        if self._analysis_scope_results is None:
            self._analysis_scope_results = analysis_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._analysis_scope_results)
        return {'source': analysis_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def lifecycle_format_check(self, check: sqlite3.Row):
        if self._lifecycle_format_results is None:
            self._lifecycle_format_results = lifecycle_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._lifecycle_format_results)
        return {'source': lifecycle_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def management_format_check(self, check: sqlite3.Row):
        if self._management_format_results is None:
            self._management_format_results = management_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._management_format_results)
        return {'source': management_format_contracts.REASONS[check['feature']], 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def mutation_format_check(self, check: sqlite3.Row):
        if self._mutation_format_results is None:
            self._mutation_format_results = mutation_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._mutation_format_results)
        return {'source': mutation_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def project_format_check(self, check: sqlite3.Row):
        if self._project_format_results is None:
            self._project_format_results = project_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._project_format_results)
        return {'source': project_format_contracts.REASONS[check['feature']], 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def graph_root_check(self, check: sqlite3.Row):
        if self._graph_root_results is None:
            self._graph_root_results = graph_root_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._graph_root_results)
        return {'source': graph_root_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def graph_directory_check(self, check: sqlite3.Row):
        if self._graph_directory_results is None:
            self._graph_directory_results = graph_directory_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._graph_directory_results)
        return {'source': graph_directory_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def graph_ambiguity_check(self, check: sqlite3.Row):
        if self._graph_ambiguity_results is None:
            self._graph_ambiguity_results = graph_ambiguity_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._graph_ambiguity_results)
        return {'source': graph_ambiguity_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def module_root_check(self, check: sqlite3.Row):
        if self._module_root_results is None:
            self._module_root_results = module_root_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._module_root_results)
        return {'source': module_root_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def module_scope_check(self, check: sqlite3.Row):
        if self._module_scope_results is None:
            self._module_scope_results = module_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._module_scope_results)
        return {'source': module_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def module_alias_check(self, check: sqlite3.Row):
        if self._module_alias_results is None:
            self._module_alias_results = module_alias_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._module_alias_results)
        return {'source': module_alias_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def module_format_check(self, check: sqlite3.Row):
        if self._module_format_results is None:
            self._module_format_results = module_format_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._module_format_results)
        return {'source': module_format_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def file_view_check(self, check: sqlite3.Row):
        if self._file_view_results is None:
            self._file_view_results = file_view_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._file_view_results)
        return {'source': file_view_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def file_scope_check(self, check: sqlite3.Row):
        if self._file_scope_results is None:
            self._file_scope_results = file_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._file_scope_results)
        return {'source': file_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def navigation_scope_check(self, check: sqlite3.Row):
        if self._navigation_scope_results is None:
            self._navigation_scope_results = navigation_scope_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (section[check['feature']] for section in self._navigation_scope_results)
        return {'source': navigation_scope_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def stack_check(self, check: sqlite3.Row):
        expected, actual = stack_contracts.exercise(self.binary, self.database.parent)
        return {'source': stack_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def vcs_check(self, check: sqlite3.Row):
        if self._vcs_results is None:
            self._vcs_results = vcs_contracts.exercise(self.binary, self.database.parent)
        expected, actual = (dict(section[check['feature']]) for section in self._vcs_results)
        if check['feature'] == 'search:rank-history':
            if self._vcs_budget_results is None:
                self._vcs_budget_results = vcs_contracts.exclusion_budget(self.binary, self.database.parent)
            for output, section in zip((expected, actual), self._vcs_budget_results):
                output.update({'budget:' + key: value for key, value in section.items()})
        return {'source': vcs_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def rank_check(self, check: sqlite3.Row):
        if self._rank_results is None:
            expected, actual = rank_contracts.exercise(self.binary, self.database.parent)
            for output, section in zip((expected, actual), rank_contracts.budget(self.binary, self.database.parent)):
                output.update({'budget:' + key: value for key, value in section.items()})
            self._rank_results = expected, actual
        expected, actual = self._rank_results
        return {'source': rank_contracts.REASON, 'samples': expected}, actual, \
            {(key, canonical_json(value)) for key, value in expected.items()}, \
            {(key, canonical_json(value)) for key, value in actual.items()}

    def evaluate(self, check: sqlite3.Row) -> None:
        started = time.perf_counter()
        with self.metrics.checkpoint('checkpoint.start'):
            self.state.execute("DELETE FROM oracle_pages WHERE check_id=?", (check["id"],))
            self.state.execute("UPDATE checks SET status='running' WHERE id=?", (check["id"],))
        try:
            handler = {"class": self.class_check, "class-qualified": self.class_check, "symbol": self.symbol_check,
                       "file": self.file_check, "outline": self.outline_check, "outline:constructors": self.structure_check, "imports": self.imports_check,
                       "search": self.search_check, "search:files": self.search_files_check,
                       "symbol:options": self.option_check, "class:options": self.option_check,
                       "symbol:qualified-pattern": self.option_check, "class:qualified-pattern": self.option_check,
                       "todo": self.grep_check, "deprecated": self.grep_check, "deeplinks": self.grep_check,
                       "suppress": self.suppression_check, "inject": self.injection_check,
                       "search:references": self.search_aggregation_check, "search:ranking": self.search_ranking_check,
                       "search:content": self.text_search_check, "annotations": self.text_search_check, **dict.fromkeys(("implementations", "hierarchy", "refs", "usages", "callers"), self.semantic_check)}.get(check["feature"])
            if check["feature"] in {"stats", "query", "schema", "db-path"}:
                handler = self.introspection_check
            if check['feature'] in INTERNAL_FEATURES:
                handler = self.map_check if check['feature'] == 'map' else self.analysis_management_check
            if check['feature'] in lifecycle_contracts.FEATURES:
                handler = self.lifecycle_check
            if check['feature'] in root_contracts.FEATURES:
                handler = self.root_check
            if check['feature'] in install_contracts.FEATURES:
                handler = self.install_check
            if check['feature'] in delegate_contracts.FEATURES:
                handler = self.delegate_check
            if check['feature'] in module_contracts.FEATURES:
                handler = self.module_check
            if check['feature'] in profile_contracts.FEATURES:
                handler = self.profile_check
            if check['feature'] in route_contracts.FEATURES:
                handler = self.route_check
            if check['feature'] in vcs_contracts.FEATURES:
                handler = self.vcs_check
            if check['feature'] in rank_contracts.FEATURES:
                handler = self.rank_check
            if check['feature'] in context_contracts.FEATURES:
                handler = self.context_check
            if check['feature'] in explore_contracts.FEATURES:
                handler = self.explore_budget_check
            if check['feature'] in unused_dep_contracts.FEATURES | {'unused-deps:target'}:
                handler = self.unused_dep_check
            if check['feature'] in java_dependency_contracts.FEATURES:
                handler = self.java_dependency_check
            if check['feature'] in android_dependency_contracts.FEATURES:
                handler = self.android_dependency_check
            if check['feature'] in graph_contracts.FEATURES:
                handler = self.graph_check
            if check['feature'] in java_parent_contracts.FEATURES:
                handler = self.parent_check
            if check['feature'] in java_inherited_type_contracts.FEATURES:
                handler = self.inherited_type_check
            if check['feature'] in java_type_access_contracts.FEATURES:
                handler = self.type_access_check
            if check['feature'] in java_exception_scope_contracts.FEATURES:
                handler = self.exception_scope_check
            if check['feature'] in java_local_interface_contracts.FEATURES:
                handler = self.local_interface_check
            if check['feature'] in java_pattern_scope_contracts.FEATURES:
                handler = self.pattern_scope_check
            if check['feature'] in java_receiver_contracts.FEATURES:
                handler = self.receiver_check
            if check['feature'] in caller_scope_contracts.FEATURES:
                handler = self.caller_scope_check
            if check['feature'] in java_type_binding_contracts.FEATURES:
                handler = self.type_binding_check
            if check['feature'] in navigation_format_contracts.FEATURES:
                handler = self.navigation_format_check
            if check['feature'] in caller_format_contracts.FEATURES:
                handler = self.caller_format_check
            if check['feature'] in search_format_contracts.FEATURES:
                handler = self.search_format_check
            if check['feature'] in format_contracts.FEATURES:
                handler = self.format_check
            if check['feature'] in insight_scope_contracts.FEATURES:
                handler = self.insight_scope_check
            if check['feature'] in analysis_scope_contracts.FEATURES:
                handler = self.analysis_scope_check
            if check['feature'] in exploration_format_contracts.FEATURES:
                handler = self.exploration_format_check
            if check['feature'] in lifecycle_format_contracts.FEATURES:
                handler = self.lifecycle_format_check
            if check['feature'] in mutation_format_contracts.FEATURES:
                handler = self.mutation_format_check
            if check['feature'] in project_format_contracts.FEATURES:
                handler = self.project_format_check
            if check['feature'] in management_format_contracts.FEATURES:
                handler = self.management_format_check
            if check['feature'] in graph_root_contracts.FEATURES:
                handler = self.graph_root_check
            if check['feature'] in graph_directory_contracts.FEATURES:
                handler = self.graph_directory_check
            if check['feature'] in graph_ambiguity_contracts.FEATURES:
                handler = self.graph_ambiguity_check
            if check['feature'] in module_root_contracts.FEATURES:
                handler = self.module_root_check
            if check['feature'] in module_alias_contracts.FEATURES:
                handler = self.module_alias_check
            if check['feature'] in module_scope_contracts.FEATURES:
                handler = self.module_scope_check
            if check['feature'] in module_format_contracts.FEATURES:
                handler = self.module_format_check
            if check['feature'] in call_hierarchy_contracts.FEATURES:
                handler = self.call_hierarchy_check
            if check['feature'] in graph_mcp_contracts.FEATURES:
                handler = self.graph_mcp_check
            if check['feature'] in file_view_contracts.FEATURES:
                handler = self.file_view_check
            if check['feature'] in file_scope_contracts.FEATURES:
                handler = self.file_scope_check
            if check['feature'] in navigation_scope_contracts.FEATURES:
                handler = self.navigation_scope_check
            if check['feature'] in stack_contracts.FEATURES:
                handler = self.stack_check
            if check['feature'] in android_contracts.FEATURES:
                handler = self.android_check
            if check['feature'] in android_syntax_contracts.FEATURES:
                handler = self.android_syntax_check
            if check['feature'] in java_resource_contracts.FEATURES:
                handler = self.java_resource_check
            if check['feature'] == 'api':
                handler = self.api_check
            if check['feature'] in mobile_contracts.EXTENSIONS or check['feature'] in perl_contracts.EXTENSIONS:
                handler = self.mobile_text_check
            if check['feature'] in annotation_contracts.EXTENSIONS:
                handler = self.annotation_function_check
            if handler is None:
                raise Unsupported(f"no live handler for {check['feature']}")
            expected, actual, expected_keys, actual_keys = handler(check)
            missing_keys, unexpected_keys = expected_keys - actual_keys, actual_keys - expected_keys
            missing = sorted(missing_keys.elements() if isinstance(missing_keys, Counter) else missing_keys)
            unexpected = sorted(unexpected_keys.elements() if isinstance(unexpected_keys, Counter) else unexpected_keys)
            verdict = "fail" if missing or unexpected else "pass"
            with self.metrics.checkpoint('checkpoint.finish'):
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
            with self.metrics.checkpoint('checkpoint.finish'):
                self.state.execute(
                    "UPDATE checks SET status='complete',verdict=?,error=?,completed_at=? WHERE id=?",
                    (verdict, str(error), now_ms(), check["id"]),
                )
        finally:
            self.metrics.record('check.' + check['feature'], time.perf_counter() - started)


JAVA_EXCLUDED_FEATURES = (set(mobile_contracts.EXTENSIONS) | set(perl_contracts.EXTENSIONS)
                          | {'composables', 'previews', 'swiftui', 'async-funcs',
                             'storyboard-usages', 'asset-usages', 'deeplinks:non-java',
                             'suppress:non-java', 'inject:non-java'})


def required_features(help_text: str = '') -> set[str]:
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
    features.update({"global:format", "global:walk-up", "global:subtree", "global:local",
                     "global:scope-command-matrix", "search:rank-presets",
                     "module-route:budgets", "detect-stacks:composition-budgets",
                     "android:syntax-resolution", "xml-usages:target", "resource-usages:target"})
    features.update(LIVE_FEATURES)
    features.update(route_contracts.FEATURES)
    features.update(android_syntax_contracts.FEATURES)
    features.update(java_resource_contracts.FEATURES)
    features.update(context_contracts.PENDING)
    features.update(explore_contracts.FEATURES)
    features.update(graph_contracts.FEATURES)
    features.update(java_receiver_contracts.FEATURES)
    features.update(java_pattern_scope_contracts.FEATURES)
    features.update(java_exception_scope_contracts.FEATURES)
    features.update(java_local_interface_contracts.FEATURES)
    features.update(java_type_binding_contracts.FEATURES)
    features.update(java_type_access_contracts.FEATURES)
    features.update(java_inherited_type_contracts.FEATURES)
    features.update(unused_dep_contracts.FEATURES | unused_dep_contracts.PENDING.keys() | {'unused-deps:target'})
    features.update(java_dependency_contracts.FEATURES)
    features.update(android_dependency_contracts.FEATURES)
    features.update(format_contracts.FEATURES)
    features.update(navigation_format_contracts.FEATURES)
    features.update(file_view_contracts.FEATURES)
    features.update(file_scope_contracts.FEATURES)
    features.update(navigation_scope_contracts.FEATURES)
    features.update(caller_scope_contracts.FEATURES)
    features.update(caller_format_contracts.FEATURES)
    features.update(search_format_contracts.FEATURES)
    features.update(java_parent_contracts.FEATURES)
    features.update(insight_scope_contracts.FEATURES | insight_scope_contracts.PENDING.keys())
    features.update(analysis_scope_contracts.FEATURES)
    features.update(exploration_format_contracts.FEATURES)
    features.update(management_format_contracts.FEATURES)
    features.update(lifecycle_format_contracts.FEATURES)
    features.update(mutation_format_contracts.FEATURES)
    features.update(project_format_contracts.FEATURES)
    features.update(module_format_contracts.FEATURES)
    features.update(module_scope_contracts.FEATURES)
    features.update(module_root_contracts.FEATURES)
    features.update(module_alias_contracts.FEATURES)
    features.update(graph_root_contracts.FEATURES)
    features.update(graph_directory_contracts.FEATURES)
    features.update(graph_ambiguity_contracts.FEATURES)
    features.update(call_hierarchy_contracts.FEATURES)
    features.update(graph_mcp_contracts.FEATURES)
    return features


def plan(state: sqlite3.Connection, source_files: list[dict[str, Any]], help_text: str, candidates: Any = (), root: Path | None = None, *, java_only: bool = False) -> None:
    features = required_features(help_text)
    # A base navigation handler does not establish coverage of source bodies,
    # search ranking, or constructor/annotation entries omitted by Go-to-Symbol.
    pending_contracts = {
        "global:scope-command-matrix": "Combined path filters and Java API/module/map/analysis/graph/conventions/explore scope contracts remain unresolved; root fixture covers navigation and text searches",
        "deeplinks:non-java": "non-Java deeplink scopes require separate text/applicability contracts",
        "suppress:non-java": "Kotlin suppression scope requires a separate text/applicability contract",
        "inject:non-java": "Kotlin injection scope requires a separate syntax/applicability contract",
    }
    type_names = {Path(entry["path"]).stem for entry in source_files}
    candidate_names = set(candidates)
    annotation_names = set()
    suppression_queries = {None, '', '__audit_absent_suppression__'}
    if root is not None:
        for entry in source_files:
            content = (root / entry["path"]).read_text(encoding="utf-8")
            code = java_code_without_literals(content)
            annotation_names.update(re.findall(r"@([\w$]+)", code))
            type_names.update(re.findall(r"\b(?:class|interface|enum|record)\s+([\w$]+)", code))
            for line in content.splitlines():
                if re.search(SUPPRESSION_PATTERN, line):
                    for query in re.findall(r'"([^"\n]+)"', line):
                        suppression_queries.update((query, query.upper()))
    with state:
        state.execute("INSERT OR REPLACE INTO metadata VALUES ('audit_scope',?)",
                      ('java' if java_only else 'all',))
        for feature in sorted(features):
            state.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?)", (
                feature, "implemented" if feature in LIVE_FEATURES else "pending",
                (rank_contracts.REASON if feature in rank_contracts.FEATURES else
                 vcs_contracts.REASON if feature in vcs_contracts.FEATURES else
                 delegate_contracts.REASON if feature in delegate_contracts.FEATURES else
                 install_contracts.REASON if feature in install_contracts.FEATURES else
                 root_contracts.REASON if feature in root_contracts.FEATURES else
                 lifecycle_contracts.REASON if feature in lifecycle_contracts.FEATURES else
                 "internal CLI/DB read-only analysis and management contracts; not MCP equivalence" if feature in INTERNAL_FEATURES else
                 "independent JDK syntax against outline and indexed symbols" if feature == "outline:constructors" else
                 "independent JDK syntax: public API visibility and ordered limits within native indexed Java file scope; not MCP equivalence" if feature == "api" else
                 "live MCP text locations (Java deeplink scope)" if feature == "deeplinks" else
                 "live MCP text locations (Java suppression scope)" if feature == "suppress" else
                 "independent JDK syntax: injection declaration type locations (Java scope)" if feature == "inject" else
                 "live MCP text locations" if feature in {"annotations", "search:content", "todo", "deprecated"} else
                 "independent JDK syntax: qualified patterns and combined fuzzy/kind filters" if feature in {"symbol:qualified-pattern", "class:qualified-pattern"} else
                 "independent JDK syntax: patterns, filters, fuzzy lookup and source bodies" if feature in {"symbol:options", "class:options"} else
                 "internal CLI/DB reference aggregation and ordering; not MCP equivalence" if feature == "search:references" else
                 "internal CLI relevance tiers, limited-page stability and totals; not MCP equivalence" if feature == "search:ranking" else
                 "hybrid MCP/JDK declarations/semantic sites plus independent name-only lexical scope" if feature in {"refs", "usages", "callers"} else
                 "hybrid MCP/JDK child navigation and explicit source parent edges" if feature == "hierarchy" else
                 "live MCP code anchors rendered as import statements" if feature == "imports" else
                 "live CLI against database state" if feature in {"stats", "query", "schema", "db-path"} else
                 "live MCP navigation identity") if feature in LIVE_FEATURES else pending_contracts.get(feature, "comparison contract not implemented yet"),
            ))
        for feature, reason in pending_contracts.items():
            state.execute("INSERT OR IGNORE INTO coverage VALUES (?,'pending',?)", (feature, reason))
        for name in sorted(type_names):
            for feature in ("class", "class-qualified"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted(candidate_names):
            for feature in ("symbol", "search", "search:content", "search:references", "search:ranking"):
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({"feature": feature, "subject": name}), feature, name,
                ))
        for name in sorted(type_names | candidate_names):
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', (
                stable_id({'feature': 'inject', 'subject': name}), 'inject', name,
            ))
        for query in sorted(suppression_queries, key=lambda value: (value is not None, value or '')):
            subject = canonical_json({'query': query})
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', (
                stable_id({'feature': 'suppress', 'subject': subject}), 'suppress', subject,
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
        for module in [None, '', *sorted({str(Path(entry['path']).parent) + '/' for entry in source_files
                                        if str(Path(entry['path']).parent) != '.'})]:
            subject = canonical_json({'module': module})
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({'feature': 'map', 'subject': subject}), 'map', subject,
            ))
            if module is not None:
                state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                    stable_id({'feature': 'api', 'subject': subject}), 'api', subject,
                ))
        for feature in ("stats", "query", "schema", "db-path", "todo", "deprecated", "deeplinks", *sorted(INTERNAL_FEATURES - {'map'})):
            state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)", (
                stable_id({"feature": feature, "subject": "index-state"}), feature, "index-state",
            ))
        for feature in sorted(lifecycle_contracts.FEATURES | root_contracts.FEATURES | install_contracts.FEATURES | delegate_contracts.FEATURES):
            subject = 'disposable-fixture'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
    if java_only:
        if root is not None:
            mobile_contracts.inventory(state, root)
        with state:
            for feature in sorted(JAVA_EXCLUDED_FEATURES):
                state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                              (feature, 'out-of-scope', 'explicit Java-only repair scope; not checked and not passing'))
    else:
        mobile_contracts.plan_mobile(state, root)
        perl_contracts.plan_perl(state, root)
    annotation_contracts.plan_annotations(state, root)
    module_contracts.plan_modules(state, root)
    profile_contracts.plan_profiles(state, root)
    route_contracts.plan_routes(state, root)
    android_contracts.plan_android(state, root)
    android_syntax_contracts.plan_syntax(state, root)
    java_resource_contracts.plan_java_resources(state, root)
    vcs_contracts.plan_vcs(state, root)
    rank_contracts.plan_rank(state, root)
    stack_contracts.plan_stacks(state, root)
    context_contracts.plan_context(state, root)
    explore_contracts.plan_explore(state, root)
    graph_contracts.plan_graph(state, root)
    java_receiver_contracts.plan_receivers(state, root)
    java_exception_scope_contracts.plan_scopes(state, root)
    java_local_interface_contracts.plan_interfaces(state, root)
    java_pattern_scope_contracts.plan_scopes(state, root)
    java_type_binding_contracts.plan_types(state, root)
    java_type_access_contracts.plan_access(state, root)
    java_inherited_type_contracts.plan_types(state, root)
    unused_dep_contracts.plan_unused(state, root)
    java_dependency_contracts.plan_dependencies(state, root)
    android_dependency_contracts.plan_dependencies(state, root)
    format_contracts.plan_formats(state, root)
    file_view_contracts.plan_views(state, root)
    file_scope_contracts.plan_scope(state, root)
    navigation_scope_contracts.plan_scope(state, root)
    caller_scope_contracts.plan_scope(state, root)
    module_format_contracts.plan_formats(state, root)
    navigation_format_contracts.plan_formats(state, root)
    caller_format_contracts.plan_formats(state, root)
    search_format_contracts.plan_formats(state, root)
    exploration_format_contracts.plan_formats(state, root)
    java_parent_contracts.plan_parents(state, root)
    insight_scope_contracts.plan_scope(state, root)
    module_scope_contracts.plan_scope(state, root)
    graph_root_contracts.plan_scope(state, root)
    graph_directory_contracts.plan_scope(state, root)
    module_root_contracts.plan_scope(state, root)
    analysis_scope_contracts.plan_scope(state, root)
    management_format_contracts.plan_formats(state, root)
    lifecycle_format_contracts.plan_formats(state, root)
    mutation_format_contracts.plan_formats(state, root)
    project_format_contracts.plan_formats(state, root)
    graph_ambiguity_contracts.plan_contracts(state, root)
    module_alias_contracts.plan_aliases(state, root)


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
    inventory_hash = mobile_contracts.inventory_snapshot(root)
    if not source_files:
        raise Unsupported("target has no Java source files; language contract needed")
    binary, binary_hash = capture_binary(binary, output / 'binaries')
    contract = adapter_digest()
    text_mode = getattr(arguments, 'text_mode', 'batch')
    epoch = stable_id({"root": str(root), "snapshot": snapshot, "inventory": inventory_hash, "text_mode": text_mode,
                       "binary": binary_hash, "contract": contract})[:20]
    directory = output / epoch
    directory.mkdir(parents=True, exist_ok=True)
    binary = freeze_binary(binary, directory, binary_hash)
    state = connect(directory / "evidence.sqlite")
    try:
        state.executescript(SCHEMA)
        metadata = {
            "project_root": str(root), "snapshot_sha256": snapshot,
            "binary_sha256": binary_hash, "java_files": str(len(source_files)),
            "fixture_sha256": contract, "text_mode": text_mode,
        }
        with state:
            state.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", metadata.items())
            state.execute("UPDATE checks SET status='pending' WHERE status='running'")
            # Keep case evidence, not cached answers from an earlier IDE session.
            state.execute("DELETE FROM invocation_cache")
        url = arguments.mcp_url or discover_mcp_url(arguments.mcp_name)
        metrics = Metrics(state)
        client = StreamableHttpMcpClient(url, arguments.timeout, metrics=metrics)
        server = client.initialize()
        tools = client.tools()
        required = {"ide_find_class", "ide_find_symbol", "ide_find_file", "ide_find_references",
                    "ide_find_implementations", "ide_type_hierarchy", "ide_search_text", "ide_call_hierarchy"}
        missing = required - tools.keys()
        if missing:
            raise Unsupported("Index MCP Server lacks tools required by live contracts: " + ", ".join(sorted(missing)))
        if "ide_project_status" in tools:
            status = client.call("ide_project_status", {'project_path': str(root)})
            if not any(os.path.normpath(p.get("path", "")) == str(root) and p.get("open") for p in status.get("projects", [])):
                raise ToolError("target project is not open in Index MCP Server")
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('mcp_server',?)", (canonical_json(server),))
        database = directory / "index.sqlite"
        build_ast_index(str(binary), root, database, snapshot, 4, False)
        candidates = java_identifier_candidates(root)
        fixture = Fixture(root, binary, database, state, InvocationOracle(client, state, metrics=metrics),
                          batch_text=text_mode == 'batch', symbol_initials={name[0] for name in candidates})
        help_text = run_command([str(binary), "--help"], root, fixture.environment)
        plan(state, source_files, help_text, candidates, root, java_only=True)
        call_hierarchy_contracts.plan_methods(state, root, source_files, fixture.structure)
        graph_mcp_contracts.plan(state)
        limit = arguments.case_limit
        processed = 0
        problems = state.execute("SELECT count(*) FROM checks WHERE verdict IN ('fail','unsupported')").fetchone()[0]
        while problems < arguments.problem_limit and (limit is None or processed < limit):
            check = next_check(state)
            if check is None:
                break
            fixture.evaluate(check)
            processed += 1
            problems = state.execute("SELECT count(*) FROM checks WHERE verdict IN ('fail','unsupported')").fetchone()[0]
            if state.execute("SELECT verdict FROM checks WHERE id=?", (check["id"],)).fetchone()[0] == "error":
                break
        # Source changes invalidate evidence rather than manufacturing defects.
        if source_snapshot(root)[0] != snapshot or mobile_contracts.inventory_snapshot(root) != inventory_hash or file_sha256(binary) != binary_hash:
            raise ToolError("target sources or binary changed while scanning; evidence is invalid")
        counts = {row[0]: row[1] for row in state.execute(
            "SELECT verdict,count(*) FROM checks WHERE status='complete' GROUP BY verdict"
        )}
        remaining = state.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0]
        pending_features = state.execute("SELECT count(*) FROM coverage WHERE status='pending'").fetchone()[0]
        summary = {
            "java_files": len(source_files), "processed_this_run": processed,
            "counts": counts, "remaining_checks": remaining,
            "deferred_outline_checks": state.execute("""SELECT count(*) FROM checks
                WHERE status!='complete' AND (feature='outline' OR feature GLOB 'outline:*')""").fetchone()[0],
            "unimplemented_features": pending_features,
            "scope": "java",
            "out_of_scope_features": state.execute("SELECT count(*) FROM coverage WHERE status='out-of-scope'").fetchone()[0],
            "coverage_sources": coverage_sources(state),
            "inapplicable_features": state.execute("SELECT count(*) FROM coverage WHERE status='inapplicable'").fetchone()[0],
            "performance": metrics.summary(),
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
    parser.add_argument('--text-mode', choices=('batch', 'scalar'), default='batch',
                        help='MCP full-line acquisition or legacy per-name searches; native CLI checks stay unchanged')
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
