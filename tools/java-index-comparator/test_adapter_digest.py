"""A changed comparison contract must invalidate the resumable audit epoch."""
from pathlib import Path
import unittest
from unittest.mock import patch

from common import adapter_digest


class AdapterDigestTests(unittest.TestCase):
    def test_module_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'module_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)

    def test_annotation_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'annotation_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)

    def test_lifecycle_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'lifecycle_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)

    def test_perl_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'perl_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)

    def test_mobile_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes

        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'mobile_contracts.py' else content

        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


    def test_owned_mutation_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        for name in ('install_contracts.py', 'root_contracts.py', 'profile_contracts.py'):
            with self.subTest(contract=name):
                def changed(path):
                    content = read(path)
                    return content + b'\n# changed contract\n' if path.name == name else content

                with patch.object(Path, 'read_bytes', changed):
                    self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
