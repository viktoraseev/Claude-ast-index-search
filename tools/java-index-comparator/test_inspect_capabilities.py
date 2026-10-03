import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from common import connect, ToolError, McpRemoteError
from inspect_capabilities import inspect


class CapabilitiesTests(unittest.TestCase):
    def test_only_readonly_metadata_calls_and_only_target_status_are_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'target'
            state = connect(Path(directory) / 'capabilities.sqlite')
            self.addCleanup(state.close)
            client = Mock()
            client.initialize.return_value = {'secret': 'server metadata'}
            client.tools.return_value = {
                'ide_project_status': {'inputSchema': {'properties': {'project_path': {}}}},
                'ide_index_status': {'inputSchema': {'properties': {'project_path': {}}}},
                'ide_safe_delete': {'inputSchema': {'properties': {'path': {}}}}}
            client.call.side_effect = [
                {'projects': [{'path': str(root), 'name': 'PRIVATE'}, {'path': '/outside', 'name': 'OTHER'}]},
                {'isIndexing': False}]
            result = inspect(client, root, state)
            self.assertEqual([call.args[0] for call in client.call.call_args_list],
                             ['ide_project_status', 'ide_index_status'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('OTHER', json.dumps(dict(state.execute('SELECT key,value FROM capabilities'))))
            self.assertEqual(result['index_status_fields'], ['isIndexing'])

    def test_late_failure_keeps_completed_metadata_without_claiming_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'target'
            state = connect(Path(directory) / 'capabilities.sqlite')
            self.addCleanup(state.close)
            client = Mock()
            client.initialize.return_value = {'name': 'synthetic-server'}
            client.tools.return_value = {'ide_project_status': {}, 'ide_index_status': {}}
            private = {'isError': True, 'content': [{'type': 'text', 'text': 'SYNTHETIC_PRIVATE_ERROR'}]}
            client.call.side_effect = [{'projects': [{'path': str(root), 'open': True}]},
                                       McpRemoteError('redacted remote error', 'tool', private)]
            with self.assertRaises(McpRemoteError):
                inspect(client, root, state)
            saved = {key: json.loads(value) for key, value in state.execute('SELECT key,value FROM capabilities')}
            self.assertEqual(saved['stage'], 'failed:index_status')
            self.assertEqual(saved['tools'], client.tools.return_value)
            self.assertTrue(saved['target_status'][0]['open'])
            self.assertNotIn('index_status', saved)
            self.assertEqual(saved['failure']['response'], private)

    def test_interrupted_retry_never_exposes_the_previous_successful_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'target'
            state = connect(Path(directory) / 'capabilities.sqlite')
            self.addCleanup(state.close)
            client = Mock()
            client.initialize.return_value = {}
            client.tools.return_value = {'ide_project_status': {}, 'ide_index_status': {}}
            client.call.side_effect = [{'projects': [{'path': str(root), 'open': True}]}, {'isIndexing': False}]
            self.assertTrue(inspect(client, root, state)['complete'])
            client.call.side_effect = ToolError('metadata unavailable')
            with self.assertRaises(ToolError):
                inspect(client, root, state)
            saved = {key: json.loads(value) for key, value in state.execute('SELECT key,value FROM capabilities')}
            self.assertEqual(saved['stage'], 'failed:target_status')
            self.assertNotIn('target_status', saved)
            self.assertNotIn('index_status', saved)


if __name__ == '__main__':
    unittest.main()
