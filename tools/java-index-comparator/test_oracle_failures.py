"""Private diagnostic captures never become truth, cached successes or passes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, InvocationOracle, SCHEMA
from common import McpRemoteError, StreamableHttpMcpClient, connect


class OracleFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.private = {'isError': True, 'content': [{'type': 'text', 'text': 'SYNTHETIC_PRIVATE_DIAGNOSTIC'}]}
        self.error = McpRemoteError('MCP tool ide_find_class reported an error', 'tool', self.private)

    def test_transport_preserves_diagnostics_without_exposing_them_in_exception(self):
        client = StreamableHttpMcpClient('http://127.0.0.1/test')
        with patch.object(client, 'send', return_value=self.private):
            with self.assertRaises(McpRemoteError) as raised:
                client.call('ide_find_class', {'query': 'Example'})
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', str(raised.exception))
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', repr(raised.exception))
        self.assertEqual(raised.exception.response, self.private)

    def test_repeated_errors_are_durable_but_never_reused_as_success(self):
        client = Mock()
        client.call.side_effect = self.error
        oracle = InvocationOracle(client, self.state)
        for _ in range(2):
            with self.assertRaises(McpRemoteError):
                oracle.call('ide_find_class', {'query': 'Example'})
        self.assertEqual(client.call.call_count, 2)
        diagnostic = self.state.execute('SELECT * FROM oracle_failures').fetchone()
        self.assertEqual(diagnostic['attempts'], 2)
        self.assertEqual(json.loads(diagnostic['response_json']), self.private)
        self.assertEqual(json.loads(diagnostic['request_json']), {'query': 'Example'})
        for table in ('oracle_responses', 'oracle_cache', 'oracle_pages'):
            self.assertEqual(self.state.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        reopened = connect(self.root / 'evidence.sqlite', read_only=True)
        try:
            self.assertEqual(reopened.execute('SELECT attempts FROM oracle_failures').fetchone()[0], 2)
        finally:
            reopened.close()

    def test_parallel_failures_are_captured_on_the_owner_thread_not_as_truth(self):
        client = Mock()
        client.parallel_safe = True
        client.call.side_effect = self.error
        oracle = InvocationOracle(client, self.state)
        with self.assertRaises(McpRemoteError):
            oracle.prefetch('ide_find_class', [{'query': str(index)} for index in range(4)])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_failures').fetchone()[0], 4)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_responses').fetchone()[0], 0)

    def test_fixture_records_remote_error_not_a_native_mismatch_or_shape_pass(self):
        client = Mock()
        client.call.side_effect = self.error
        oracle = InvocationOracle(client, self.state)
        with self.state:
            self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('case','class','Example')")
        fixture = Fixture(self.root, self.root / 'unused', self.root / 'unused', self.state, oracle)
        check = self.state.execute("SELECT * FROM checks WHERE id='case'").fetchone()
        fixture.evaluate(check)
        row = self.state.execute("SELECT verdict,error FROM checks WHERE id='case'").fetchone()
        self.assertEqual(row['verdict'], 'error')
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', row['error'])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_failures').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
