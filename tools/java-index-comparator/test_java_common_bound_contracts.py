"""Current production regression, with javac-checked positive and negative inputs."""
import os
from pathlib import Path
import unittest

from common import canonical_json
import java_common_bound_contracts as contracts


class CommonBoundTests(unittest.TestCase):
    def test_common_source_bound_dependency_ownership(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        expected, actual = contracts.exercise(binary, base)
        failed = [key for key in expected if canonical_json(expected[key]) != canonical_json(actual.get(key))]
        self.assertEqual(failed, [])


if __name__ == '__main__':
    unittest.main()
