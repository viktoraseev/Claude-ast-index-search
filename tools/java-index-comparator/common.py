#!/usr/bin/env python3
"""Shared primitives for the autonomous Java index collector/comparator."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Iterator
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


SCHEMA_VERSION = 1


class ToolError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def adapter_digest() -> str:
    """Invalidate checkpoints when execution or normalization code changes."""
    directory = Path(__file__).parent
    return stable_id({name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                      for name in ("audit.py", "common.py", "build_index.py", "replay.py", "java_structure.py", "JavaStructure.java")})


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def java_files(project_root: Path) -> Iterator[Path]:
    ignored = {".arc", ".git", ".gradle", ".idea", "build", "target"}
    for directory, names, files in os.walk(project_root, followlinks=False):
        names[:] = sorted(name for name in names if name not in ignored)
        for name in sorted(files):
            if name.endswith(".java"):
                yield Path(directory, name)


def source_snapshot(project_root: Path) -> tuple[str, list[dict[str, Any]]]:
    digest = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    for path in java_files(project_root):
        relative = path.relative_to(project_root).as_posix()
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                file_digest.update(chunk)
        item = {"path": relative, "size": path.stat().st_size, "sha256": file_digest.hexdigest()}
        entries.append(item)
        digest.update(canonical_json(item).encode())
        digest.update(b"\n")
    return digest.hexdigest(), entries


def java_code_without_literals(text: str) -> str:
    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            result.append("\n")
            index = newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                result.extend("\n" for character in text[index:] if character == "\n")
                break
            result.extend("\n" for character in text[index:end + 2] if character == "\n")
            index = end + 2
        elif text.startswith('\"\"\"', index):
            end = text.find('\"\"\"', index + 3)
            if end < 0:
                result.extend("\n" for character in text[index:] if character == "\n")
                break
            result.extend("\n" for character in text[index:end + 3] if character == "\n")
            index = end + 3
        elif text[index] in {'\"', "'"}:
            quote = text[index]
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                elif text[index] == quote:
                    index += 1
                    break
                else:
                    if text[index] == "\n":
                        result.append("\n")
                    index += 1
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def java_identifier_candidates(project_root: Path) -> set[str]:
    # This is deliberately an over-approximation. Candidates only partition
    # MCP queries; they never become oracle declarations. Comments and string
    # literals are skipped because their prose otherwise dominates the trie.
    pattern = re.compile(r"(?<![\w$])([_$\w]+)", re.UNICODE)
    result: set[str] = set()
    for path in java_files(project_root):
        text = java_code_without_literals(path.read_text(encoding="utf-8", errors="replace"))
        for match in pattern.finditer(text):
            value = match.group(1)
            if value and (value[0] in "_$" or value[0].isalpha()):
                result.add(value)
    return result


def discover_mcp_url(server_name: str, codex_binary: str = "codex") -> str:
    completed = subprocess.run(
        [codex_binary, "mcp", "get", server_name, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolError(f"codex mcp get failed: {completed.stderr.strip()}")
    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ToolError("codex mcp get did not return JSON") from error
    transport = config.get("transport", {})
    if transport.get("type") != "streamable_http" or not transport.get("url"):
        raise ToolError(f"MCP {server_name!r} is not a streamable_http server")
    if not config.get("enabled", False):
        raise ToolError(f"MCP {server_name!r} is disabled")
    return str(transport["url"])


class StreamableHttpMcpClient:
    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url
        self.timeout = timeout
        self.next_id = 1
        self.session_id: str | None = None

    def _decode(self, body: bytes, content_type: str) -> Any:
        text = body.decode("utf-8")
        if "text/event-stream" in content_type:
            payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            if not payloads:
                raise ToolError("MCP SSE response contained no data event")
            text = payloads[-1]
        return json.loads(text) if text.strip() else None

    def send(self, method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        request_id = None
        if not notification:
            request_id = self.next_id
            self.next_id += 1
            message["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        http_request = urllib_request.Request(
            self.url,
            data=canonical_json(message).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(http_request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                decoded = self._decode(response.read(), response.headers.get("Content-Type", ""))
        except (HTTPError, URLError, TimeoutError) as error:
            raise ToolError(f"MCP HTTP request failed for {method}: {error}") from error
        if notification:
            return None
        if not isinstance(decoded, dict):
            raise ToolError(f"MCP returned an invalid response for {method}")
        if decoded.get("id") != request_id:
            raise ToolError(f"MCP response id mismatch for {method}")
        if "error" in decoded:
            raise ToolError(f"MCP error for {method}: {decoded['error']}")
        return decoded.get("result")

    def initialize(self) -> dict[str, Any]:
        result = self.send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "java-index-collector", "version": "1"},
            },
        )
        self.send("notifications/initialized", notification=True)
        return result

    def tools(self) -> dict[str, dict[str, Any]]:
        result = self.send("tools/list")
        return {tool["name"]: tool for tool in result.get("tools", [])}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.send("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            # Error content can contain project source; never send it to logs.
            raise ToolError(f"MCP tool {name} reported an error")
        texts = [part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"]
        if not texts:
            raise ToolError(f"MCP tool {name} returned no text")
        text = "\n".join(texts)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}


def now_ms() -> int:
    return time.time_ns() // 1_000_000
