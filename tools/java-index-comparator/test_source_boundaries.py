"""A source snapshot must not hide inaccessible files or escape its root."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, java_files, source_snapshot


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()

    def test_external_source_link_is_rejected_before_opening_its_content(self):
        target = self.directory / 'outside.java'
        target.write_text('class Outside {}\n')
        link = self.root / 'Alias.java'
        link.symlink_to(target)
        original = Path.open

        def guarded_open(path, *args, **kwargs):
            if path in {target, link}:
                raise AssertionError('source outside the selected root was read')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', guarded_open):
            with self.assertRaisesRegex(ToolError, 'outside target'):
                source_snapshot(self.root)

    def test_links_within_the_selected_root_are_still_in_scope(self):
        (self.root / 'Actual.java').write_text('class Actual {}\n')
        (self.root / 'Alias.java').symlink_to('Actual.java')
        _, files = source_snapshot(self.root)
        self.assertEqual([file['path'] for file in files], ['Actual.java', 'Alias.java'])
        self.assertEqual(files[0]['sha256'], files[1]['sha256'])

    def test_directory_read_failure_cannot_become_an_empty_successful_snapshot(self):
        with patch('common.os.scandir', side_effect=PermissionError('synthetic inaccessible directory')):
            with self.assertRaises(PermissionError):
                source_snapshot(self.root)

    def test_directory_links_are_not_recursively_followed(self):
        outside = self.directory / 'outside'
        outside.mkdir()
        (outside / 'Outside.java').write_text('class Outside {}\n')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        self.assertEqual(list(java_files(self.root)), [])


if __name__ == '__main__':
    unittest.main()
