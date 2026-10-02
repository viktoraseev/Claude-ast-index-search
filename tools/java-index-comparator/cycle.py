#!/usr/bin/env python3
"""Run resumable audit/fix/test/commit rounds, keeping agent output on disk."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from audit import scan
from common import ToolError, canonical_json, connect, now_ms, source_snapshot
from replay import replay


SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rounds(
 id INTEGER PRIMARY KEY,phase TEXT NOT NULL,base_head TEXT NOT NULL,
 summary_json TEXT,commit_head TEXT,error TEXT,created_at INTEGER NOT NULL
);
"""


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True)
    if result.returncode:
        raise ToolError(f"git {arguments[0]} failed; no force or destructive retry will be attempted")
    return result.stdout.strip()


def changed_files(repository: Path) -> list[str]:
    result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repository, capture_output=True, check=True)
    entries = result.stdout.decode().split("\0")
    paths = []
    for entry in entries:
        if not entry:
            continue
        if entry[:2] != "??" and ("R" in entry[:2] or "C" in entry[:2]):
            raise ToolError("renames require explicit review before automatic staging")
        path = entry[3:]
        # Local task instructions can contain private project paths.
        if path != "AGENTS.md":
            paths.append(path)
    return paths


def logged(command: list[str], repository: Path, directory: Path, name: str,
           *, prompt: str | None = None, timeout: float | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.stdout.log").open("ab") as stdout, (directory / f"{name}.stderr.log").open("ab") as stderr:
        result = subprocess.run(command, cwd=repository, input=prompt.encode() if prompt is not None else None,
                                stdout=stdout, stderr=stderr, timeout=timeout)
    if result.returncode:
        raise ToolError(f"{name} failed ({result.returncode}); see round logs, payloads were not printed")


def agent_prompt(summary: dict, root: Path) -> str:
    return f"""Read AGENTS.md and repository contributor rules. This is one round of an
automated differential repair, not permission to redefine the goal.
Target is read-only: {root}. Evidence SQLite: {summary['evidence']}.
Read the checks table using a streaming cursor: up to the first 100 verdict=fail
rows, plus unsupported/error and pending coverage contracts. Group by cause;
do not generate one test per row. Add compact, public-safe regression fixtures
that exercise actual production behaviour. Demonstrate failure before the fix,
then repair ast-index. If a verdict is a normalization/scope/pagination bug,
repair the comparator and add a test; never mask a production defect.
Support missing audit contracts rather than marking them covered or skipping
them. All applicable ast-index features remain in scope, not only class/symbol.
Do not modify the target project. Do not commit target source, evidence databases,
private names or large fixtures. Do not change AGENTS.md. Keep project payloads
out of your messages. Do not commit or push: the driver verifies and commits.
Do not install plugins/hooks/MCP configuration or write outside this repository
and its artifact directory. Project content belongs only in private artifacts;
public regression snippets must be small and synthetic.
Run relevant tests. Finish with a concise cause/fix/test summary.
"""


def set_phase(state: sqlite3.Connection, round_id: int, phase: str, **values: str) -> None:
    assignments = ["phase=?", "error=NULL", *(f"{key}=?" for key in values)]
    with state:
        state.execute(f"UPDATE rounds SET {','.join(assignments)} WHERE id=?", (phase, *values.values(), round_id))


def create_or_find_pr(repository: Path, target: str, branch: str, directory: Path) -> str:
    remote = git(repository, "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/]+)/[^/]+(?:\.git)?$", remote)
    if not match:
        raise ToolError("cannot determine GitHub fork owner from origin")
    head = f"{match.group(1)}:{branch}"

    def find():
        result = subprocess.run(["gh", "pr", "list", "--repo", target, "--head", head,
                                 "--base", "main", "--state", "open", "--json", "url,headRefOid"],
                                cwd=repository, capture_output=True, text=True)
        if result.returncode:
            raise ToolError("cannot query upstream PRs; check GitHub authentication")
        rows = json.loads(result.stdout)
        if len(rows) > 1:
            raise ToolError("multiple upstream PRs match this branch")
        if rows and rows[0]["headRefOid"] != git(repository, "rev-parse", "HEAD"):
            raise ToolError("upstream PR is not at the verified commit")
        return rows[0]["url"] if rows else None

    existing = find()
    if existing:
        return existing
    body = ("Adds compact Java regression tests and fixes verified by automated "
            "differential checks against Index MCP Server.\n\n"
            "The repair cycle rebuilds the native index, replays each original "
            "problem batch, runs release workspace tests, and restarts the full "
            "audit. Target source and evidence databases are excluded from Git.")
    logged(["gh", "pr", "create", "--repo", target, "--base", "main", "--head", head,
            "--title", "Fix Java indexing with automated MCP differential checks", "--body", body],
           repository, directory, "create-pr")
    result = find()
    if not result:
        raise ToolError("PR creation did not produce a verifiable upstream PR")
    return result


def seed_summary(evidence: Path, root: Path, binary: Path) -> dict:
    source = connect(evidence.resolve(), read_only=True)
    try:
        metadata = dict(source.execute("SELECT key,value FROM metadata"))
        if metadata.get("project_root") != str(root) or metadata.get("snapshot_sha256") != source_snapshot(root)[0]:
            raise ToolError("seed evidence belongs to a different target or source snapshot")
        if metadata.get("binary_sha256") != hashlib.sha256(binary.read_bytes()).hexdigest():
            raise ToolError("seed evidence belongs to a different native binary")
        counts = dict(source.execute("SELECT verdict,count(*) FROM checks WHERE status='complete' GROUP BY verdict"))
        if not counts.get("fail") and not counts.get("unsupported"):
            raise ToolError("seed evidence has no recorded repair work")
        return {"evidence": str(evidence.resolve()), "counts": counts, "complete": False,
                "remaining_checks": source.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0],
                "unimplemented_features": source.execute("SELECT count(*) FROM coverage WHERE status='pending'").fetchone()[0]}
    finally:
        source.close()


def run(arguments: argparse.Namespace) -> int:
    repository = Path(__file__).resolve().parents[2]
    root = Path(arguments.project_root).expanduser().resolve()
    output = Path(arguments.output_dir).expanduser().resolve()
    if output == root or root in output.parents:
        raise ToolError("artifacts must be outside the target project")
    branch = git(repository, "branch", "--show-current")
    if not branch or branch in {"main", "master"}:
        raise ToolError("select a feature branch before running automatic commits")
    agent_command = json.loads(arguments.agent_command)
    if not isinstance(agent_command, list) or not agent_command or any(not isinstance(value, str) for value in agent_command):
        raise ToolError("agent-command must be a nonempty JSON array of strings")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "cycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ToolError("another repair cycle is already running") from error
        state = connect(output / "cycle.sqlite")
        try:
            state.executescript(SCHEMA)
            identity = canonical_json({"root": str(root), "repository": str(repository), "branch": branch})
            previous = state.execute("SELECT value FROM configuration WHERE key='identity'").fetchone()
            if previous and previous[0] != identity:
                raise ToolError("artifact directory belongs to a different target or branch")
            with state:
                state.execute("INSERT OR REPLACE INTO configuration VALUES ('identity',?)", (identity,))
            completed = 0
            while arguments.max_rounds is None or completed < arguments.max_rounds:
                row = state.execute("SELECT * FROM rounds WHERE phase!='done' ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    if changed_files(repository):
                        raise ToolError("commit or preserve existing work before starting a new automated round")
                    with state:
                        initial = None
                        if getattr(arguments, "seed_evidence", None) and not state.execute("SELECT 1 FROM rounds LIMIT 1").fetchone():
                            initial = seed_summary(arguments.seed_evidence, root, repository / "target/release/ast-index")
                        cursor = state.execute("INSERT INTO rounds(phase,base_head,summary_json,created_at) VALUES (?,?,?,?)", (
                            "agent" if initial else "build", git(repository, "rev-parse", "HEAD"),
                            canonical_json(initial) if initial else None, now_ms()))
                    row = state.execute("SELECT * FROM rounds WHERE id=?", (cursor.lastrowid,)).fetchone()
                round_id = row["id"]
                directory = output / "rounds" / str(round_id)
                phase = row["phase"]
                print(canonical_json({"round": round_id, "phase": phase}), flush=True)
                try:
                    if phase == "build":
                        logged(["cargo", "build", "--release", "--workspace"], repository, directory, "build")
                        set_phase(state, round_id, "audit")
                    elif phase == "audit":
                        scan_arguments = argparse.Namespace(
                            project_root=str(root), output_dir=str(output / "audits"),
                            ast_index=str(repository / "target/release/ast-index"),
                            mcp_url=arguments.mcp_url, mcp_name=arguments.mcp_name,
                            timeout=arguments.timeout, case_limit=None, problem_limit=100,
                        )
                        summary = scan(scan_arguments)
                        print(canonical_json({key: summary[key] for key in ("counts", "remaining_checks", "unimplemented_features", "complete")}), flush=True)
                        if summary["complete"]:
                            set_phase(state, round_id, "pr", summary_json=canonical_json(summary))
                            continue
                        set_phase(state, round_id, "agent", summary_json=canonical_json(summary))
                    elif phase == "agent":
                        logged(agent_command, repository, directory, "agent", prompt=agent_prompt(json.loads(row["summary_json"]), root), timeout=arguments.agent_timeout)
                        if git(repository, "rev-parse", "HEAD") != row["base_head"]:
                            raise ToolError("HEAD changed during agent work; review before continuing")
                        if not changed_files(repository):
                            raise ToolError("agent made no code changes; the incomplete audit cannot count as success")
                        set_phase(state, round_id, "verify")
                    elif phase == "verify":
                        logged([sys.executable, "-m", "unittest", "discover", "-s", "tools/java-index-comparator", "-p", "test_*.py"], repository, directory, "tool-tests")
                        logged(["cargo", "build", "--release", "--workspace"], repository, directory, "fixed-build")
                        summary = json.loads(row["summary_json"])
                        if summary["counts"].get("fail"):
                            result = replay(Path(summary["evidence"]), root, repository / "target/release/ast-index", directory / "batch-verification")
                            if not result["verified"]:
                                raise ToolError("the original problem batch still fails or is unsupported; refusing to commit")
                        logged(["cargo", "test", "--release", "--workspace"], repository, directory, "workspace-tests")
                        git(repository, "diff", "--check")
                        set_phase(state, round_id, "commit")
                    elif phase == "commit":
                        if git(repository, "rev-parse", "HEAD") == row["base_head"]:
                            files = changed_files(repository)
                            if not files or any(not path.startswith(("src/", "tests/", "tools/", "crates/")) and path not in {"Cargo.toml", "Cargo.lock", ".gitignore", "README.md"} for path in files):
                                raise ToolError("changed files need review before staging")
                            git(repository, "add", "--", *files)
                            git(repository, "commit", "-m", f"Fix index discrepancies in audit round {round_id}")
                        head = git(repository, "rev-parse", "HEAD")
                        if git(repository, "rev-list", "--count", f"{row['base_head']}..{head}") != "1":
                            raise ToolError("unexpected commit history; review before pushing")
                        set_phase(state, round_id, "push", commit_head=head)
                    elif phase == "push":
                        if git(repository, "rev-parse", "HEAD") != row["commit_head"]:
                            raise ToolError("HEAD changed before push")
                        logged(["cargo", "test", "--release", "--workspace"], repository, directory, "committed-tests")
                        logged(["git", "push", "origin", branch], repository, directory, "push")
                        set_phase(state, round_id, "done")
                        completed += 1
                    elif phase == "pr":
                        logged(["cargo", "test", "--release", "--workspace"], repository, directory, "final-tests")
                        logged(["git", "push", "origin", branch], repository, directory, "final-push")
                        url = create_or_find_pr(repository, arguments.pr_repo, branch, directory)
                        with state:
                            state.execute("INSERT OR REPLACE INTO configuration VALUES ('upstream_pr',?)", (url,))
                        set_phase(state, round_id, "done")
                        print(canonical_json({"complete": True, "upstream_pr": url}), flush=True)
                        return 0
                    else:
                        raise ToolError("unknown checkpoint phase")
                except Exception as error:
                    with state:
                        state.execute("UPDATE rounds SET error=? WHERE id=?", (type(error).__name__, round_id))
                    raise
            return 1
        finally:
            state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mcp-url")
    parser.add_argument("--mcp-name", default="intellij-index")
    parser.add_argument("--pr-repo", default="defendend/Claude-ast-index-search")
    parser.add_argument("--seed-evidence", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--agent-command", default='["codex","exec","--approve-for-me","--json","-"]')
    parser.add_argument("--agent-timeout", type=float)
    parser.add_argument("--max-rounds", type=int)
    arguments = parser.parse_args()
    if arguments.max_rounds is not None and arguments.max_rounds < 1:
        parser.error("max-rounds must be positive")
    try:
        return run(arguments)
    except (ToolError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"cycle stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
