"""Injection truth is indexed once per fixture, not rescanned per query."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA
from build_index import build_ast_index
from common import ToolError, connect


class InjectionCacheTests(unittest.TestCase):
    def test_queries_share_one_source_pass_and_new_fixture_refreshes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            example = root / 'Example.java'
            example.write_text('class Example { @Inject Service service; @Autowired Other other; }')
            (root / 'Types.java').write_text('class Service {} class Other {}')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'injection-cache')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(root, binary, database, state, None)
                read_structure = fixture.structure
                attempts = 0

                def interrupted(file):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 2:
                        raise ToolError('simulated interrupted index build')
                    return read_structure(file)

                with patch.object(fixture, 'structure', side_effect=interrupted):
                    with self.assertRaises(ToolError):
                        fixture.injection_check({'subject': 'Service'})
                # A partial, committed derived index must be rebuilt, not used.
                with patch.object(fixture, 'structure', wraps=fixture.structure) as read:
                    for name in ('Service', 'Other', 'Missing'):
                        _, _, expected, actual = fixture.injection_check({'subject': name})
                        self.assertEqual(expected, actual)
                        self.assertEqual(bool(expected), name != 'Missing')
                    self.assertEqual(read.call_count, 2)
                # Persisted derived rows must never become cross-invocation truth.
                example.write_text('class Example { @Inject Missing added; }')
                fresh = Fixture(root, binary, database, state, None)
                for name in ('Service', 'Missing'):
                    _, _, expected, actual = fresh.injection_check({'subject': name})
                    self.assertEqual(expected, actual)
                    self.assertEqual(bool(expected), name == 'Missing')
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
