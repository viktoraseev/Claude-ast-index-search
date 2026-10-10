"""Compact production tests for the Java resource parent's definition/access gaps."""
import os
from pathlib import Path
import tempfile
import unittest

import java_resource_definition_contracts as contracts


class JavaResourceDefinitionTests(unittest.TestCase):
    def test_file_kinds_and_access_guards_execute_production(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=base) as directory:
            expected, actual = contracts.exercise(binary, Path(directory))
        want, got = expected[contracts.FEATURE], actual[contracts.FEATURE]
        self.assertEqual([key for key in want if want[key] != got[key]], [])


if __name__ == '__main__':
    unittest.main()
