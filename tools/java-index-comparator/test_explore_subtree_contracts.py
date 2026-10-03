"""Root-scoped Java seed budgets execute the production CLI, not MCP."""
import os
from pathlib import Path
import tempfile
import unittest

from root_contracts import Runner


class ExplorationSubtreeBudgetsTests(unittest.TestCase):
    def test_selected_root_is_filtered_before_seed_caps(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(prefix='explore-root-cap-', dir=artifacts) as temporary:
            runner = Runner(binary, Path(temporary).resolve())
            runner.root.mkdir()
            runner.environment['AST_INDEX_ROOT'] = str(runner.root)
            # The authored sources exceed every lexical seed cap. Equal names
            # make primary-root rows fill the old unscoped insertion-order pool.
            for number in range(220):
                (runner.root / f'Noise{number:03}.java').write_text('class Pulse {}\n')
            attached = runner.directory / 'attached'
            attached.mkdir()
            (attached / 'Target.java').write_text('class Pulse {}\n')
            runner.command('rebuild', '--force')
            runner.command('subtree', 'add', 'attached', attached)
            runner.command('rebuild', '--force')
            for flags in ((), ('--rwr',)):
                with self.subTest(flags=flags):
                    doc = runner.json('--subtree', 'attached', 'explore', 'pulse', *flags)
                    rows = sorted((runner.path(row['path']), row['name'], row['line'])
                                  for row in doc['symbols'])
                    self.assertEqual(rows, [('attached/Target.java', 'Pulse', 1)])
                    self.assertEqual([runner.path(row['path']) for row in doc['files']],
                                     ['attached/Target.java'])
                    self.assertEqual(doc['neighbours'], [])


if __name__ == '__main__':
    unittest.main()
