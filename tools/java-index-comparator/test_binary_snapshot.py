from pathlib import Path
import tempfile
import unittest

from build_index import freeze_binary
from common import ToolError, file_sha256


class BinarySnapshotTests(unittest.TestCase):
    def test_a_concurrent_build_cannot_replace_the_pinned_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build-output"
            binary.write_bytes(b"original")
            digest = file_sha256(binary)
            snapshot = freeze_binary(binary, root / "epoch", digest)
            binary.write_bytes(b"rebuilt")
            self.assertEqual(snapshot.read_bytes(), b"original")
            self.assertEqual(freeze_binary(binary, root / "epoch", digest), snapshot)
            self.assertTrue(snapshot.stat().st_mode & 0o100)

    def test_a_changed_build_is_rejected_before_the_snapshot_is_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build-output"
            binary.write_bytes(b"original")
            digest = file_sha256(binary)
            binary.write_bytes(b"changed")
            with self.assertRaisesRegex(ToolError, "changed while"):
                freeze_binary(binary, root / "epoch", digest)
            self.assertFalse((root / "epoch/ast-index").exists())
            self.assertEqual(list((root / "epoch").iterdir()), [])

    def test_a_modified_existing_snapshot_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build-output"
            binary.write_bytes(b"original")
            digest = file_sha256(binary)
            snapshot = freeze_binary(binary, root / "epoch", digest)
            snapshot.write_bytes(b"tampered")
            with self.assertRaisesRegex(ToolError, "differs"):
                freeze_binary(binary, root / "epoch", digest)
