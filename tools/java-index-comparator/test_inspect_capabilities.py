import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from common import connect
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


if __name__ == '__main__':
    unittest.main()
