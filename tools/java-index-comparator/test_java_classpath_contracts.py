"""Production duplicate source ownership and fail-closed Java audit planning."""
import os
from pathlib import Path
import tempfile
import unittest

from audit import SCHEMA
from common import canonical_json, connect
import java_classpath_contracts as contracts
import mobile_contracts


class JavaClasspathTests(unittest.TestCase):
    def test_duplicate_classpath_ownership(self):
        expected, actual = contracts.exercise(Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')),
            Path(__file__).resolve().parents[2] / '.artifacts/tests')
        self.assertEqual([key for key in expected if canonical_json(expected[key]) != canonical_json(actual.get(key))], [])

    def test_mixed_inventory_does_not_skip_java(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary) / 'root'
            root.mkdir()
            (root / 'Probe.java').write_text('class Probe {}')
            (root / 'AndroidManifest.xml').write_text('<manifest/>')
            state = connect(Path(temporary) / 'inventory.sqlite')
            try:
                state.executescript(SCHEMA)
                mobile_contracts.inventory(state, root)
                contracts.plan_contracts(state, root)
                self.assertEqual(tuple(state.execute('SELECT status,verdict FROM checks WHERE feature=?',
                                                     (contracts.FEATURE,)).fetchone()), ('pending', None))
                self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
