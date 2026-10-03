#!/usr/bin/env python3
"""Replay a stored problem batch through the live production CLI fixture."""
import argparse
import json
from pathlib import Path
import sys

from audit import Fixture, InvocationOracle, SCHEMA
from build_index import build_ast_index, capture_binary, freeze_binary
from common import StreamableHttpMcpClient, ToolError, adapter_digest, canonical_json, connect, file_sha256, source_snapshot, stable_id
import mobile_contracts
import text_snapshot


class StoredOracle:
    def __init__(self, source, check_id: str):
        self.source, self.check_id, self.page = source, check_id, 0
        self.has_tool = any(row[1] == "tool" for row in source.execute("PRAGMA table_info(pages)"))
        feature = source.execute("SELECT feature FROM checks WHERE id=?", (check_id,)).fetchone()[0]
        self.legacy_tool = {"class": "ide_find_class", "class-qualified": "ide_find_class",
                            "symbol": "ide_find_symbol", "file": "ide_find_file"}.get(feature)

    def call(self, tool: str, arguments: dict):
        row = self.source.execute("SELECT * FROM pages WHERE check_id=? AND page=?", (self.check_id, self.page)).fetchone()
        if row is None:
            raise ToolError("recorded oracle page is missing")
        if tool != (row["tool"] if self.has_tool else self.legacy_tool):
            raise ToolError("replay tool differs from the recorded oracle operation")
        if json.loads(row["request_json"]) != arguments:
            raise ToolError("replay request differs from the recorded oracle scope/query")
        self.page += 1
        return json.loads(row["response_json"])

    def assert_consumed(self):
        if self.source.execute("SELECT 1 FROM pages WHERE check_id=? AND page>=? LIMIT 1",
                               (self.check_id, self.page)).fetchone():
            raise ToolError("recorded oracle operations were not all replayed")



class ArchiveOracle:
    """Reuse supplemental oracle operations only at their exact captured request."""
    def __init__(self, source, check_id, live=None):
        self.source, self.check_id, self.live = source, check_id, live

    def call(self, tool, arguments):
        row = self.source.execute(
            "SELECT response_json FROM pages WHERE check_id=? AND tool=? AND request_json=? ORDER BY page LIMIT 1",
            (self.check_id, tool, canonical_json(arguments)),
        ).fetchone()
        if row is not None:
            return json.loads(row[0])
        if self.live is None:
            raise ToolError("supplemental oracle operation is missing; live oracle required")
        return self.live.call(tool, arguments)


def problem_batch(source, limit: int):
    """Stream the bounded failure batch and every recorded contract error."""
    yield from source.execute(
        "SELECT * FROM checks WHERE verdict='fail' ORDER BY rowid LIMIT ?", (limit,))
    yield from source.execute(
        "SELECT * FROM checks WHERE verdict IN ('unsupported','error') ORDER BY rowid")


def replay(evidence: Path, root: Path, binary: Path, output: Path, limit: int = 100, *, mcp_url: str | None = None, oracle_evidence: Path | None = None) -> dict:
    root, binary, output = root.resolve(), binary.resolve(), output.resolve()
    if output == root or root in output.parents:
        raise ToolError("replay artifacts must be outside the target project")
    source = connect(evidence, read_only=True)
    archive = None
    try:
        metadata = dict(source.execute("SELECT key,value FROM metadata"))
        snapshot, _ = source_snapshot(root)
        inventory_hash = mobile_contracts.inventory_snapshot(root)
        if str(root) != metadata.get("project_root") or snapshot != metadata.get("snapshot_sha256"):
            raise ToolError("replay target differs from the captured source snapshot")
        if metadata.get('inventory_sha256', inventory_hash) != inventory_hash:
            raise ToolError('replay file-type inventory differs from captured evidence')
        if oracle_evidence is not None:
            archive = connect(oracle_evidence, read_only=True)
            archived_metadata = dict(archive.execute("SELECT key,value FROM metadata"))
            if archived_metadata.get('project_root') != str(root) or archived_metadata.get('snapshot_sha256') != snapshot:
                raise ToolError('supplemental oracle target differs from the captured source snapshot')
            if archived_metadata.get('inventory_sha256', inventory_hash) != inventory_hash:
                raise ToolError('supplemental oracle file-type inventory differs from target')
        binary, binary_hash = capture_binary(binary, output / 'binaries')
        epoch = stable_id({"evidence": str(evidence.resolve()), "snapshot": snapshot, "inventory": inventory_hash, "binary": binary_hash, "limit": limit,
                           "fixture": adapter_digest(), "mcp_url": mcp_url,
                           "oracle_evidence": str(oracle_evidence.resolve()) if oracle_evidence else None})[:20]
        directory = output / epoch
        binary = freeze_binary(binary, directory, binary_hash)
        database = directory / "index.sqlite"
        build_ast_index(str(binary), root, database, snapshot)
        state = connect(directory / "verification.sqlite")
        try:
            state.executescript(SCHEMA)
            with state:
                state.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", {
                    "project_root": str(root), "snapshot_sha256": snapshot,
                    "binary_sha256": binary_hash, "original_evidence": str(evidence.resolve()),
                    "fixture_sha256": adapter_digest(),
                    "inventory_sha256": inventory_hash,
                    "audit_scope": metadata.get('audit_scope', 'all'),
                }.items())
                state.executemany("INSERT OR REPLACE INTO coverage VALUES (?,?,?)",
                                  source.execute("SELECT feature,status,reason FROM coverage"))
            live = None
            if mcp_url:
                client = StreamableHttpMcpClient(mcp_url)
                client.initialize()
                live = InvocationOracle(client, state)
                with state:
                    state.execute("DELETE FROM invocation_cache")
            if oracle_evidence is not None:
                with state:
                    state.execute("INSERT OR REPLACE INTO metadata VALUES ('supplemental_oracle_evidence',?)", (str(oracle_evidence.resolve()),))
            refreshed = 0
            snapshot_copied = False
            fixture = None
            has_text_snapshot = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_snapshot_dependencies'").fetchone()
            for check in problem_batch(source, limit):
                existing = state.execute("SELECT status FROM checks WHERE id=?", (check["id"],)).fetchone()
                if existing and existing[0] == "complete":
                    continue
                with state:
                    state.execute("INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)", (check["id"], check["feature"], check["subject"]))
                oracle = StoredOracle(source, check["id"])
                batch_text = bool(has_text_snapshot and source.execute('SELECT 1 FROM text_snapshot_dependencies WHERE check_id=?', (check['id'],)).fetchone())
                if batch_text and not snapshot_copied:
                    text_snapshot.copy_snapshot(source, state, root)
                    snapshot_copied = True
                if fixture is None:
                    fixture = Fixture(root, binary, database, state, oracle, schedule_followups=False,
                                      batch_text=batch_text)
                else:
                    # One immutable source/index session, not one full-project
                    # fingerprint validation per case. Keep each recorded
                    # request stream separate; final source guards remain below.
                    fixture.client = oracle
                    fixture.batch_text = batch_text
                    if fixture._text_snapshot is not None:
                        fixture._text_snapshot.client = oracle
                fixture.evaluate(check)
                try:
                    oracle.assert_consumed()
                except ToolError as error:
                    with state:
                        state.execute("UPDATE checks SET verdict='error',error=? WHERE id=?", (str(error), check["id"]))
                outcome = state.execute("SELECT verdict FROM checks WHERE id=?", (check['id'],)).fetchone()[0]
                if (live is not None or archive is not None) and outcome in {'unsupported', 'error'}:
                    # New normalization may need narrower queries or references
                    # beyond a formerly unsupported response. Recollect the same
                    # feature/subject live; retain the original evidence unchanged.
                    refreshed_oracle = ArchiveOracle(archive, check['id'], live) if archive is not None else live
                    Fixture(root, binary, database, state, refreshed_oracle, schedule_followups=False).evaluate(check)
                    refreshed += 1
            if source_snapshot(root)[0] != snapshot or mobile_contracts.inventory_snapshot(root) != inventory_hash or file_sha256(binary) != binary_hash:
                raise ToolError("sources or binary changed during replay; verification is invalid")
            counts = {row[0]: row[1] for row in state.execute("SELECT verdict,count(*) FROM checks GROUP BY verdict")}
            remaining = state.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0]
            return {"counts": counts, "refreshed_checks": refreshed, "verified": bool(counts.get("pass")) and not remaining and not any(counts.get(key) for key in ("fail", "unsupported", "error")),
                    "verification": str(directory / "verification.sqlite")}
        finally:
            state.close()
    finally:
        if archive is not None:
            archive.close()
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--ast-index", default="target/release/ast-index", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--oracle-evidence", type=Path, help="Supplemental captured operations from the identical target snapshot")
    parser.add_argument("--mcp-url", help="Read-only oracle for checks requiring new partition/normalization requests")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("limit must be positive")
    try:
        result = replay(args.evidence, args.project_root, args.ast_index, args.output_dir, args.limit, mcp_url=args.mcp_url, oracle_evidence=args.oracle_evidence)
        print(canonical_json(result))
        return 0 if result["verified"] else 1
    except (ToolError, OSError) as error:
        print(f"replay failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
