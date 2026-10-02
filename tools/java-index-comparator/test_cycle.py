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
    def test_empty_problem_batch_prioritizes_a_coverage_family(self):
        summary = {"evidence": "evidence.sqlite", "counts": {"pass": 100},
                   "unimplemented_features": 53}
        prompt = cycle.agent_prompt(summary, Path("project"))
        self.assertIn("close one coherent family of pending contracts", prompt)
        self.assertIn("not just one easy command", prompt)
        self.assertIn("all relevant file types", prompt)
        self.assertIn("Java-only inventory is not proof", prompt)

    def test_recorded_problems_and_failed_verification_take_priority(self):
        cases = [{"counts": {kind: 1}} for kind in ("fail", "unsupported", "error")]
        cases.append({"counts": {"pass": 100}, "verification": {"verified": False}})
        for case in cases:
            with self.subTest(case=case):
                prompt = cycle.agent_prompt({"evidence": "evidence.sqlite", **case}, Path("project"))
                self.assertIn("repair the recorded problem batch or failed verification first", prompt)
                self.assertNotIn("close one coherent family of pending contracts", prompt)

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

    def test_revalidated_batch_resumes_stopped_audit_then_runs_a_fresh_full_audit(self):
        self.exercise_cycle(resume=True)

    def exercise_cycle(self, failed_stage=None, resume=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = argparse.Namespace(
                project_root=str(root / "target"), output_dir=str(root / "artifacts"),
                agent_command='["test-agent"]', timeout=5, agent_timeout=10,
                mcp_url="http://localhost/test", mcp_name="test", max_rounds=None,
                pr_repo="owner/repository",
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
            with patch.object(cycle, "git", side_effect=git), patch.object(cycle, "changed_files", side_effect=lambda _: ["src/fix.rs"] if status["dirty"] else []), patch.object(cycle, "logged", side_effect=logged), patch.object(cycle, "replay", return_value={"verified": True}), patch.object(cycle, "seed_summary", return_value=summary), patch.object(cycle, "create_or_find_pr", return_value="https://github.com/owner/repository/pull/1"), patch.object(cycle, "scan", side_effect=[final] if resume else [summary, final]) as scan, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cycle.run(arguments), 0)
            self.assertEqual(scan.call_count, 1 if resume else 2)
            self.assertTrue(all(call.args[0].case_limit is None for call in scan.call_args_list))
            self.assertIn(("git", "add", "--", "src/fix.rs"), commands)
            self.assertIn(("git", "push", "origin", "feature"), commands)
            self.assertEqual(sum(command[:4] == ("cargo", "test", "--release", "--workspace") for command in commands),
                             4 if failed_stage == "workspace-tests" else 3)
            self.assertEqual(commands.count(("test-agent",)), 2 if failed_stage else 1)
            # Production fixture tests must see the repaired binary even when
            # the coding agent ran only targeted/debug tests.
            self.assertEqual(stages[stages.index("tool-tests") - 1], "fixed-build")
            if failed_stage:
                agent_positions = [index for index, command in enumerate(commands) if command == ("test-agent",)]
                commit_position = next(index for index, command in enumerate(commands) if command[:2] == ("git", "commit"))
                self.assertLess(agent_positions[1], commit_position)


if __name__ == "__main__":
    unittest.main()
