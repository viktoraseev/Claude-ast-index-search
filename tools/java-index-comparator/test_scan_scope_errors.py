"""Selected Java searches must not fail on excluded Java source reads."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from root_contracts import Runner


class ScanScopeErrorTests(unittest.TestCase):
    def test_excluded_invalid_java_does_not_break_selected_content_search(self):
        boundary = Path(__file__).resolve().parents[2] / '.artifacts'
        boundary.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='scan-scope-regression-', dir=boundary) as temporary:
            runner = Runner(Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')),
                            Path(temporary).resolve())
            runner.root.mkdir()
            (runner.root / '.git').mkdir()
            good = runner.root / 'Good.java'
            bad = runner.root / 'Bad.java'
            good.write_text('// scopeProbe\nclass Good {}\n')
            bad.write_text('// scopeProbe\nclass Bad {}\n')
            runner.environment.update(AST_INDEX_ROOT=str(runner.root),
                                      AST_INDEX_DB_PATH=str(runner.directory / 'index.sqlite'))
            runner.command('rebuild', '--force')
            bad.write_bytes(b'// scopeProbe \xff\nclass Bad {}\n')
            before = good.read_bytes(), bad.read_bytes()
            for fmt in ('text', 'json'):
                for suffix in (False, True):
                    with self.subTest(format=fmt, suffix=suffix):
                        arguments = ['search', 'scopeProbe', '--in-file', 'Good.java', '--limit', '100']
                        flags = ['--format', fmt]
                        code, output = runner.command(
                            *(arguments + flags if suffix else flags + arguments), acceptable=(0, 1))
                        self.assertEqual(code, 0, 'excluded Java file must not fail selected search')
                        self.assertIn('Good.java', output)
                        self.assertNotIn('Bad.java', output)
                        if fmt == 'json':
                            self.assertIsInstance(json.loads(output), dict)
            self.assertEqual(before, (good.read_bytes(), bad.read_bytes()))


if __name__ == '__main__':
    unittest.main()
