#!/usr/bin/env python3
"""Replay a stored problem batch through the live production CLI fixture."""
import argparse
import json
from pathlib import Path
import sys

from audit import Fixture, SCHEMA
from build_index import build_ast_index, freeze_binary
from common import ToolError, adapter_digest, canonical_json, connect, file_sha256, source_snapshot, stable_id


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


def problem_batch(source, limit: int):
    """Stream the bounded failure batch and every recorded contract error."""
    yield from source.execute(
        "SELECT * FROM checks WHERE verdict='fail' ORDER BY rowid LIMIT ?", (limit,))
    yield from source.execute(
        "SELECT * FROM checks WHERE verdict IN ('unsupported','error') ORDER BY rowid")


def replay(evidence: Path, root: Path, binary: Path, output: Path, limit: int = 100) -> dict:
    root, binary, output = root.resolve(), binary.resolve(), output.resolve()
    if output == root or root in output.parents:
        raise ToolError("replay artifacts must be outside the target project")
    source = connect(evidence, read_only=True)
    try:
        metadata = dict(source.execute("SELECT key,value FROM metadata"))
        snapshot, _ = source_snapshot(root)
        if str(root) != metadata.get("project_root") or snapshot != metadata.get("snapshot_sha256"):
            raise ToolError("replay target differs from the captured source snapshot")
        binary_hash = file_sha256(binary)
        epoch = stable_id({"evidence": str(evidence.resolve()), "snapshot": snapshot, "binary": binary_hash, "limit": limit,
                           "fixture": adapter_digest()})[:20]
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
                }.items())
                state.executemany("INSERT OR REPLACE INTO coverage VALUES (?,?,?)",
                                  source.execute("SELECT feature,status,reason FROM coverage"))
            for check in problem_batch(source, limit):
                existing = state.execute("SELECT status FROM checks WHERE id=?", (check["id"],)).fetchone()
                if existing and existing[0] == "complete":
                    continue
                with state:
                    state.execute("INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)", (check["id"], check["feature"], check["subject"]))
                oracle = StoredOracle(source, check["id"])
                fixture = Fixture(root, binary, database, state, oracle, schedule_followups=False)
                fixture.evaluate(check)
                try:
                    oracle.assert_consumed()
                except ToolError as error:
                    with state:
                        state.execute("UPDATE checks SET verdict='error',error=? WHERE id=?", (str(error), check["id"]))
            if source_snapshot(root)[0] != snapshot or file_sha256(binary) != binary_hash:
                raise ToolError("sources or binary changed during replay; verification is invalid")
            counts = {row[0]: row[1] for row in state.execute("SELECT verdict,count(*) FROM checks GROUP BY verdict")}
            remaining = state.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0]
            return {"counts": counts, "verified": bool(counts.get("pass")) and not remaining and not any(counts.get(key) for key in ("fail", "unsupported", "error")),
                    "verification": str(directory / "verification.sqlite")}
        finally:
            state.close()
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--ast-index", default="target/release/ast-index", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("limit must be positive")
    try:
        result = replay(args.evidence, args.project_root, args.ast_index, args.output_dir, args.limit)
        print(canonical_json(result))
        return 0 if result["verified"] else 1
    except (ToolError, OSError) as error:
        print(f"replay failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
