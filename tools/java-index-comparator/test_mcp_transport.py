"""Bounded protocol failures must never expose MCP project payloads."""
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from common import StreamableHttpMcpClient, ToolError


class Response:
    def __init__(self, body):
        self.stream = BytesIO(body)
        self.headers = {'Content-Type': 'application/json'}
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.stream.read(size)


class McpTransportTests(unittest.TestCase):
    def test_response_read_is_bounded_before_decoding_any_payload(self):
        response = Response(b'x' * 100)
        client = StreamableHttpMcpClient('http://127.0.0.1/test')
        with patch('common.MCP_RESPONSE_MAX_BYTES', 32, create=True), \
                patch('common.urllib_request.urlopen', return_value=response), \
                patch.object(client, '_decode', side_effect=AssertionError('oversized data was decoded')):
            with self.assertRaisesRegex(ToolError, 'response.*budget'):
                client.send('tools/call')
        self.assertEqual(response.read_sizes, [33])

    def test_small_complete_reply_is_unchanged(self):
        response = Response(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'symbols': []}}).encode())
        with patch('common.urllib_request.urlopen', return_value=response):
            self.assertEqual(StreamableHttpMcpClient('http://127.0.0.1/test').send('tools/call'), {'symbols': []})
        self.assertTrue(response.read_sizes[0] > 0)

    def test_rpc_error_payload_is_not_present_in_public_exception(self):
        response = Response(json.dumps({'id': 1, 'error': {'code': -32603, 'message': 'PRIVATE_PROJECT_PAYLOAD'}}).encode())
        with patch('common.urllib_request.urlopen', return_value=response):
            with self.assertRaises(ToolError) as raised:
                StreamableHttpMcpClient('http://127.0.0.1/test').send('tools/call')
        self.assertNotIn('PRIVATE_PROJECT_PAYLOAD', str(raised.exception))

    def test_invalid_json_and_utf8_are_explicit_redacted_protocol_errors(self):
        for body in (b'PRIVATE_PROJECT_PAYLOAD', b'\xffPRIVATE_PROJECT_PAYLOAD'):
            with self.subTest(body=body), patch('common.urllib_request.urlopen', return_value=Response(body)):
                with self.assertRaises(ToolError) as raised:
                    StreamableHttpMcpClient('http://127.0.0.1/test').send('tools/call')
                self.assertNotIn('PRIVATE_PROJECT_PAYLOAD', str(raised.exception))

    def test_wrong_tool_result_shape_is_explicit_not_an_unhandled_attribute_error(self):
        client = StreamableHttpMcpClient('http://127.0.0.1/test')
        with patch.object(client, 'send', return_value=None):
            with self.assertRaises(ToolError):
                client.call('ide_find_symbol', {})

    def test_invalid_catalogue_and_initialization_do_not_hide_missing_contracts(self):
        client = StreamableHttpMcpClient('http://127.0.0.1/test')
        with patch.object(client, 'send', return_value=None) as send:
            with self.assertRaises(ToolError):
                client.initialize()
            self.assertEqual(send.call_count, 1)  # No initialized notification on failure.
        for result in (None, {}, {'tools': [None]}, {'tools': [{'name': 'same'}, {'name': 'same'}]}):
            with self.subTest(result=result), patch.object(client, 'send', return_value=result):
                with self.assertRaises(ToolError):
                    client.tools()


if __name__ == '__main__':
    unittest.main()
