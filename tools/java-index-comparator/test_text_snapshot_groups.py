"""Bounded Java text acquisition, production CLI and offline replay regressions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

WORKSPACE = Path(__file__).resolve().parents[2]
RUNTIME = WORKSPACE / 'tools/java-index-comparator'
sys.path.insert(1, str(RUNTIME))
from audit import Fixture, SCHEMA
from build_index import build_ast_index, capture_binary
from common import ToolError, McpRemoteError, connect, source_snapshot
from oracle_store import Metrics
from replay import replay
from text_snapshot import TextSnapshot, copy_snapshot



class Oracle:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.cursors = {}
        self.fail_path = None
        self.corrupt = None

    def call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if 'cursor' in arguments:
            rows, start = self.cursors[arguments['cursor']]
        else:
            if self.fail_path in arguments['paths']:
                raise ToolError('synthetic interruption')
            rows = []
            for path in arguments['paths']:
                rows.extend({'file': path, 'line': number, 'context': line.strip()}
                            for number, line in enumerate((self.root / path).read_text().split('\n'), 1)
                            if line)
            start = 0
        page = rows[start:start + 500]
        result = {'matches': page, 'hasMore': start + 500 < len(rows)}
        if result['hasMore']:
            cursor = str(len(self.cursors))
            self.cursors[cursor] = (rows, start + 500)
            result['nextCursor'] = cursor
        return self.corrupt(result) if self.corrupt else result


class GroupedTests(unittest.TestCase):
    def setUp(self):
        artifacts = WORKSPACE / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        for name, content in (
            ('A.java', '@Deprecated class A { String label = "Ω"; }\n'),
            ('B.java', 'class B { String label = "Ω"; }\n\n'),
            ('C.java', 'class C {}\n')):
            (self.root / name).write_text(content)
        self.state = self.database('evidence')
        for key, query in (('annotation', '@Deprecated'), ('type', 'A'), ('unicode', 'Ω'), ('absent', 'Missing')):
            self.state.execute("INSERT INTO checks(id,feature,subject) VALUES (?,'search:content',?)", (key, query))
        self.state.commit()
        self.oracle = Oracle(self.root)
        self.snapshot = TextSnapshot(self.root, self.state, self.oracle, Metrics(self.state))

    def database(self, name):
        connection = connect(self.directory / (name + '.sqlite'))
        connection.executescript(SCHEMA)
        self.addCleanup(connection.close)
        return connection

    def test_whole_scalar_and_grouped_locations_equal_with_fewer_calls(self):
        old_state = self.database('scalar')
        old_state.executemany("INSERT INTO checks(id,feature,subject) VALUES (?,'search:content',?)",
                              [('annotation', '@Deprecated'), ('type', 'A'), ('unicode', 'Ω'), ('absent', 'Missing')])
        old_state.commit()
        old_client = Oracle(self.root)
        old = TextSnapshot(self.root, old_state, old_client, Metrics(old_state))
        old.GROUP_FILES = 1
        for key, query in (('annotation', '@Deprecated'), ('type', 'A'), ('unicode', 'Ω'), ('absent', 'Missing')):
            self.assertEqual(old.search(key, query), self.snapshot.search(key, query))
        self.assertEqual(len(old_client.calls), 3)
        self.assertEqual(len(self.oracle.calls), 1)
        self.assertEqual(self.state.execute('SELECT count(*) FROM text_snapshot_pages').fetchone()[0], 3)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 1)

    def test_paginated_group_preserves_all_files_and_raw_replay(self):
        (self.root / 'A.java').write_text('class A {}\n' + '// line\n' * 800)
        self.snapshot.search('type', 'A')
        self.assertEqual(len(self.oracle.calls), 2)
        destination = self.database('replay')
        copy_snapshot(self.state, destination, self.root)
        self.assertEqual(list(self.state.execute('SELECT path,line,content FROM text_snapshot_lines ORDER BY path,line')),
                         list(destination.execute('SELECT path,line,content FROM text_snapshot_lines ORDER BY path,line')))
        self.assertEqual(destination.execute('SELECT count(*) FROM text_snapshot_pages').fetchone()[0], 6)

    def test_replay_ignores_corrupt_derived_rows(self):
        self.snapshot.search('unicode', 'Ω')
        self.state.execute("UPDATE text_snapshot_lines SET content='FAKE'")
        self.state.commit()
        destination = self.database('replay')
        copy_snapshot(self.state, destination, self.root)
        self.assertIn('Ω', destination.execute("SELECT content FROM text_snapshot_lines WHERE path='A.java'").fetchone()[0])

    def test_interruption_keeps_completed_groups_and_resume_queries_only_missing_file(self):
        self.snapshot.GROUP_FILES = 2
        self.oracle.fail_path = 'C.java'
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')
        self.assertEqual(self.state.execute("SELECT count(*) FROM text_snapshot_files WHERE status='complete'").fetchone()[0], 2)
        self.oracle.fail_path = None
        resumed = TextSnapshot(self.root, self.state, self.oracle, Metrics(self.state))
        resumed.GROUP_FILES = 2
        resumed.search('type', 'A')
        self.assertEqual([call[1]['paths'] for call in self.oracle.calls], [['A.java', 'B.java'], ['C.java'], ['C.java']])

    def test_line_budget_splits_groups_without_losing_files(self):
        self.snapshot.GROUP_LINES = 2
        self.snapshot.search('unicode', 'Ω')
        self.assertEqual([call[1]['paths'] for call in self.oracle.calls], [['A.java', 'B.java'], ['C.java']])
        self.assertEqual(self.state.execute("SELECT count(*) FROM text_snapshot_files WHERE status='complete'").fetchone()[0], 3)

    def test_byte_budget_splits_groups(self):
        self.snapshot.GROUP_BYTES = 1
        self.snapshot.search('type', 'A')
        self.assertEqual(len(self.oracle.calls), 3)

    def test_group_invalid_params_falls_back_to_verified_single_file_requests(self):
        original = self.oracle.call
        rejected = []
        def single_only(tool, arguments):
            if len(arguments.get('paths', [])) > 1:
                rejected.append(arguments)
                raise McpRemoteError('synthetic group unsupported', 'rpc', {'error': {'code': -32602}})
            return original(tool, arguments)
        self.oracle.call = single_only
        self.snapshot.search('unicode', 'Ω')
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(self.oracle.calls), 3)
        self.assertEqual(self.state.execute("SELECT count(*) FROM text_snapshot_files WHERE status='complete'").fetchone()[0], 3)
        copy_snapshot(self.state, self.database('replay'), self.root)

    def test_unrelated_remote_errors_are_not_masked_by_fallback(self):
        def failed(tool, arguments):
            raise McpRemoteError('synthetic internal error', 'rpc', {'error': {'code': -32603}})
        self.oracle.call = failed
        with self.assertRaises(McpRemoteError):
            self.snapshot.search('type', 'A')
        self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone())

    def test_transport_timeout_retries_same_request_without_inventing_truth(self):
        original, attempts = self.oracle.call, []
        def flaky(tool, arguments):
            attempts.append(dict(arguments))
            if len(attempts) == 1:
                raise ToolError('MCP HTTP request failed for tools/call: TimeoutError')
            return original(tool, arguments)
        self.oracle.call = flaky
        with patch('text_snapshot.time.sleep'):
            found = self.snapshot.search('unicode', 'Ω')
        self.assertEqual(len(found), 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 1)

    def test_persistent_transport_timeout_is_bounded_and_not_a_pass(self):
        attempts = []
        def timed_out(tool, arguments):
            attempts.append(arguments)
            raise ToolError('MCP HTTP request failed for tools/call: TimeoutError')
        self.oracle.call = timed_out
        with patch('text_snapshot.time.sleep'), self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')
        self.assertEqual(len(attempts), 3)
        self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 0)

    def test_decoder_error_is_not_retried(self):
        attempts = []
        def malformed(tool, arguments):
            attempts.append(arguments)
            raise ToolError('MCP HTTP request failed for tools/call: ValueError')
        self.oracle.call = malformed
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')
        self.assertEqual(len(attempts), 1)

    def test_foreign_location_and_missing_line_do_not_pass(self):
        self.oracle.corrupt = lambda result: {**result, 'matches': result['matches'][:-1]}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')
        self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone())

    def test_stale_reply_does_not_pass(self):
        self.oracle.corrupt = lambda result: {**result, 'stale': True}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_foreign_path_does_not_pass(self):
        self.oracle.corrupt = lambda result: {**result, 'matches': [
            *result['matches'], {'file': '../Foreign.java', 'line': 1, 'context': 'class Foreign {}'}]}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_truncation_and_missing_cursor_do_not_pass(self):
        self.oracle.corrupt = lambda result: {**result, 'truncated': True}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')
        self.oracle.corrupt = lambda result: {**result, 'hasMore': True}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_repeated_cursor_does_not_pass(self):
        (self.root / 'A.java').write_text('// line\n' * 1200)
        self.oracle.corrupt = lambda result: {**result, 'nextCursor': '0'}
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_changed_source_during_group_does_not_pass(self):
        def mutate(result):
            with (self.root / 'A.java').open('a') as target:
                target.write('// source changed\n')
            return result
        self.oracle.corrupt = mutate
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_exact_preview_mismatch_does_not_pass(self):
        def corrupt(result):
            result['matches'][0]['context'] = 'fabricated preview'
            return result
        self.oracle.corrupt = corrupt
        with self.assertRaises(ToolError):
            self.snapshot.search('type', 'A')

    def test_legacy_one_file_archive_remains_replayable(self):
        old = TextSnapshot(self.root, self.state, self.oracle, Metrics(self.state))
        old.GROUP_FILES = 1
        old.search('type', 'A')
        destination = self.database('replay')
        copy_snapshot(self.state, destination, self.root)
        self.assertEqual(list(self.state.execute('SELECT path,line,content FROM text_snapshot_lines ORDER BY path,line')),
                         list(destination.execute('SELECT path,line,content FROM text_snapshot_lines ORDER BY path,line')))

    def test_actual_native_cli_and_offline_replay_keep_whole_cases(self):
        built = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        binary, native_hash = capture_binary(built, self.directory / 'binaries')
        fingerprint = source_snapshot(self.root)[0]
        database = self.directory / 'index.sqlite'
        build_ast_index(str(binary), self.root, database, fingerprint)
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                              [('project_root', str(self.root)), ('snapshot_sha256', fingerprint)])
        self.state.commit()
        fixture = Fixture(self.root, binary, database, self.state, self.oracle, batch_text=True)
        for check in list(self.state.execute('SELECT * FROM checks')):
            fixture.evaluate(check)
        self.assertEqual(dict(self.state.execute('SELECT verdict,count(*) FROM checks GROUP BY verdict')), {'pass': 4})
        self.assertEqual(len(self.oracle.calls), 1)
        expected = dict(self.state.execute('SELECT id,expected_json FROM checks'))
        self.state.execute("UPDATE checks SET verdict='fail'")
        self.state.commit()
        with patch('replay.StreamableHttpMcpClient', side_effect=AssertionError('offline replay')):
            result = replay(self.directory / 'evidence.sqlite', self.root, binary, self.directory / 'replay')
        self.assertTrue(result['verified'], result.get('counts'))
        self.assertEqual(result['counts'], {'pass': 4})
        observed = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(dict(observed.execute('SELECT id,expected_json FROM checks')), expected)
            self.assertEqual(observed.execute("SELECT value FROM metadata WHERE key='binary_sha256'").fetchone()[0], native_hash)
        finally:
            observed.close()

    def test_replay_rejects_group_membership_tampering(self):
        self.snapshot.search('type', 'A')
        raw = self.state.execute('SELECT id,request_json FROM oracle_responses').fetchone()
        request = json.loads(raw['request_json'])
        request['paths'] = ['A.java']
        self.state.execute('UPDATE oracle_responses SET request_json=? WHERE id=?', (json.dumps(request), raw['id']))
        self.state.commit()
        with self.assertRaises(ToolError):
            copy_snapshot(self.state, self.database('replay'), self.root)

    def test_replay_rejects_rebound_request_even_with_same_logical_members(self):
        self.snapshot.search('type', 'A')
        row = self.state.execute('SELECT id,request_json FROM oracle_responses').fetchone()
        request = json.loads(row['request_json'])
        request['paths'].reverse()
        self.state.execute('UPDATE oracle_responses SET request_json=? WHERE id=?', (json.dumps(request), row['id']))
        self.state.commit()
        with self.assertRaises(ToolError):
            copy_snapshot(self.state, self.database('replay'), self.root)


if __name__ == '__main__':
    unittest.main()
