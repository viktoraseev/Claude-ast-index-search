import argparse
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import SCHEMA as EVIDENCE_SCHEMA
import cycle
from common import ToolError, connect


class CycleTests(unittest.TestCase):
    def test_pr_requires_the_audited_head_and_clean_worktree(self):
        for head, changed in (('different-head', []), ('audited-head', ['src/changed.rs'])):
            with self.subTest(head=head, changed=changed), patch.object(cycle, 'git', return_value=head), patch.object(cycle, 'changed_files', return_value=changed), patch.object(cycle, 'verify_completed_evidence') as verify:
                with self.assertRaises(cycle.ToolError):
                    cycle.assert_ready({'evidence': 'evidence.sqlite'}, Path('target'), Path('repository'), 'audited-head')
                verify.assert_not_called()

    def test_equivalence_gate_is_persisted_and_rejects_dropped_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = connect(directory / 'cycle.sqlite')
            self.addCleanup(state.close)
            state.executescript(cycle.SCHEMA)
            with state:
                state.execute("INSERT INTO configuration VALUES ('equivalence_reference',?)",
                              (cycle.canonical_json(str(directory / 'reference.sqlite')),))
            summary = {'evidence': str(directory / 'new.sqlite'), 'remaining_checks': 0, 'counts': {'pass': 10}}
            with patch('check_audit_equivalence.compare', return_value={'verified': False}) as check:
                with self.assertRaises(ToolError):
                    cycle.verify_equivalence(state, summary, directory)
                check.assert_called_once()
            with patch('check_audit_equivalence.compare') as check:
                for partial in ({'remaining_checks': 1}, {'counts': {'fail': 1}}, {'counts': {'error': 1}}):
                    cycle.verify_equivalence(state, {**summary, **partial}, directory)
                check.assert_not_called()

    def test_empty_problem_batch_prioritizes_a_coverage_family(self):
        summary = {"evidence": "evidence.sqlite", "counts": {"pass": 100},
                   "unimplemented_features": 53}
        prompt = cycle.agent_prompt(summary, Path("project"))
        self.assertIn("close one coherent family of pending contracts", prompt)
        self.assertIn("not just one easy command", prompt)
        self.assertIn("all relevant file types", prompt)
        self.assertIn("Java-only inventory is not proof", prompt)
        self.assertIn("Scope is JAVA ONLY", prompt)
        self.assertIn("Do not repair Kotlin, Swift, Perl, shell", prompt)
        self.assertIn("Ignore non-Java pending rows in older evidence", prompt)
        self.assertNotIn("All applicable ast-index features remain in scope", prompt)
        self.assertIn("Preserve concurrently added regression assertions too", prompt)
        self.assertIn("Do not run git restore, git checkout or git reset", prompt)
        self.assertIn("retain unrelated and concurrent", prompt)

    def test_pending_parent_requires_finite_evidence_based_closure(self):
        prompt = cycle.agent_prompt({"evidence": "evidence.sqlite", "counts": {"pass": 100},
                                     "unimplemented_features": 7}, Path("project"))
        for requirement in ("finite acceptance checklist", "commands/options and entity mappings",
                            "not a substitute for executable criteria",
                            "Do not mark a parent implemented by relabeling",
                            "every existing case ID"):
            self.assertTrue(requirement in prompt, "missing concrete coverage-closure requirement")

    def test_pending_parent_does_not_invent_unbounded_compiler_requirements(self):
        prompt = cycle.agent_prompt({"evidence": "evidence.sqlite", "counts": {"pass": 100},
                                     "unimplemented_features": 7}, Path("project"))
        for requirement in ("advertised command/API contract or a captured target",
                            "concrete input, expected result and stopping condition",
                            "invent compiler-equivalence or new external/JDK binary-loader",
                            "Preserve every recorded case and unsupported",
                            "required API expansion for user direction",
                            "not as a silent exclusion, invented pass or automatic scope expansion"):
            self.assertIn(requirement, prompt)

    def test_mixed_framework_contracts_do_not_expand_java_scope(self):
        for counts in ({"pass": 100}, {"fail": 1}):
            with self.subTest(counts=counts):
                prompt = cycle.agent_prompt({"evidence": "evidence.sqlite", "counts": counts},
                                            Path("project"))
                for requirement in ("Do not repair XML parsers",
                                    "split Java-applicable criteria from non-Java-only criteria",
                                    "must not block Java completion",
                                    "Preserve earlier fixes, regression assertions and case IDs",
                                    "never turn an untested Java criterion into a pass"):
                    self.assertIn(requirement, prompt)

    def test_recorded_problems_and_failed_verification_take_priority(self):
        cases = [{"counts": {kind: 1}} for kind in ("fail", "unsupported", "error")]
        cases.append({"counts": {"pass": 100}, "verification": {"verified": False}})
        for case in cases:
            with self.subTest(case=case):
                prompt = cycle.agent_prompt({"evidence": "evidence.sqlite", **case}, Path("project"))
                self.assertIn("repair the recorded problem batch or failed verification first", prompt)
                self.assertNotIn("close one coherent family of pending contracts", prompt)

    def test_deferred_outline_cannot_replace_missing_parent_acceptance(self):
        summary = {"evidence": "evidence.sqlite", "counts": {"pass": 15353},
                   "remaining_checks": 205, "deferred_outline_checks": 205,
                   "unimplemented_features": 5}
        prompt = cycle.agent_prompt(summary, Path("project"))
        for instruction in ("Outline runs LAST", "do not select deferred outline checks",
                            "implement that missing acceptance mechanism",
                            "actually pending parent from coverage"):
            self.assertIn(instruction, prompt)
        self.assertIn("require executed evidence for its acceptance checklist", prompt)

    def test_failed_command_keeps_payload_on_disk_not_in_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ToolError) as caught:
                cycle.logged([cycle.sys.executable, "-c", "import sys;print('private payload');sys.exit(3)"], root, root / "logs", "agent")
            self.assertNotIn("private payload", str(caught.exception))
            self.assertIn("private payload", (root / "logs/agent.stdout.log").read_text())

    def test_phase_and_metadata_are_committed_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = connect(Path(temporary) / "cycle.sqlite")
            self.addCleanup(state.close)
            state.executescript(cycle.SCHEMA)
            with state:
                state.execute("INSERT INTO rounds VALUES (1,'audit','head',NULL,NULL,'old error',1)")
            cycle.set_phase(state, 1, "agent", summary_json='{"complete":false}')
            row = state.execute("SELECT * FROM rounds WHERE id=1").fetchone()
            self.assertEqual(row["phase"], "agent")
            self.assertIsNone(row["error"])
            self.assertEqual(row["summary_json"], '{"complete":false}')

    def test_incomplete_round_runs_agent_verification_commit_then_full_audit_again(self):
        self.exercise_cycle()

    def test_verification_failures_return_to_agent_before_commit(self):
        for stage in ("tool-tests", "fixed-build", "workspace-tests"):
            with self.subTest(stage=stage):
                self.exercise_cycle(stage)

    def test_committed_test_failure_queues_repair_before_any_push(self):
        self.exercise_cycle('committed-tests')

    def test_revalidated_batch_resumes_stopped_audit_then_runs_a_fresh_full_audit(self):
        self.exercise_cycle(resume=True)

    def test_deferred_pr_still_requires_a_complete_audit_and_final_tests(self):
        self.exercise_cycle(defer_pr=True)

    def test_stale_final_audit_is_refreshed_without_creating_a_pr_or_repairing_again(self):
        self.exercise_cycle(stale_ready=True)

    def test_pr_deferral_survives_driver_reload_without_the_flag(self):
        self.exercise_cycle(defer_pr=None, persisted_defer=True)

    def test_explicit_pr_creation_overrides_persisted_deferral(self):
        self.exercise_cycle(defer_pr=False, persisted_defer=True)

    def exercise_cycle(self, failed_stage=None, resume=False, defer_pr=False, persisted_defer=False, stale_ready=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = argparse.Namespace(
                project_root=str(root / "target"), output_dir=str(root / "artifacts"),
                agent_command='["test-agent"]', timeout=5, agent_timeout=10,
                mcp_url="http://localhost/test", mcp_name="test", max_rounds=None,
                pr_repo="owner/repository",
                defer_pr=defer_pr,
            )
            status = {"head": "before", "dirty": False, "failed": False}
            commands = []
            stages = []

            def git(repository, *args):
                commands.append(("git", *args))
                if args[:2] == ("branch", "--show-current"):
                    return "feature"
                if args[0] == "rev-parse":
                    return status["head"]
                if args[0] == "commit":
                    status.update(head="after", dirty=False)
                if args[0] == "rev-list":
                    return "1"
                return ""

            def logged(command, *args, **kwargs):
                commands.append(tuple(command))
                if len(args) >= 3:
                    stages.append(args[2])
                if command == ["test-agent"]:
                    status["dirty"] = True
                    self.assertIn("Evidence SQLite", kwargs["prompt"])
                    if status["failed"]:
                        self.assertIn(failed_stage, kwargs["prompt"])
                if len(args) >= 3 and args[2] == failed_stage and not status["failed"]:
                    status["failed"] = True
                    raise cycle.CommandFailed(failed_stage, 1)

            summary = {"evidence": str(root / "evidence.sqlite"), "counts": {"fail": 100},
                       "remaining_checks": 20, "unimplemented_features": 1, "complete": False}
            final = {**summary, "counts": {"pass": 120}, "remaining_checks": 0,
                     "unimplemented_features": 0, "complete": True}
            if persisted_defer:
                journal = connect(root / 'artifacts/cycle.sqlite')
                journal.executescript(cycle.SCHEMA)
                with journal:
                    journal.execute("INSERT INTO configuration VALUES ('defer_pr','true')")
                journal.close()
            if resume:
                journal = connect(root / "artifacts/cycle.sqlite")
                journal.executescript(cycle.SCHEMA)
                with journal:
                    journal.execute("INSERT INTO rounds VALUES (1,'audit','before',NULL,NULL,'CommandFailed',1)")
                journal.close()
                arguments.resume_batch = root / "revalidated.sqlite"
                batch = connect(arguments.resume_batch)
                batch.executescript(EVIDENCE_SCHEMA)
                with batch:
                    batch.executemany("INSERT INTO metadata VALUES (?,?)", {
                        "original_evidence": str(root / "evidence.sqlite"),
                        "fixture_sha256": cycle.adapter_digest(),
                    }.items())
                batch.close()
            def readiness(*args):
                if stale_ready and not status.get('stale_refreshed'):
                    status['stale_refreshed'] = True
                    raise cycle.StaleEvidence('synthetic changed fingerprint')
                return {'verified': True}

            scans = [final] if resume else [summary, final]
            if stale_ready:
                scans.append(final)
            with patch.object(cycle, "git", side_effect=git), patch.object(cycle, "changed_files", side_effect=lambda _: ["src/fix.rs"] if status["dirty"] else []), patch.object(cycle, "logged", side_effect=logged), patch.object(cycle, "replay", return_value={"verified": True}), patch.object(cycle, "seed_summary", return_value=summary), patch.object(cycle, "create_or_find_pr", return_value="https://github.com/owner/repository/pull/1") as create_pr, patch.object(cycle, "assert_ready", side_effect=readiness) as ready, patch.object(cycle, "scan", side_effect=scans) as scan, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cycle.run(arguments), 0)
            deferred = persisted_defer if defer_pr is None else defer_pr
            self.assertEqual(create_pr.call_count, 0 if deferred else 1)
            if deferred:
                self.assertIn('"pr_deferred":true', output.getvalue())
            self.assertEqual(scan.call_count, (1 if resume else 2) + int(stale_ready))
            self.assertEqual(ready.call_count, 2 + int(stale_ready))
            self.assertTrue(all(call.args[0].case_limit is None for call in scan.call_args_list))
            self.assertIn(("git", "add", "--", "src/fix.rs"), commands)
            self.assertIn(("git", "push", "origin", "feature"), commands)
            self.assertEqual(sum(command[:4] == ("cargo", "test", "--release", "--workspace") for command in commands),
                             5 if failed_stage == "committed-tests" else 4 if failed_stage == "workspace-tests" else 3)
            self.assertEqual(commands.count(("test-agent",)), 2 if failed_stage else 1)
            # Production fixture tests must see the repaired binary even when
            # the coding agent ran only targeted/debug tests.
            self.assertEqual(stages[stages.index("tool-tests") - 1], "fixed-build")
            if failed_stage:
                agent_positions = [index for index, command in enumerate(commands) if command == ("test-agent",)]
                commit_position = next(index for index, command in enumerate(commands) if command[:2] == ("git", "commit"))
                if failed_stage == 'committed-tests':
                    commits = [index for index, command in enumerate(commands) if command[:2] == ('git', 'commit')]
                    self.assertEqual(len(commits), 2)
                    self.assertLess(commit_position, agent_positions[1])
                    self.assertLess(agent_positions[1], commits[1])
                    push_position = commands.index(('git', 'push', 'origin', 'feature'))
                    self.assertLess(commits[1], push_position)
                else:
                    self.assertLess(agent_positions[1], commit_position)


if __name__ == "__main__":
    unittest.main()
