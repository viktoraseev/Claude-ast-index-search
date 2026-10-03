import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from audit import Fixture, SCHEMA, Unsupported
from benchmark_text_batch import capture_file, compare_recorded
from common import canonical_json, connect


class TextBatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA + '''
            CREATE TABLE batch_lines(path TEXT,line INTEGER,content TEXT,PRIMARY KEY(path,line));
            CREATE TABLE batch_requests(request_json TEXT PRIMARY KEY);
            CREATE TABLE batch_comparisons(id TEXT PRIMARY KEY,verdict TEXT,difference_json TEXT);
        ''')
        self.client = Mock()
        self.fixture = Fixture(self.root, self.root / 'unused', self.root / 'unused', self.state, self.client)
        (self.root / 'A.java').write_bytes('class A {\r\n\r\n  // Ω Ω\r\n}\r\n'.encode())

    def reply(self, lines, **flags):
        contents = {1: 'class A {', 3: '// Ω Ω', 4: '}'}
        return {'matches': [{'file': 'A.java', 'line': line, 'context': contents.get(line)} for line in lines], **flags}

    def test_full_nonempty_line_coverage_and_cursor_chain_are_required(self):
        self.client.call.side_effect = [self.reply([1, 3], hasMore=True, nextCursor='page2'),
                                        self.reply([4], hasMore=False)]
        self.assertEqual(capture_file(self.fixture, 'A.java'), 3)
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(self.client.call.call_args_list[1].args[1],
                         {'project_path': str(self.root), 'pageSize': 500, 'cursor': 'page2'})
        self.assertEqual(self.state.execute('SELECT count(*) FROM batch_lines').fetchone()[0], 3)

    def test_missing_line_outside_file_and_stale_reply_cannot_be_used(self):
        for response in (self.reply([1, 4]), self.reply([1, 3, 4], stale=True),
                         {'matches': [{'file': 'B.java', 'line': 1}]}):
            with self.subTest(response=response):
                self.client.call.side_effect = None
                self.client.call.return_value = response
                with self.assertRaises(Unsupported):
                    capture_file(self.fixture, 'A.java')
                self.assertEqual(self.state.execute('SELECT count(*) FROM batch_lines').fetchone()[0], 0)

    def test_file_budget_rejects_before_network_or_source_loading(self):
        with self.assertRaises(Unsupported):
            capture_file(self.fixture, 'A.java', maximum_bytes=1)
        self.client.call.assert_not_called()

    def test_matching_positions_with_different_ide_text_are_rejected(self):
        response = self.reply([1, 3, 4])
        response['matches'][0]['context'] = 'class Unsaved {'
        self.client.call.return_value = response
        with self.assertRaises(Unsupported):
            capture_file(self.fixture, 'A.java')
        self.assertEqual(self.state.execute('SELECT count(*) FROM batch_lines').fetchone()[0], 0)

    def test_source_change_rolls_back_the_file_checkpoint(self):
        def changed(*args):
            (self.root / 'A.java').write_text('class Changed {}\n')
            return self.reply([1, 3, 4])
        self.client.call.side_effect = changed
        with self.assertRaises(Unsupported):
            capture_file(self.fixture, 'A.java')
        self.assertEqual(self.state.execute('SELECT count(*) FROM batch_lines').fetchone()[0], 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 0)

    def test_comparison_uses_recorded_mcp_not_native_index_and_records_actual_differences(self):
        self.client.call.return_value = self.reply([1, 3, 4])
        capture_file(self.fixture, 'A.java')
        baseline = connect(self.root / 'baseline.sqlite')
        self.addCleanup(baseline.close)
        baseline.executescript(SCHEMA)
        with baseline:
            for identity, query, lines in (('unicode', 'Ω', [3]), ('wrong', 'A', []), ('empty', 'Absent', [])):
                baseline.execute("INSERT INTO checks(id,feature,subject,status) VALUES (?,'search:content',?,'complete')",
                                 (identity, query))
                request = {'project_path': str(self.root), 'query': query, 'caseSensitive': True,
                           'filePattern': '*.java', 'context': 'all'}
                baseline.execute('INSERT INTO pages VALUES (?,?,?,?,?)',
                                 (identity, 0, canonical_json(request), canonical_json(self.reply(lines)), 'ide_search_text'))
        result = compare_recorded(baseline, self.state, self.root, {'A.java'})
        self.assertEqual((result['cases'], result['pass'], result['mismatch'], result['unsupported']), (3, 2, 1, 0))
        row = self.state.execute("SELECT difference_json FROM batch_comparisons WHERE id='wrong'").fetchone()
        self.assertEqual(row[0], '{"extra":[["A.java",1]],"missing":[]}')


if __name__ == '__main__':
    unittest.main()
