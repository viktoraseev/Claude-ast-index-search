"""The ast-index MCP helper must route per-call Java roots over its default.

This uses owned source and the real local helper/CLI, not Index MCP oracle data.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from root_contracts import Runner


class McpExplicitRootTests(unittest.TestCase):
    def test_per_call_root_overrides_server_default_even_for_relative_paths(self):
        repository = Path(__file__).resolve().parents[2]
        artifacts = repository / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary).resolve()
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', repository / 'target/release/ast-index')).resolve()
            helper = Path(os.environ.get('AST_INDEX_TEST_MCP_BINARY', repository / 'target/release/ast-index-mcp')).resolve()
            runner = Runner(binary, directory)
            default, requested = directory / 'default', directory / 'requested'
            for root, name in ((default, 'DefaultOnly'), (requested, 'RequestedOnly')):
                (root / '.git').mkdir(parents=True)
                (root / (name + '.java')).write_text(f'public class {name} {{}}\n')
                runner.command('rebuild', '--force', cwd=root, environment={'AST_INDEX_ROOT': str(root)})
            requests = []
            for identity, root in enumerate((None, str(requested), 'requested',
                                             str(directory / 'missing'), str(requested / 'RequestedOnly.java')), 1):
                arguments = {'pattern': '*', 'format': 'json'}
                if root is not None:
                    arguments['project_root'] = root
                requests.append({'jsonrpc': '2.0', 'id': identity, 'method': 'tools/call',
                                 'params': {'name': 'class', 'arguments': arguments}})
            with (directory / 'helper.stdout.log').open('wb') as stdout, \
                    (directory / 'helper.stderr.log').open('wb') as stderr:
                result = subprocess.run([str(helper)], cwd=directory,
                    env={**runner.environment, 'AST_INDEX_ROOT': str(default), 'AST_INDEX_BIN': str(binary)},
                    input=''.join(json.dumps(row) + '\n' for row in requests).encode(),
                    stdout=stdout, stderr=stderr, timeout=15)
            self.assertEqual(result.returncode, 0)
            log = directory / 'helper.stdout.log'
            self.assertLess(log.stat().st_size, 1024 * 1024)
            with log.open() as stream:
                responses = {row['id']: row['result'] for line in stream if (row := json.loads(line))}
            self.assertEqual(set(responses), {1, 2, 3, 4, 5})
            for identity, name in ((1, 'DefaultOnly'), (2, 'RequestedOnly'), (3, 'RequestedOnly')):
                with self.subTest(identity=identity):
                    self.assertFalse(responses[identity]['isError'])
                    value = json.loads(responses[identity]['content'][0]['text'])
                    self.assertEqual([(row['name'], row['path'], row['line']) for row in value['items']],
                                     [(name, name + '.java', 1)])
                    self.assertEqual(value['pagination']['total'], 1)
            for identity in (4, 5):
                self.assertTrue(responses[identity]['isError'])


if __name__ == '__main__':
    unittest.main()
