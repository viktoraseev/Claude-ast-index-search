"""Compact synthetic tests for batch truth, production CLI and exact replay."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, InvocationOracle, SCHEMA
from build_index import build_ast_index
from common import ToolError, connect, file_sha256, source_snapshot
from oracle_store import Metrics
from replay import replay
from text_snapshot import TextSnapshot, copy_snapshot


class SyntheticTextOracle:
    """Adapter fixture, not evidence about the real Index MCP implementation."""
    def __init__(self, root):
        self.root, self.calls, self.interrupt = root, [], None

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        path, = arguments['paths']
        if self.interrupt == path:
            raise ToolError('synthetic network interruption')
        assert tool == 'ide_search_text' and arguments['regex'] is True
        return {'matches': [{'file': path, 'line': index, 'context': line.strip()}
                            for index, line in enumerate((self.root / path).read_text().split('\n'), 1)
                            if line], 'hasMore': False}


class TextSnapshotTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        (self.root / 'Example.java').write_text('@Deprecated class Example {\n String label = "Ω";\n}\n')
        (self.root / 'Other.java').write_text('class Other {}\n')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.client = SyntheticTextOracle(self.root)
        self.metrics = Metrics(self.state)
        self.snapshot = TextSnapshot(self.root, self.state, self.client, self.metrics)
        with self.state:
            for identity, query in (('type', 'Example'), ('unicode', 'Ω'), ('absent', 'Missing')):
                self.state.execute("INSERT INTO checks(id,feature,subject) VALUES (?,'search:content',?)", (identity, query))

    def test_many_names_share_one_capture_and_keep_raw_provenance_separate(self):
        for identity, query in (('type', 'Example'), ('unicode', 'Ω'), ('absent', 'Missing')):
            result = self.snapshot.search(identity, query)
            self.assertEqual(bool(result), identity != 'absent')
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.state.execute('SELECT count(*) FROM text_snapshot_dependencies').fetchone()[0], 3)
        self.assertEqual(self.state.execute('SELECT count(*) FROM text_snapshot_pages').fetchone()[0], 2)
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 2)

    def test_interruption_resumes_at_file_checkpoint_even_with_invocation_cache(self):
        self.client.interrupt = 'Other.java'
        cached = InvocationOracle(self.client, self.state)
        interrupted = TextSnapshot(self.root, self.state, cached, cached.metrics)
        with self.assertRaises(ToolError):
            interrupted.search('type', 'Example')
        self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone())
        self.assertEqual(self.state.execute('SELECT count(*) FROM text_snapshot_dependencies').fetchone()[0], 0)
        self.client.interrupt = None
        TextSnapshot(self.root, self.state, cached, cached.metrics).search('type', 'Example')
        self.assertEqual(len(self.client.calls), 3)  # Completed first file not fetched again.

    def test_replay_reconstructs_from_raw_proof_not_corrupt_derived_rows(self):
        self.snapshot.search('unicode', 'Ω')
        with self.state:
            self.state.execute("UPDATE text_snapshot_lines SET content='FAKE'")
        destination = connect(self.directory / 'copy.sqlite')
        self.addCleanup(destination.close)
        destination.executescript(SCHEMA)
        copy_snapshot(self.state, destination, self.root)
        value = destination.execute("SELECT content FROM text_snapshot_lines WHERE path='Example.java' AND line=2").fetchone()[0]
        self.assertIn('Ω', value)
        self.assertNotIn('FAKE', value)

    def test_replay_rejects_tampered_raw_query_or_source(self):
        self.snapshot.search('type', 'Example')
        destination = connect(self.directory / 'copy.sqlite')
        self.addCleanup(destination.close)
        destination.executescript(SCHEMA)
        with self.state:
            self.state.execute("UPDATE oracle_responses SET request_json='{}'")
        with self.assertRaises(ToolError):
            copy_snapshot(self.state, destination, self.root)

    def test_eligibility_excludes_whitespace_sensitive_queries(self):
        for query in ('', ' A', 'A B', '\n', '.*'):
            self.assertFalse(TextSnapshot.eligible(query))
        for query in ('@Deprecated', 'Ω', 'get$Value'):
            self.assertTrue(TextSnapshot.eligible(query))

    def test_new_fixture_cannot_use_changed_source_or_inventory(self):
        self.snapshot.search('type', 'Example')
        (self.root / 'Example.java').write_text('class Changed {}\n')
        fresh = TextSnapshot(self.root, self.state, self.client, self.metrics)
        with self.assertRaises(ToolError):
            fresh.search('type', 'Example')

    def test_whole_line_capability_failure_falls_back_to_original_literal_contract(self):
        responses = []

        class NoFullLineOracle:
            def call(client, tool, arguments):
                responses.append(arguments)
                if arguments.get('regex'):
                    return {'matches': []}  # Incomplete full-line coverage.
                return {'matches': [{'file': 'Example.java', 'line': 1}], 'hasMore': False}

        fixture = Fixture(self.root, self.directory / 'unused', self.directory / 'unused',
                          self.state, NoFullLineOracle(), batch_text=True)
        with patch.object(fixture, 'cli', return_value={'content_matches': [{'path': 'Example.java', 'line': 1}]}):
            for _ in range(2):
                fixture.evaluate(self.state.execute("SELECT * FROM checks WHERE id='type'").fetchone())
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='type'").fetchone()[0], 'pass')
        self.assertEqual(len(responses), 3)  # One failed bulk probe, two actual literal calls.
        self.assertEqual(responses[1]['query'], 'Example')
        self.assertNotIn('regex', responses[1])
        self.assertEqual(self.state.execute('SELECT count(*) FROM text_snapshot_dependencies').fetchone()[0], 0)

    def test_production_cli_and_archived_replay_use_the_same_fixture(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        database = self.directory / 'index.sqlite'
        fingerprint = source_snapshot(self.root)[0]
        build_ast_index(str(binary), self.root, database, fingerprint)
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                   [('project_root', str(self.root)), ('snapshot_sha256', fingerprint)])
        fixture = Fixture(self.root, binary, database, self.state, self.client, batch_text=True)
        for check in list(self.state.execute('SELECT * FROM checks')):
            fixture.evaluate(check)
        self.assertEqual([row[0] for row in self.state.execute('SELECT verdict FROM checks')], ['pass'] * 3)
        self.assertEqual(len(self.client.calls), 2)
        # Force archived cases into the replay batch: this checks replay
        # plumbing, not a claim of a production regression being repaired.
        with self.state:
            self.state.execute("UPDATE checks SET verdict='fail'")
        with patch('replay.StreamableHttpMcpClient', side_effect=AssertionError('replay must be offline')), \
                patch('text_snapshot.file_sha256', wraps=file_sha256) as fingerprints:
            result = replay(self.directory / 'evidence.sqlite', self.root, binary, self.directory / 'replay')
        self.assertTrue(result['verified'], json.dumps(result))
        self.assertEqual(result['counts'], {'pass': 3})
        # Two files: validate archived raw proof once, validate the shared
        # snapshot once. Adding replay cases must not add project-wide hashes.
        self.assertEqual(fingerprints.call_count, 4)

    def test_shared_replay_session_still_rejects_sources_changed_mid_batch(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        fingerprint = source_snapshot(self.root)[0]
        for identity, query in (('type', 'Example'), ('unicode', '\u03a9')):
            self.snapshot.search(identity, query)
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                   [('project_root', str(self.root)), ('snapshot_sha256', fingerprint)])
            self.state.execute("UPDATE checks SET verdict='fail' WHERE id IN ('type','unicode')")
        original = Fixture.text_search_check
        evaluated = []

        def mutate_after_first(fixture, check):
            result = original(fixture, check)
            evaluated.append(check['id'])
            if len(evaluated) == 1:
                with (self.root / 'Other.java').open('a') as target:
                    target.write('// changed during replay\n')
            return result

        with patch.object(Fixture, 'text_search_check', mutate_after_first):
            with self.assertRaisesRegex(ToolError, 'changed during replay'):
                replay(self.directory / 'evidence.sqlite', self.root, binary, self.directory / 'mutated-replay')
        self.assertEqual(len(evaluated), 2)

    def test_atomic_build_replacement_cannot_interrupt_production_java_replay(self):
        # Operate on a disposable copy, never mutate the actual build output.
        built = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        binary = self.directory / 'build-output'
        shutil.copyfile(built, binary)
        binary.chmod(0o755)
        digest = file_sha256(binary)
        fingerprint = source_snapshot(self.root)[0]
        self.snapshot.search('type', 'Example')
        with self.state:
            self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                   [('project_root', str(self.root)), ('snapshot_sha256', fingerprint)])
            self.state.execute("UPDATE checks SET verdict='fail' WHERE id='type'")

        original_open, replaced = Path.open, []

        def replace_after_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == binary and args == ('rb',) and not replaced:
                replacement = self.directory / 'replacement'
                shutil.copyfile(built, replacement)
                with original_open(replacement, 'ab') as output:
                    output.write(b'\nsynthetic replacement fingerprint\n')
                replacement.chmod(0o755)
                os.replace(replacement, binary)
                replaced.append(True)
            return stream

        with patch.object(Path, 'open', replace_after_open), \
                patch('replay.StreamableHttpMcpClient', side_effect=AssertionError('offline replay')):
            result = replay(self.directory / 'evidence.sqlite', self.root, binary,
                            self.directory / 'replacement-replay')
        self.assertEqual(replaced, [True])
        self.assertTrue(result['verified'])
        self.assertEqual(result['counts'], {'pass': 1})
        state = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(state.execute("SELECT value FROM metadata WHERE key='binary_sha256'").fetchone()[0], digest)
            self.assertEqual(file_sha256(Path(result['verification']).parent / 'ast-index'), digest)
        finally:
            state.close()


if __name__ == '__main__':
    unittest.main()
