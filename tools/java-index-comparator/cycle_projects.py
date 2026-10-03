#!/usr/bin/env python3
"""Repair several explicit folders and require fresh evidence at one HEAD.

Repeat --project-root in the desired order. Repeat --target-output to reuse
existing per-project journals; otherwise private child directories are created
under --output-dir. Children always defer PR creation to this coordinator.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from common import ToolError, canonical_json, connect, now_ms, stable_id
import cycle


SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS targets(
 ordinal INTEGER PRIMARY KEY,root TEXT NOT NULL,output TEXT NOT NULL,
 status TEXT NOT NULL,head TEXT,evidence TEXT,error TEXT,updated_at INTEGER NOT NULL
);
"""


def lock_directory(directory, stack):
    directory.mkdir(parents=True, exist_ok=True)
    handle = stack.enter_context((directory / 'cycle.lock').open('a'))
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise ToolError('another cycle owns a requested artifact directory') from error


def wait_for_child(directory, ordinal):
    """Attach after an existing cycle, without starting a duplicate driver.

The kernel-held lock, not an old journal row, proves the owner is alive. A
stopped cycle releases it; its durable checkpoint can then be resumed normally.
"""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'cycle.lock').open('a') as handle:
        announced = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if not announced:
                    print(canonical_json({'target': ordinal, 'phase': 'waiting-for-existing-cycle'}), flush=True)
                    announced = True
                time.sleep(0.5)


def proof(repository, target, head, logs):
    """Read the latest durable child result, then run the current gate afresh.

Do not import audit into this long-lived process: coding-agent repairs can
change its required features while another project is being processed.
"""
    root, output = target
    journal = output / 'cycle.sqlite'
    if not journal.exists():
        return None
    state = connect(journal, read_only=True)
    try:
        state.execute('BEGIN')
        identity = state.execute("SELECT value FROM configuration WHERE key='identity'").fetchone()
        expected = {'root': str(root), 'repository': str(repository),
                    'branch': cycle.git(repository, 'branch', '--show-current')}
        if not identity or json.loads(identity[0]) != expected:
            raise ToolError('child journal belongs to another target, repository or branch')
        row = state.execute('SELECT * FROM rounds ORDER BY id DESC LIMIT 1').fetchone()
        if not row or row['phase'] != 'done' or row['error'] or row['base_head'] != head:
            return None
        summary = json.loads(row['summary_json'] or '{}')
        if (summary.get('complete') is not True or not summary.get('evidence')
                or summary.get('remaining_checks') != 0 or summary.get('unimplemented_features') != 0
                or not isinstance(summary.get('counts'), dict)
                or set(summary['counts']) != {'pass'}):
            return None
        evidence = Path(summary['evidence']).resolve()
        if not evidence.is_relative_to(output):
            raise ToolError('child evidence escaped its artifact directory')
    finally:
        state.close()
    result = cycle.command_summary([
        sys.executable, str(Path(__file__).with_name('completion.py')),
        '--evidence', str(evidence), '--project-root', str(root),
        '--ast-index', str(repository / 'target/release/ast-index')], logs, 'fresh-proof')
    if result.get('verified') is not True or summary['counts'] != {'pass': result.get('checks')}:
        return None
    # Preserve the child's original case IDs/text truth policy as well as its
    # current population. This only writes a private comparison report.
    state = connect(journal, read_only=True)
    try:
        cycle.verify_equivalence(state, summary, logs)
    finally:
        state.close()
    return {'evidence': str(evidence), 'head': head, 'checks': result['checks']}


def child_command(arguments, target):
    root, output = target
    command = [sys.executable, str(Path(__file__).with_name('cycle.py')),
               '--project-root', str(root), '--output-dir', str(output), '--defer-pr',
               '--mcp-name', arguments.mcp_name, '--timeout', str(arguments.timeout),
               '--text-mode', arguments.text_mode, '--agent-command', arguments.agent_command]
    if arguments.mcp_url:
        command += ['--mcp-url', arguments.mcp_url]
    if arguments.agent_timeout is not None:
        command += ['--agent-timeout', str(arguments.agent_timeout)]
    return command


def run(arguments):
    repository = Path(__file__).resolve().parents[2]
    boundary = (repository / '.artifacts').resolve()
    output = Path(arguments.output_dir).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in arguments.project_root]
    if not roots or len(set(roots)) != len(roots) or any(not root.is_dir() for root in roots):
        raise ToolError('provide distinct existing project folders')
    outputs = ([Path(path).expanduser().resolve() for path in arguments.target_output]
               if arguments.target_output else [output / 'projects' / stable_id(str(root))[:20] for root in roots])
    if len(outputs) != len(roots) or len(set(outputs)) != len(outputs):
        raise ToolError('target-output must be distinct and match project-root count')
    for artifact in [output, *outputs]:
        if not artifact.is_relative_to(boundary) or artifact == boundary:
            raise ToolError('coordinator artifacts must stay inside repository .artifacts')
        if any(artifact == root or artifact.is_relative_to(root) for root in roots):
            raise ToolError('artifacts must be outside every read-only target')
    if output in outputs:
        raise ToolError('coordinator and child journals must use different directories')
    branch = cycle.git(repository, 'branch', '--show-current')
    if not branch or branch in {'main', 'master'}:
        raise ToolError('select a feature branch before automatic repairs')
    targets = list(zip(roots, outputs))
    with ExitStack() as stack:
        lock_directory(output, stack)
        state = connect(output / 'projects.sqlite')
        stack.callback(state.close)
        state.executescript(SCHEMA)
        identity = canonical_json({'repository': str(repository), 'branch': branch,
                                   'targets': [[str(root), str(child)] for root, child in targets]})
        old = state.execute("SELECT value FROM configuration WHERE key='identity'").fetchone()
        if old and old[0] != identity:
            raise ToolError('coordinator journal belongs to another target list or branch')
        with state:
            state.execute("INSERT OR REPLACE INTO configuration VALUES ('identity',?)", (identity,))
            for ordinal, (root, child) in enumerate(targets, 1):
                state.execute("INSERT OR IGNORE INTO targets VALUES (?,?,?,'pending',NULL,NULL,NULL,?)",
                              (ordinal, str(root), str(child), now_ms()))
        while True:
            head = cycle.git(repository, 'rev-parse', 'HEAD')
            refreshed = False
            for ordinal, target in enumerate(targets, 1):
                wait_for_child(target[1], ordinal)
                if cycle.git(repository, 'rev-parse', 'HEAD') != head:
                    refreshed = True
                    break
                result = proof(repository, target, head, output / 'logs' / str(ordinal))
                if result is None:
                    with state:
                        state.execute("UPDATE targets SET status='repairing',error=NULL,updated_at=? WHERE ordinal=?",
                                      (now_ms(), ordinal))
                    print(canonical_json({'target': ordinal, 'phase': 'repair'}), flush=True)
                    try:
                        cycle.logged(child_command(arguments, target), repository, output / 'logs' / str(ordinal), 'child-cycle')
                    except Exception as error:
                        with state:
                            state.execute("UPDATE targets SET status='interrupted',error=?,updated_at=? WHERE ordinal=?",
                                          (type(error).__name__, now_ms(), ordinal))
                        raise
                    # An exit code is not proof of completion. Start with a
                    # fresh HEAD and recheck *all* earlier projects as well.
                    new_head = cycle.git(repository, 'rev-parse', 'HEAD')
                    result = proof(repository, target, new_head, output / 'logs' / str(ordinal))
                    if result is None:
                        raise ToolError('child exited without current complete evidence')
                    refreshed = True
                with state:
                    state.execute("UPDATE targets SET status='verified',head=?,evidence=?,error=NULL,updated_at=? WHERE ordinal=?",
                                  (result['head'], result['evidence'], now_ms(), ordinal))
                if refreshed:
                    break
            if refreshed:
                continue
            # Exclude other child cycles during the final cross-project gate.
            with ExitStack() as final_locks:
                for _, child in targets:
                    lock_directory(child, final_locks)
                if cycle.git(repository, 'rev-parse', 'HEAD') != head or cycle.changed_files(repository):
                    raise ToolError('HEAD or worktree changed before final multi-project verification')
                if any(proof(repository, target, head, output / 'logs' / str(i)) is None
                       for i, target in enumerate(targets, 1)):
                    continue
                cycle.logged(['cargo', 'test', '--release', '--workspace'], repository, output / 'logs', 'final-tests')
                cycle.logged(['git', 'push', 'origin', branch], repository, output / 'logs', 'final-push')
                if cycle.git(repository, 'rev-parse', 'HEAD') != head or cycle.changed_files(repository):
                    raise ToolError('HEAD or worktree changed during final multi-project verification')
                if any(proof(repository, target, head, output / 'logs' / str(i)) is None
                       for i, target in enumerate(targets, 1)):
                    continue
                url = None if arguments.defer_pr else cycle.create_or_find_pr(repository, arguments.pr_repo, branch, output / 'logs')
                with state:
                    state.execute("INSERT OR REPLACE INTO configuration VALUES ('verified_head',?)", (head,))
                    if url:
                        state.execute("INSERT OR REPLACE INTO configuration VALUES ('upstream_pr',?)", (url,))
                print(canonical_json({'complete': True, 'projects': len(targets), 'head': head,
                                      'pr_deferred': arguments.defer_pr, 'upstream_pr': url}), flush=True)
                return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', action='append', required=True)
    parser.add_argument('--target-output', action='append')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--mcp-url')
    parser.add_argument('--mcp-name', default='intellij-index')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--agent-command', default='["codex","exec","--approve-for-me","--json","-"]')
    parser.add_argument('--agent-timeout', type=float)
    parser.add_argument('--text-mode', choices=('batch', 'scalar'), default='batch')
    parser.add_argument('--pr-repo', default='defendend/Claude-ast-index-search')
    parser.add_argument('--defer-pr', action='store_true')
    arguments = parser.parse_args()
    try:
        return run(arguments)
    except (ToolError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(canonical_json({'complete': False, 'error': type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
