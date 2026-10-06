"""Production receiver-site regressions and applicability guards."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, adapter_digest
import java_same_line_site_contracts as sites


class SameLineSiteContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_same_line_sites_match_authored_identities(self):
        expected, actual = sites.exercise(self.binary, self.directory)
        differences = {feature: [key for key, want in expected[feature].items()
                                 if actual[feature].get(key) != want] for feature in sorted(sites.FEATURES)}
        self.assertEqual(differences, {feature: [] for feature in sorted(sites.FEATURES)})

    def test_incomplete_inventory_and_external_mutation_are_errors(self):
        inventory = sites.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(sites.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                sites.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            sites.exercise(self.binary, '/private/tmp')

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# byte-site fixture edit\n' if path.name == 'java_same_line_site_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
