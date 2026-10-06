"""Production red/green checks for Java chained and generic receiver sites."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, adapter_digest
import java_receiver_site_contracts as sites


class ReceiverSiteContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_chained_generic_sites_match_source_identities(self):
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

    def test_external_fixture_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n// site fixture edit\n' if path.name == 'JavaReceiverSiteProbe.java' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
