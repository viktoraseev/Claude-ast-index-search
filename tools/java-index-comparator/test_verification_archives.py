"""Private recorded Java oracle regressions cannot bypass automatic gates."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from common import ToolError, connect
import cycle
from verification_archives import verify_registered, preserved_expectations


class VerificationArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name).resolve()
        self.root = self.output / 'project'
        self.root.mkdir()
        self.manifest = self.output / 'verification-inputs.json'
        self.replay = Mock(return_value={'verified': True, 'counts': {'pass': 1}})

    def archive(self, name='recorded', root=None, count=1):
        evidence = self.output / (name + '.sqlite')
        state = connect(evidence)
        try:
            state.executescript('CREATE TABLE metadata(key TEXT,value TEXT);'
                                'CREATE TABLE checks(verdict TEXT);')
            with state:
                state.execute('INSERT INTO metadata VALUES (?,?)', ('project_root', str(root or self.root)))
                state.executemany('INSERT INTO checks VALUES (?)', [('fail',)] * count)
        finally:
            state.close()
        return {'label': name, 'evidence': str(evidence)}

    def verify(self):
        return verify_registered(self.output, self.root, Path('native'), self.output / 'results', self.replay)

    def test_absent_manifest_does_not_run_anything(self):
        self.assertIsNone(self.verify())
        self.replay.assert_not_called()

    def test_failed_java_archive_remains_failed_without_live_oracle(self):
        archive = self.archive()
        self.manifest.write_text(json.dumps({'schema': 1, 'archives': [archive]}))
        verification = self.output / 'results' / 'recorded' / 'verification.sqlite'
        verification.parent.mkdir(parents=True)
        verification.touch()
        self.replay.return_value = {'verified': False, 'counts': {'fail': 1}, 'verification': str(verification)}
        from unittest.mock import patch
        with patch('verification_archives.preserved_expectations', return_value={'preserved': True}):
            result = self.verify()
        self.assertFalse(result['verified'])
        self.assertEqual(result['archives'][0]['source_evidence'], archive['evidence'])
        self.replay.assert_called_once_with(Path(archive['evidence']), self.root,
                                            Path('native'), self.output / 'results' / 'recorded')

    def test_invalid_json_types_are_rejected_before_commands(self):
        for value in (None, [], 1, {'schema': 1, 'archives': []}):
            with self.subTest(value=value):
                self.manifest.write_text(json.dumps(value))
                with self.assertRaises(ToolError):
                    self.verify()
        self.replay.assert_not_called()

    def test_green_replay_cannot_change_the_recorded_oracle_expectation(self):
        paths = [self.output / name for name in ('source.sqlite', 'result.sqlite')]
        for path in paths:
            state = connect(path)
            try:
                state.executescript('CREATE TABLE checks(id TEXT,feature TEXT,subject TEXT,status TEXT,verdict TEXT,expected_json TEXT);')
                with state:
                    state.execute('INSERT INTO checks VALUES (?,?,?,?,?,?)',
                                  ('java-case', 'call-tree', 'method', 'complete', 'fail',
                                   json.dumps({'metadata': {'depth': 2}, 'items': [{'name': 'callee'}]})))
            finally:
                state.close()
        self.assertEqual(preserved_expectations(*paths), {'compared': 1, 'changed': 0, 'preserved': True})
        state = connect(paths[1])
        with state:
            state.execute("UPDATE checks SET verdict='pass',expected_json='{}'")
        state.close()
        self.assertEqual(preserved_expectations(*paths), {'compared': 1, 'changed': 1, 'preserved': False})

    def test_validate_entire_batch_before_first_replay(self):
        good = self.archive()
        for invalid in (self.archive('other', root=self.output / 'wrong'),
                        self.archive('too_many', count=101), good,
                        {'label': '../escape', 'evidence': good['evidence']},
                        {'label': 'escape', 'evidence': str(self.output.parent / 'outside.sqlite')}):
            with self.subTest(invalid=invalid['label']):
                self.manifest.write_text(json.dumps({'schema': 1, 'archives': [good, invalid]}))
                with self.assertRaises(ToolError):
                    self.verify()
        self.replay.assert_not_called()

    def test_unfinished_archive_is_rejected(self):
        archive = self.archive()
        Path(archive['evidence'] + '-wal').write_bytes(b'not-complete')
        self.manifest.write_text(json.dumps({'schema': 1, 'archives': [archive]}))
        with self.assertRaises(ToolError):
            self.verify()
        self.replay.assert_not_called()

    def test_already_committed_failure_queues_repair_without_rewriting_history(self):
        state = connect(self.output / 'cycle.sqlite')
        self.addCleanup(state.close)
        state.executescript(cycle.SCHEMA)
        with state:
            state.execute('INSERT INTO rounds(id,phase,base_head,commit_head,created_at) VALUES (?,?,?,?,?)',
                          (49, 'push', 'old', 'committed', 1))
        row = state.execute('SELECT * FROM rounds').fetchone()
        failure = {'verified': False, 'stage': 'registered-oracle-regressions'}
        cycle.queue_committed_regressions(state, row, {'counts': {'pass': 10}}, failure, 'committed')
        rows = state.execute('SELECT * FROM rounds ORDER BY id').fetchall()
        self.assertEqual([(r['id'], r['phase'], r['base_head'], r['commit_head']) for r in rows],
                         [(49, 'done', 'old', 'committed'), (50, 'agent', 'committed', None)])
        self.assertEqual(json.loads(rows[1]['summary_json'])['verification'], failure)
        with self.assertRaises(ToolError):
            cycle.queue_committed_regressions(state, row, {}, failure, 'different')
        self.assertEqual(state.execute('SELECT count(*) FROM rounds').fetchone()[0], 2)
