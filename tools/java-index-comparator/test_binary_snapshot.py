import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from build_index import capture_binary, freeze_binary
from common import ToolError, file_sha256


class BinarySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def test_a_concurrent_build_cannot_replace_the_pinned_executable(self):
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
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
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
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
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            root = Path(temporary)
            binary = root / "build-output"
            binary.write_bytes(b"original")
            digest = file_sha256(binary)
            snapshot = freeze_binary(binary, root / "epoch", digest)
            snapshot.write_bytes(b"tampered")
            with self.assertRaisesRegex(ToolError, "differs"):
                freeze_binary(binary, root / "epoch", digest)

    def test_capture_reuses_content_but_rejects_a_tampered_snapshot(self):
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            root = Path(temporary)
            binary = root / 'build-output'
            binary.write_bytes(b'original')
            snapshot, digest = capture_binary(binary, root / 'binaries')
            self.assertEqual(digest, file_sha256(binary))
            self.assertEqual(capture_binary(binary, root / 'binaries'), (snapshot, digest))
            binary.write_bytes(b'rebuilt')
            changed, changed_digest = capture_binary(binary, root / 'binaries')
            self.assertNotEqual(changed_digest, digest)
            self.assertNotEqual(snapshot, changed)
            self.assertEqual(snapshot.read_bytes(), b'original')
            snapshot.write_bytes(b'tampered')
            binary.write_bytes(b'original')
            with self.assertRaisesRegex(ToolError, 'differs'):
                capture_binary(binary, root / 'binaries')
            self.assertFalse(any(path.name.startswith('.binary-') for path in (root / 'binaries').iterdir()))

    def test_capture_rejects_an_in_place_write_even_with_restored_mtime(self):
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            root = Path(temporary)
            binary = root / 'build-output'
            binary.write_bytes(b'original')
            stamp = binary.stat()
            original_fsync = os.fsync

            def mutate(descriptor):
                binary.write_bytes(b'changed!')
                os.utime(binary, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                original_fsync(descriptor)

            with patch('build_index.os.fsync', mutate):
                with self.assertRaisesRegex(ToolError, 'changed in place'):
                    capture_binary(binary, root / 'binaries')
            self.assertEqual(list((root / 'binaries').iterdir()), [])

    def test_capture_keeps_the_opened_generation_when_a_build_replaces_the_path(self):
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            root = Path(temporary)
            binary = root / 'build-output'
            replacement = root / 'replacement'
            binary.write_bytes(b'original')
            replacement.write_bytes(b'rebuilt')
            original_fsync = os.fsync

            def replace(descriptor):
                os.replace(replacement, binary)
                original_fsync(descriptor)

            with patch('build_index.os.fsync', replace):
                snapshot, digest = capture_binary(binary, root / 'binaries')
            self.assertEqual(snapshot.read_bytes(), b'original')
            self.assertEqual(digest, file_sha256(snapshot))
            self.assertEqual(binary.read_bytes(), b'rebuilt')
            self.assertNotEqual(digest, file_sha256(binary))
            self.assertFalse(any(path.name.startswith('.binary-') for path in (root / 'binaries').iterdir()))
