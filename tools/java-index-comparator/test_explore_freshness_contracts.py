"""Execute Java exploration on compiled synthetic sources, without an MCP double."""
import os
from pathlib import Path
import unittest

import explore_freshness_contracts as contracts


class ExploreFreshnessTests(unittest.TestCase):
    def test_receiver_ownership_is_independent_of_stored_graph_freshness(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        expected, actual = contracts.exercise(binary, Path(__file__).resolve().parents[2] / '.artifacts/tests')
        mismatches = [key for key in expected if expected[key] != actual.get(key)]
        self.assertEqual([], mismatches)


if __name__ == '__main__':
    unittest.main()
