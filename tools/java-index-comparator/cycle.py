#!/usr/bin/env python3
"""Run resumable audit/fix/test/commit rounds, keeping agent output on disk."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from common import ToolError, adapter_digest, discover_mcp_url, canonical_json, connect, file_sha256, now_ms, source_snapshot


SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rounds(
 id INTEGER PRIMARY KEY,phase TEXT NOT NULL,base_head TEXT NOT NULL,
 summary_json TEXT,commit_head TEXT,error TEXT,created_at INTEGER NOT NULL
);
"""


class CommandFailed(ToolError):
    def __init__(self, stage: str, returncode: int):
        self.stage, self.returncode = stage, returncode
        super().__init__(f"{stage} failed ({returncode}); see round logs, payloads were not printed")


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
           *, prompt: str | None = None, timeout: float | None = None,
           acceptable_exit_codes: tuple[int, ...] = (0,)) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.stdout.log").open("ab") as stdout, (directory / f"{name}.stderr.log").open("ab") as stderr:
        result = subprocess.run(command, cwd=repository, input=prompt.encode() if prompt is not None else None,
                                stdout=stdout, stderr=stderr, timeout=timeout)
    if result.returncode not in acceptable_exit_codes:
        raise CommandFailed(name, result.returncode)


def command_summary(command: list[str], directory: Path, label: str) -> dict:
    name = f"{label}-{now_ms()}"
    repository = Path(__file__).resolve().parents[2]
    logged(command, repository, directory, name, acceptable_exit_codes=(0, 1))
    with (directory / f"{name}.stdout.log").open("rb") as output:
        value = output.read(1024 * 1024 + 1)
    if len(value) > 1024 * 1024:
        raise ToolError("tool emitted an unbounded aggregate summary")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ToolError("tool summary must be an object")
    return result


def scan(arguments: argparse.Namespace) -> dict:
    # Each invocation imports the current adapter implementation. A long-lived
    # driver must not keep using the pre-repair Python module after agent edits.
    command = [sys.executable, str(Path(__file__).with_name("audit.py")),
               "--project-root", arguments.project_root, "--output-dir", arguments.output_dir,
               "--ast-index", arguments.ast_index, "--mcp-name", arguments.mcp_name,
               "--timeout", str(arguments.timeout), "--problem-limit", str(arguments.problem_limit)]
    if arguments.mcp_url:
        command.extend(["--mcp-url", arguments.mcp_url])
    if arguments.case_limit is not None:
        command.extend(["--case-limit", str(arguments.case_limit)])
    return command_summary(command, Path(arguments.output_dir) / "logs", "audit")


def replay(evidence: Path, root: Path, binary: Path, output: Path, *, mcp_url: str | None = None, oracle_evidence: Path | None = None) -> dict:
    command = [sys.executable, str(Path(__file__).with_name("replay.py")),
               "--evidence", str(evidence), "--project-root", str(root),
               "--ast-index", str(binary), "--output-dir", str(output), "--limit", "100"]
    if mcp_url:
        command.extend(["--mcp-url", mcp_url])
    if oracle_evidence:
        command.extend(["--oracle-evidence", str(oracle_evidence)])
    return command_summary(command, output / "logs", "replay")


def reload_driver(completed: int = 0) -> None:
    """Reload repaired driver code only after a durable phase transition."""
    script = Path(__file__).resolve()
    if Path(sys.argv[0]).resolve() == script:
        os.environ['AST_INDEX_CYCLE_COMPLETED_ROUNDS'] = str(completed)
        os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])


def agent_prompt(summary: dict, root: Path) -> str:
    problems = any(summary.get('counts', {}).get(kind, 0)
                   for kind in ('fail', 'unsupported', 'error'))
    failed_verification = summary.get('verification', {}).get('verified') is False
    if problems or failed_verification:
        priority = "Round priority: repair the recorded problem batch or failed verification first."
    else:
        priority = """Round priority: close one coherent family of pending contracts,
not just one easy command followed by another expensive full audit. Inspect the
pending list first, group commands sharing an oracle or fixture, and implement
the related contracts together. Do not trade correctness for a coverage count;
leave genuinely unresolved contracts pending and explain the remaining gap."""
    return f"""Read AGENTS.md and repository contributor rules. This is one round of an
automated differential repair, not permission to redefine the goal.
{priority}
Target is read-only: {root}. Evidence SQLite: {summary['evidence']}.
Latest verification: {canonical_json(summary.get('verification', {}))}.
Round logs: {summary.get('round_logs', 'not available')}.
Oracle connection: {canonical_json(summary.get('oracle', {}))}.
Read the checks table using a streaming cursor: up to the first 100 verdict=fail
rows, plus unsupported/error and pending coverage contracts. Group by cause;
do not generate one test per row. Add compact, public-safe regression fixtures
that exercise actual production behaviour. Demonstrate failure before the fix,
then repair ast-index. If a verdict is a normalization/scope/pagination bug,
repair the comparator and add a test; never mask a production defect.
Distinguish MCP differential coverage from independent source and internal CLI/DB
contract checks. A native DB agreeing with native output does not establish MCP
equivalence. Label each evidence source accurately; do not claim MCP coverage
for an oracle-less command. Language-inapplicable features need explicit,
reproducible applicability evidence, not a blanket skip or a fake pass.
Determine applicability with a bounded inventory of all relevant file types
inside the exact target root. A Java-only inventory is not proof that another
language or framework is absent. Persist the inventory evidence privately and
add synthetic negative tests proving that an applicable feature cannot be
silently classified as inapplicable. Mutation commands may be exercised only
on disposable fixtures inside this repository's artifact directory, never on
the read-only target, real hooks, shared MCP configuration or another index.
Support missing audit contracts rather than marking them covered or skipping
them. All applicable ast-index features remain in scope, not only class/symbol.
Do not modify the target project. Do not commit target source, evidence databases,
private names or large fixtures. Do not change AGENTS.md. Keep project payloads
out of your messages. Do not commit or push: the driver verifies and commits.
Preserve the existing harness changes and regression assertions. Establish
red/green evidence for the current batch, not a defect fixed in an earlier round.
Validate broad-query normalization against full-name oracle
queries before treating a navigation mismatch as a production defect.
Do not install plugins/hooks/MCP configuration or write outside this repository
and its artifact directory. Project content belongs only in private artifacts;
public regression snippets must be small and synthetic.
Run relevant tests. The driver independently runs the tool suite, original-batch
replay and release workspace suite before committing. Do not spend the round on
unrelated baseline lint warnings or repeated whole-workspace checks; investigate
a broader check only when it exposes a regression caused by this repair.
Finish with a concise cause/fix/test summary.
"""


def set_phase(state: sqlite3.Connection, round_id: int, phase: str, **values: str) -> None:
    assignments = ["phase=?", "error=NULL", *(f"{key}=?" for key in values)]
    with state:
        state.execute(f"UPDATE rounds SET {','.join(assignments)} WHERE id=?", (phase, *values.values(), round_id))


def verify_equivalence(state: sqlite3.Connection, summary: dict, directory: Path) -> None:
    """Do not let optimizations silently drop established case/feature scopes."""
    policy = state.execute("SELECT value FROM configuration WHERE key='equivalence_reference'").fetchone()
    if not policy or summary.get('remaining_checks') or any(summary.get('counts', {}).get(key) for key in ('fail', 'unsupported', 'error')):
        # A deliberately bounded repair batch is not a completed full audit.
        return
    try:
        reference = json.loads(policy[0])
    except (TypeError, ValueError) as error:
        raise ToolError('invalid persisted audit-equivalence reference') from error
    if not isinstance(reference, str) or not reference:
        raise ToolError('invalid persisted audit-equivalence reference')
    from check_audit_equivalence import compare
    result = compare(Path(reference), Path(summary['evidence']), directory / 'audit-equivalence.sqlite')
    if not result['verified']:
        raise ToolError('audit equivalence failed; see private case-level report before continuing')
    print(canonical_json({'audit_equivalence': {key: result[key] for key in
                                               ('original_cases', 'present_cases', 'additional_cases', 'text_truth_cases', 'verified')}}), flush=True)


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
        if metadata.get("binary_sha256") != file_sha256(binary):
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
                if getattr(arguments, 'defer_pr', None) is not None:
                    state.execute("INSERT OR REPLACE INTO configuration VALUES ('defer_pr',?)",
                                  (canonical_json(arguments.defer_pr),))
                if getattr(arguments, 'equivalence_reference', None) is not None:
                    state.execute("INSERT OR REPLACE INTO configuration VALUES ('equivalence_reference',?)",
                                  (canonical_json(str(arguments.equivalence_reference.resolve())),))
            resume_batch = getattr(arguments, "resume_batch", None)
            if resume_batch is not None:
                row = state.execute("SELECT * FROM rounds WHERE phase!='done' ORDER BY id DESC LIMIT 1").fetchone()
                if row is None or row["phase"] != "audit" or not row["error"]:
                    raise ToolError("a revalidated batch can resume only a stopped audit checkpoint")
                if git(repository, "rev-parse", "HEAD") != row["base_head"]:
                    raise ToolError("HEAD changed since the interrupted audit")
                batch = connect(resume_batch.resolve(), read_only=True)
                try:
                    metadata = dict(batch.execute("SELECT key,value FROM metadata"))
                    if not metadata.get("original_evidence") or metadata.get("fixture_sha256") != adapter_digest():
                        raise ToolError("resume batch must be replayed through the current fixture")
                finally:
                    batch.close()
                summary = seed_summary(resume_batch, root, repository / "target/release/ast-index")
                set_phase(state, row["id"], "agent", summary_json=canonical_json(summary))
            completed = int(os.environ.pop('AST_INDEX_CYCLE_COMPLETED_ROUNDS', '0'))
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
                            text_mode=getattr(arguments, 'text_mode', 'batch'),
                        )
                        summary = scan(scan_arguments)
                        print(canonical_json({key: summary[key] for key in ("counts", "remaining_checks", "unimplemented_features", "complete")}), flush=True)
                        verify_equivalence(state, summary, directory)
                        if summary["complete"]:
                            set_phase(state, round_id, "pr", summary_json=canonical_json(summary))
                            continue
                        set_phase(state, round_id, "agent", summary_json=canonical_json(summary))
                    elif phase == "agent":
                        summary = {**json.loads(row["summary_json"]), "round_logs": str(directory),
                                   "oracle": {"mcp_url": arguments.mcp_url, "mcp_name": arguments.mcp_name}}
                        logged(agent_command, repository, directory, "agent", prompt=agent_prompt(summary, root), timeout=arguments.agent_timeout)
                        if git(repository, "rev-parse", "HEAD") != row["base_head"]:
                            raise ToolError("HEAD changed during agent work; review before continuing")
                        if not changed_files(repository):
                            raise ToolError("agent made no code changes; the incomplete audit cannot count as success")
                        set_phase(state, round_id, "verify")
                        reload_driver(completed)
                    elif phase == "verify":
                        summary = json.loads(row["summary_json"])
                        verify_equivalence(state, summary, directory)
                        try:
                            logged(["cargo", "build", "--release", "--workspace"], repository, directory, "fixed-build")
                            # Fixture tests invoke the production release CLI;
                            # never test new adapters against a stale executable.
                            logged([sys.executable, "-m", "unittest", "discover", "-s", "tools/java-index-comparator", "-p", "test_*.py"], repository, directory, "tool-tests")
                            if summary["counts"].get("fail") or summary["counts"].get("unsupported"):
                                result = replay(Path(summary["evidence"]), root, repository / "target/release/ast-index", directory / "batch-verification", mcp_url=arguments.mcp_url or discover_mcp_url(arguments.mcp_name),
                                                oracle_evidence=Path(summary['verification']['verification']) if summary.get('verification', {}).get('verification') else None)
                                if not result["verified"]:
                                    summary["verification"] = result
                                    set_phase(state, round_id, "agent", summary_json=canonical_json(summary))
                                    continue
                            logged(["cargo", "test", "--release", "--workspace"], repository, directory, "workspace-tests")
                        except CommandFailed as error:
                            # A failed repair is another repair attempt, not a
                            # terminal driver error. Preserve evidence and logs.
                            summary["verification"] = {"verified": False, "stage": error.stage,
                                                       "returncode": error.returncode}
                            set_phase(state, round_id, "agent", summary_json=canonical_json(summary))
                            continue
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
                        verify_equivalence(state, json.loads(row['summary_json']), directory)
                        logged(["cargo", "test", "--release", "--workspace"], repository, directory, "final-tests")
                        logged(["git", "push", "origin", branch], repository, directory, "final-push")
                        deferred = state.execute("SELECT value FROM configuration WHERE key='defer_pr'").fetchone()
                        if deferred and deferred[0] not in {'true', 'false'}:
                            raise ToolError('invalid persisted PR policy; no PR was created')
                        if deferred and deferred[0] == 'true':
                            set_phase(state, round_id, "done")
                            print(canonical_json({"complete": True, "pr_deferred": True,
                                                  "evidence": json.loads(row['summary_json'])['evidence']}), flush=True)
                            return 0
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
    pr_policy = parser.add_mutually_exclusive_group()
    pr_policy.add_argument('--defer-pr', dest='defer_pr', action='store_true', default=None,
                           help='Verify and finish this target without opening a PR; persists across resumes')
    pr_policy.add_argument('--create-pr', dest='defer_pr', action='store_false', default=None,
                           help='Create the PR after a complete audit, overriding a persisted deferral')
    parser.add_argument("--seed-evidence", type=Path)
    parser.add_argument('--equivalence-reference', type=Path,
                        help='Preserve prior full-audit case IDs/feature coverage/text truth; persists across resumes')
    parser.add_argument("--resume-batch", type=Path,
                        help="Resume a stopped audit using a batch revalidated against the current binary")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--agent-command", default='["codex","exec","--approve-for-me","--json","-"]')
    parser.add_argument("--agent-timeout", type=float)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument('--text-mode', choices=('batch', 'scalar'), default='batch',
                        help='MCP text acquisition mode; native CLI and semantic checks are unchanged')
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
