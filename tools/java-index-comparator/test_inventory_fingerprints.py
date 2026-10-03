"""Auxiliary Java inputs invalidate evidence even on coarse/preserved timestamps."""
import os
from pathlib import Path
import tempfile
import unittest

from common import connect, file_sha256
from audit import SCHEMA
from mobile_contracts import inventory, inventory_snapshot


class InventoryFingerprintTests(unittest.TestCase):
    def test_descriptor_content_change_cannot_reuse_the_same_audit_epoch(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            root = Path(temporary)
            for name in ('pom.xml', 'build.gradle', 'settings.gradle', 'ya.make',
                         'gradle.properties', 'libs.versions.toml', 'plugins/android.gradle',
                         'build/Generated.java'):
                with self.subTest(descriptor=name):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'public-synthetic-one\n')
                    stamp = path.stat()
                    before = inventory_snapshot(root)
                    path.write_bytes(b'public-synthetic-two\n')
                    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    self.assertEqual(path.stat().st_size, stamp.st_size)
                    self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
                    self.assertNotEqual(inventory_snapshot(root), before)

    def test_descriptor_hash_is_persisted_in_the_private_inventory(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            path = root / 'pom.xml'
            path.write_text('<project><artifactId>public-probe</artifactId></project>\n')
            state = connect(directory / 'inventory.sqlite')
            try:
                state.executescript(SCHEMA)
                inventory(state, root)
                self.assertEqual(state.execute("SELECT sha256 FROM file_inventory WHERE path='pom.xml'").fetchone()[0],
                                 file_sha256(path))
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
