"""Several real gates must agree; child exit codes and old journals do not suffice."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, canonical_json, connect
import cycle
import cycle_projects as projects


class ProjectCyclesTests(unittest.TestCase):
    def setUp(self):
        self.repository = Path(__file__).resolve().parents[2]
        base = self.repository / '.artifacts' / 'tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.roots = [self.directory / name for name in ('first', 'second')]
        for root in self.roots:
            root.mkdir()
        self.output = self.directory / 'coordinator'
        self.targets = list(zip(self.roots, [self.directory / name for name in ('one', 'two')]))
        self.arguments = argparse.Namespace(
            project_root=list(map(str, self.roots)), target_output=[str(out) for _, out in self.targets],
            output_dir=str(self.output), mcp_url='http://127.0.0.1/test', mcp_name='fixture',
            timeout=5, text_mode='batch', agent_command='["synthetic-agent"]', agent_timeout=None,
            pr_repo='fixture/upstream', defer_pr=False)

    def journal(self, target, *, head='head', complete=True, phase='done'):
        root, output = target
        state = connect(output / 'cycle.sqlite')
        try:
            state.executescript(cycle.SCHEMA)
            identity = {'root': str(root), 'repository': str(self.repository), 'branch': 'feature'}
            evidence = output / 'audits/evidence.sqlite'
            summary = {'complete': complete, 'evidence': str(evidence), 'remaining_checks': 0,
                       'unimplemented_features': 0, 'counts': {'pass': 10}}
            with state:
                state.execute("INSERT OR REPLACE INTO configuration VALUES ('identity',?)", (canonical_json(identity),))
                state.execute('INSERT INTO rounds(phase,base_head,summary_json,created_at) VALUES (?,?,?,1)',
                              (phase, head, canonical_json(summary)))
        finally:
            state.close()

    def test_journal_and_current_gate_both_required_without_importing_cached_audit(self):
        target = self.targets[0]
        with patch.object(cycle, 'git', return_value='feature'), \
                patch.object(cycle, 'command_summary', return_value={'verified': True, 'checks': 10}) as gate, \
                patch.object(cycle, 'verify_equivalence') as equivalent:
            self.assertIsNone(projects.proof(self.repository, target, 'head', self.output))
            self.journal(target, head='old')
            self.assertIsNone(projects.proof(self.repository, target, 'head', self.output))
            self.journal(target, complete=False)
            self.assertIsNone(projects.proof(self.repository, target, 'head', self.output))
            self.journal(target, phase='audit')
            self.assertIsNone(projects.proof(self.repository, target, 'head', self.output))
            gate.assert_not_called()
            self.journal(target)
            result = projects.proof(self.repository, target, 'head', self.output)
            self.assertEqual(result['head'], 'head')
            self.assertEqual(result['checks'], 10)
            command = gate.call_args.args[0]
            self.assertEqual(Path(command[1]).name, 'completion.py')
            equivalent.assert_called_once()
            gate.return_value = {'verified': False}
            self.assertIsNone(projects.proof(self.repository, target, 'head', self.output))

    def test_contradictory_complete_summary_cannot_bypass_original_case_equivalence(self):
        self.journal(self.targets[0])
        state = connect(self.targets[0][1] / 'cycle.sqlite')
        self.addCleanup(state.close)
        summary = json.loads(state.execute('SELECT summary_json FROM rounds').fetchone()[0])
        with patch.object(cycle, 'git', return_value='feature'), \
                patch.object(cycle, 'command_summary', return_value={'verified': True, 'checks': 10}), \
                patch.object(cycle, 'verify_equivalence'):
            for changes in ({'remaining_checks': 1}, {'unimplemented_features': 1},
                            {'counts': {'pass': 10, 'fail': 1}}, {'counts': {'pass': 9}}):
                with self.subTest(changes=changes):
                    with state:
                        state.execute('UPDATE rounds SET summary_json=?', (canonical_json({**summary, **changes}),))
                    self.assertIsNone(projects.proof(self.repository, self.targets[0], 'head', self.output))

    def test_directory_lock_excludes_another_cycle_and_releases_on_exit(self):
        with projects.ExitStack() as owner:
            projects.lock_directory(self.output, owner)
            with projects.ExitStack() as contender:
                with self.assertRaisesRegex(ToolError, 'another cycle'):
                    projects.lock_directory(self.output, contender)
        with projects.ExitStack() as next_owner:
            projects.lock_directory(self.output, next_owner)

    def test_existing_live_child_is_waited_for_not_restarted(self):
        waits = []
        attempts = [BlockingIOError(), None]

        def flock(*args):
            result = attempts.pop(0)
            if result:
                raise result

        with patch.object(projects.fcntl, 'flock', side_effect=flock), \
                patch.object(projects.time, 'sleep', side_effect=waits.append), \
                patch.object(cycle, 'logged') as execute, contextlib.redirect_stdout(io.StringIO()) as output:
            projects.wait_for_child(self.output, 1)
        self.assertEqual(waits, [0.5])
        self.assertIn('waiting-for-existing-cycle', output.getvalue())
        execute.assert_not_called()

    def test_completed_existing_child_head_is_refreshed_without_redundant_audit(self):
        status = {'head': 'before'}

        def git(repository, *args):
            return 'feature' if args[0] == 'branch' else status['head']

        def waited(*args):
            status['head'] = 'after'

        def proof(repository, target, head, logs):
            self.assertEqual(head, 'after')
            return {'evidence': str(target[1] / 'evidence.sqlite'), 'head': head, 'checks': 10}

        with patch.object(cycle, 'git', side_effect=git), \
                patch.object(projects, 'wait_for_child', side_effect=waited), \
                patch.object(projects, 'proof', side_effect=proof), \
                patch.object(cycle, 'changed_files', return_value=[]), \
                patch.object(cycle, 'logged') as execute, \
                patch.object(cycle, 'create_or_find_pr', return_value='public-pr'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(projects.run(self.arguments), 0)
        self.assertNotIn('child-cycle', [call.args[3] for call in execute.call_args_list])

    def test_wrong_project_journal_and_escaping_evidence_are_rejected(self):
        self.journal(self.targets[0])
        state = connect(self.targets[0][1] / 'cycle.sqlite')
        self.addCleanup(state.close)
        with patch.object(cycle, 'git', return_value='feature'), patch.object(cycle, 'command_summary') as gate:
            with state:
                state.execute("UPDATE configuration SET value='{}' WHERE key='identity'")
            with self.assertRaisesRegex(ToolError, 'another target'):
                projects.proof(self.repository, self.targets[0], 'head', self.output)
            self.journal(self.targets[0])
            summary = json.loads(state.execute('SELECT summary_json FROM rounds ORDER BY id DESC LIMIT 1').fetchone()[0])
            with state:
                state.execute('UPDATE rounds SET summary_json=?',
                              (json.dumps({**summary, 'evidence': str(self.directory / 'escaped.sqlite')}),))
            with self.assertRaisesRegex(ToolError, 'escaped'):
                projects.proof(self.repository, self.targets[0], 'head', self.output)
            gate.assert_not_called()

    def exercise(self, *, changed_during_final=False, failed_child=False, defer=False, incomplete_child=False):
        self.arguments.defer_pr = defer
        state = {'head': 'initial', 'proofs': {}, 'calls': [], 'mutation': False}

        def git(repository, *args):
            return 'feature' if args[0] == 'branch' else state['head']

        def proof(repository, target, head, logs):
            if state['proofs'].get(target[0]) != head:
                return None
            return {'evidence': str(target[1] / 'evidence.sqlite'), 'head': head, 'checks': 10}

        def logged(command, repository, logs, label):
            state['calls'].append((label, command))
            if label == 'child-cycle':
                if failed_child:
                    raise cycle.CommandFailed(label, 7)
                root = Path(command[command.index('--project-root') + 1])
                self.assertIn('--defer-pr', command)
                if root == self.roots[1] and state['head'] == 'initial':
                    state['head'] = 'fixed'
                if not incomplete_child:
                    state['proofs'][root] = state['head']
            if label == 'final-tests' and changed_during_final:
                state['head'] = 'unexpected'

        with patch.object(cycle, 'git', side_effect=git), \
                patch.object(projects, 'proof', side_effect=proof), \
                patch.object(cycle, 'logged', side_effect=logged), \
                patch.object(cycle, 'changed_files', return_value=[]), \
                patch.object(cycle, 'create_or_find_pr', return_value='public-pr') as create, \
                contextlib.redirect_stdout(io.StringIO()):
            if changed_during_final or failed_child or incomplete_child:
                with self.assertRaises(ToolError):
                    projects.run(self.arguments)
                create.assert_not_called()
            else:
                self.assertEqual(projects.run(self.arguments), 0)
                if defer:
                    create.assert_not_called()
                else:
                    create.assert_called_once()
                children = [cmd for label, cmd in state['calls'] if label == 'child-cycle']
                self.assertEqual([Path(cmd[cmd.index('--project-root') + 1]) for cmd in children],
                                 [self.roots[0], self.roots[1], self.roots[0]])
                self.assertEqual(set(state['proofs'].values()), {'fixed'})
                journal = connect(self.output / 'projects.sqlite', read_only=True)
                try:
                    self.assertEqual(set(row[0] for row in journal.execute('SELECT head FROM targets')), {'fixed'})
                    self.assertEqual(journal.execute("SELECT value FROM configuration WHERE key='verified_head'").fetchone()[0], 'fixed')
                finally:
                    journal.close()
        return state

    def test_second_project_fix_rechecks_first_before_one_pr(self):
        self.exercise()

    def test_failed_child_has_atomic_interruption_checkpoint_and_can_resume(self):
        self.exercise(failed_child=True)
        state = connect(self.output / 'projects.sqlite', read_only=True)
        try:
            self.assertEqual(tuple(state.execute('SELECT status,error FROM targets WHERE ordinal=1').fetchone()),
                             ('interrupted', 'CommandFailed'))
        finally:
            state.close()
        self.exercise()

    def test_changed_final_head_never_opens_pr(self):
        self.exercise(changed_during_final=True)

    def test_successful_child_exit_without_proof_never_opens_pr(self):
        self.exercise(incomplete_child=True)

    def test_deferred_run_still_requires_all_projects_and_final_tests(self):
        result = self.exercise(defer=True)
        self.assertIn('final-tests', [label for label, _ in result['calls']])

    def test_manifest_mismatch_and_unsafe_artifacts_rejected_before_child(self):
        self.exercise(defer=True)
        self.arguments.project_root.reverse()
        with patch.object(cycle, 'git', return_value='feature'), patch.object(cycle, 'logged') as logged:
            with self.assertRaisesRegex(ToolError, 'another target list'):
                projects.run(self.arguments)
            logged.assert_not_called()
        self.arguments.output_dir = str(self.roots[0] / 'unsafe')
        with self.assertRaisesRegex(ToolError, 'outside every'):
            projects.run(self.arguments)


if __name__ == '__main__':
    unittest.main()
