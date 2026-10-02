import unittest
from pathlib import Path
from unittest.mock import patch

import cycle


class DriverReloadTests(unittest.TestCase):
    def test_entrypoint_reloads_with_identical_arguments(self):
        script = str(Path(cycle.__file__).resolve())
        arguments = [script, '--project-root', 'project', '--output-dir', 'artifacts']
        with patch.dict(cycle.os.environ, {}), patch.object(cycle.sys, 'argv', arguments), patch.object(cycle.os, 'execv') as execute:
            cycle.reload_driver()
            self.assertEqual(cycle.os.environ['AST_INDEX_CYCLE_COMPLETED_ROUNDS'], '0')
        execute.assert_called_once_with(cycle.sys.executable, [cycle.sys.executable, *arguments])

    def test_imported_driver_does_not_replace_its_caller(self):
        with patch.object(cycle.sys, 'argv', ['unittest']), patch.object(cycle.os, 'execv') as execute:
            cycle.reload_driver()
        execute.assert_not_called()

    def test_reload_preserves_completed_rounds_for_max_rounds(self):
        script = str(Path(cycle.__file__).resolve())
        with patch.dict(cycle.os.environ, {}), patch.object(cycle.sys, 'argv', [script]), patch.object(cycle.os, 'execv'):
            cycle.reload_driver(completed=2)
            self.assertEqual(cycle.os.environ['AST_INDEX_CYCLE_COMPLETED_ROUNDS'], '2')


if __name__ == '__main__':
    unittest.main()
