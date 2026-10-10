"""Java metadata acceptance checks production resource ownership, not JSON shape."""
import os
from pathlib import Path
import tempfile
import unittest

import java_resource_metadata_contracts as contracts


class JavaResourceMetadataTests(unittest.TestCase):
    def test_declaring_resources_and_constant_namespaces(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=base) as directory:
            expected, actual = contracts.exercise(binary, Path(directory))
        want, got = expected[contracts.FEATURE], actual[contracts.FEATURE]
        self.assertEqual([key for key in want if want[key] != got[key]], [])


if __name__ == '__main__':
    unittest.main()
