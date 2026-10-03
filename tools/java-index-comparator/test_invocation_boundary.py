"""Every target invocation overrides a caller's unrelated AST_INDEX_ROOT."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA
import build_index
import collect
from common import connect


class InvocationBoundaryTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        self.database = self.directory / 'index.sqlite'
        self.state = connect(self.database)
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)

    def test_fixture_always_pins_its_exact_target(self):
        with patch.dict(os.environ, {'AST_INDEX_ROOT': str(self.directory / 'unrelated')}):
            fixture = Fixture(self.root, Path('unused-binary'), self.database, self.state, Mock())
        self.assertEqual(fixture.environment['AST_INDEX_ROOT'], str(self.root))
        self.assertEqual(fixture.environment['AST_INDEX_DB_PATH'], str(self.database))

    def test_native_rebuilder_overrides_inherited_root(self):
        with patch.dict(os.environ, {'AST_INDEX_ROOT': str(self.directory / 'unrelated')}), \
                patch.object(build_index.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            build_index.build_ast_index('unused-binary', self.root, self.database, 'snapshot', rebuild=True)
        self.assertEqual(command.call_args.kwargs['cwd'], self.root)
        self.assertEqual(command.call_args.kwargs['env']['AST_INDEX_ROOT'], str(self.root))

    def test_legacy_collector_rebuild_cannot_inherit_another_project(self):
        with patch.dict(os.environ, {'AST_INDEX_ROOT': str(self.directory / 'unrelated')}), \
                patch.object(collect.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            collect.build_ast_index('unused-binary', self.root, self.database, 'snapshot', 1, True)
        self.assertEqual(command.call_args.kwargs['cwd'], self.root)
        self.assertEqual(command.call_args.kwargs['env']['AST_INDEX_ROOT'], str(self.root))


if __name__ == '__main__':
    unittest.main()
