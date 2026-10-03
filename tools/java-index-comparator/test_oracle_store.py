import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, InvocationOracle, SCHEMA, Unsupported
from common import StreamableHttpMcpClient, canonical_json, connect
from oracle_store import Metrics, OracleStore
from replay import StoredOracle


class OracleStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        with self.state:
            self.state.executemany("INSERT INTO checks(id,feature,subject) VALUES (?,'class',?)",
                                   [('first', 'Example'), ('second', 'Other')])
        self.client = Mock()
        self.client.call.return_value = {'classes': [{'name': 'Example', 'file': 'Example.java', 'line': 1}]}

    def test_repeated_captures_store_one_payload_and_preserve_all_ordered_operations(self):
        oracle = InvocationOracle(self.client, self.state)
        fixture = Fixture(self.root, self.root / 'binary', self.root / 'index', self.state, oracle)
        arguments = {'project_path': str(self.root), 'query': 'Example', 'scope': 'project_files'}
        with self.state:
            for _ in range(100):
                fixture.paginated('first', 'ide_find_class', arguments, 'classes')
        self.client.call.assert_called_once()
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 1)
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 100)
        stored = StoredOracle(self.state, 'first')
        for _ in range(100):
            self.assertEqual(stored.call('ide_find_class', arguments), self.client.call.return_value)
        stored.assert_consumed()
        summary = oracle.metrics.summary()
        self.assertEqual(summary['oracle.memory_hit']['count'], 99)
        self.assertEqual(summary['mcp.tool.ide_find_class']['count'], 1)

    def test_memory_hits_do_not_reparse_json_and_replies_cannot_be_mutated(self):
        oracle = InvocationOracle(self.client, self.state)
        response = oracle.call('ide_find_class', {'query': 'Example'})
        with patch('audit.json.loads', side_effect=AssertionError('unnecessary JSON parse')):
            self.assertIs(oracle.call('ide_find_class', {'query': 'Example'}), response)
        with self.assertRaises(TypeError):
            response['classes'][0]['name'] = 'corrupted'
        with self.assertRaises(TypeError):
            response['classes'].append({})

    def test_memory_eviction_falls_back_to_disk_without_another_mcp_call(self):
        oracle = InvocationOracle(self.client, self.state, max_entries=1)
        first = oracle.call('ide_find_class', {'query': 'Example'})
        oracle.call('ide_find_class', {'query': 'Other'})
        self.assertEqual(oracle.call('ide_find_class', {'query': 'Example'}), first)
        self.assertEqual(self.client.call.call_count, 2)
        self.assertLessEqual(len(oracle.memory.entries), 1)

    def test_byte_budget_disables_oversize_memory_entries_without_losing_disk_evidence(self):
        oracle = InvocationOracle(self.client, self.state, max_bytes=100)
        for _ in range(2):
            oracle.call('ide_find_class', {'query': 'Example'})
        self.assertEqual(oracle.memory.bytes, 0)
        self.client.call.assert_called_once()
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 1)

    def test_different_scopes_and_different_answers_are_not_merged(self):
        store = OracleStore(self.state)
        response = {'classes': []}
        with self.state:
            store.page('first', 0, 'ide_find_class', {'scope': 'project_files'}, response)
            store.page('first', 1, 'ide_find_class', {'scope': 'libraries'}, response)
            store.page('first', 2, 'ide_find_class', {'scope': 'libraries'}, {'classes': [{'name': 'Other'}]})
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 3)

    def test_cache_reset_keeps_captured_evidence_but_forces_fresh_mcp_requests(self):
        oracle = InvocationOracle(self.client, self.state)
        reply = oracle.call('ide_find_class', {'query': 'Example'})
        with self.state:
            oracle.store.page('first', 0, 'ide_find_class', {'query': 'Example'}, reply)
            self.state.execute('DELETE FROM invocation_cache')
        InvocationOracle(self.client, self.state).call('ide_find_class', {'query': 'Example'})
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 1)

    def test_rollback_drops_only_uncommitted_page_links(self):
        store = OracleStore(self.state)
        with self.state:
            store.page('first', 0, 'ide_find_class', {}, {'classes': []})
            self.state.execute("UPDATE checks SET status='complete',verdict='pass' WHERE id='first'")
        store.page('second', 0, 'ide_find_class', {}, {'classes': []})
        self.state.rollback()
        self.assertEqual(self.state.execute('SELECT check_id FROM pages').fetchall()[0][0], 'first')
        self.assertEqual(self.state.execute('SELECT count(*) FROM pages').fetchone()[0], 1)

    def test_legacy_capture_tables_remain_readable_without_migration(self):
        legacy = connect(self.root / 'legacy.sqlite')
        self.addCleanup(legacy.close)
        legacy.executescript('CREATE TABLE checks(id TEXT,feature TEXT); CREATE TABLE pages(check_id TEXT,page INTEGER,request_json TEXT,response_json TEXT,tool TEXT);')
        with legacy:
            legacy.execute("INSERT INTO checks VALUES ('case','class')")
            legacy.execute('INSERT INTO pages VALUES (?,?,?,?,?)', ('case', 0, '{}', '{"classes":[]}', 'ide_find_class'))
        oracle = StoredOracle(legacy, 'case')
        self.assertEqual(oracle.call('ide_find_class', {}), {'classes': []})
        oracle.assert_consumed()

    def test_http_and_json_timings_do_not_contain_queries_or_payloads(self):
        metrics = Metrics(self.state)
        client = StreamableHttpMcpClient('http://example.invalid', metrics=metrics)
        response = Mock()
        response.headers = {'Content-Type': 'application/json'}
        response.read.return_value = canonical_json({'jsonrpc': '2.0', 'id': 1, 'result': {
            'content': [{'type': 'text', 'text': '{"classes":[{"name":"SECRET"}]}'}]}}).encode()
        transport = Mock()
        transport.__enter__ = Mock(return_value=response)
        transport.__exit__ = Mock(return_value=False)
        with patch('common.urllib_request.urlopen', return_value=transport):
            client.call('ide_find_class', {'query': 'SECRET'})
        summary = metrics.summary()
        self.assertEqual(summary['mcp.http']['count'], 1)
        self.assertEqual(summary['mcp.payload_decode']['count'], 1)
        self.assertNotIn('SECRET', json.dumps(summary))

    def test_bounded_prefetch_keeps_sqlite_on_the_owner_thread_and_replay_in_demand_order(self):
        gate = threading.Barrier(2)
        lock = threading.Lock()
        concurrency = {'active': 0, 'peak': 0}

        def reply(tool, arguments):
            with lock:
                concurrency['active'] += 1
                concurrency['peak'] = max(concurrency['peak'], concurrency['active'])
            gate.wait(timeout=2)
            with lock:
                concurrency['active'] -= 1
            return {'classes': [{'name': arguments['query']}]}

        self.client.parallel_safe = True
        self.client.call.side_effect = reply
        oracle = InvocationOracle(self.client, self.state)
        requests = [{'query': name} for name in ('A', 'B', 'C', 'D')]
        oracle.prefetch('ide_find_class', requests, workers=2)
        self.assertEqual(concurrency['peak'], 2)
        fixture = Fixture(self.root, self.root / 'binary', self.root / 'index', self.state, oracle)
        with self.state:
            for arguments in reversed(requests):
                fixture.paginated('first', 'ide_find_class', arguments, 'classes')
        self.assertEqual(self.client.call.call_count, 4)
        stored = StoredOracle(self.state, 'first')
        for arguments in reversed(requests):
            self.assertEqual(stored.call('ide_find_class', arguments)['classes'][0]['name'], arguments['query'])
        stored.assert_consumed()

    def test_prefetch_rejects_mutations_cursors_and_unbounded_worker_counts(self):
        oracle = InvocationOracle(self.client, self.state)
        from common import ToolError
        for tool, requests, workers in [('write_file', [{}], 2), ('ide_find_class', [{}], 5),
                                        ('ide_find_class', [{'cursor': 'next'}], 2)]:
            with self.subTest(tool=tool, workers=workers):
                with self.assertRaises(ToolError):
                    oracle.prefetch(tool, requests, workers=workers)
        self.client.call.assert_not_called()

    def test_prefetch_does_not_hide_stale_responses_with_an_unrecorded_retry(self):
        self.client.parallel_safe = True
        self.client.call.return_value = {'symbols': [], 'stale': True}
        for workers in (1, 2):
            with self.subTest(workers=workers):
                self.client.reset_mock()
                oracle = InvocationOracle(self.client, self.state)
                with self.assertRaisesRegex(Unsupported, 'stale'):
                    oracle.prefetch('ide_find_symbol', [{'query': 'A'}], workers=workers)
                self.client.call.assert_called_once()
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_cache').fetchone()[0], 0)

    def test_outline_does_not_query_literals_but_keeps_dollar_and_unicode_identifiers(self):
        (self.root / 'Example.java').write_text('class Example { int $0 = 123; int Ⅷvalue = 0xFF; }')
        self.client.call.return_value = {'symbols': []}
        fixture = Fixture(self.root, self.root / 'binary', self.root / 'index', self.state, self.client)
        fixture.cli = Mock(return_value={'symbols': []})
        fixture.outline_check({'id': 'first', 'subject': 'Example.java'})
        queries = [call.args[1]['query'] for call in self.client.call.call_args_list]
        self.assertTrue(all(not query[0].isdigit() for query in queries))
        self.assertIn('$', queries)
        self.assertIn('Ⅷ', queries)

    def test_parallel_http_requests_have_distinct_ids_and_thread_safe_metrics(self):
        from concurrent.futures import ThreadPoolExecutor
        metrics = Metrics(self.state)
        client = StreamableHttpMcpClient('http://example.invalid', metrics=metrics)
        ids, lock = [], threading.Lock()

        class Response:
            headers = {'Content-Type': 'application/json'}
            def __init__(self, request):
                self.request = json.loads(request.data)
                with lock:
                    ids.append(self.request['id'])
            def read(self, size=-1):
                body = canonical_json({'id': self.request['id'], 'result': {}}).encode()
                return body if size < 0 else body[:size]
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with patch('common.urllib_request.urlopen', side_effect=lambda request, **kwargs: Response(request)):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: client.send('tools/list'), range(32)))
        self.assertEqual(sorted(ids), list(range(1, 33)))
        self.assertEqual(metrics.summary()['mcp.http']['count'], 32)


if __name__ == '__main__':
    unittest.main()
