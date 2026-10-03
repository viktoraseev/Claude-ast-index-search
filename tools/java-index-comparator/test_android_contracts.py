"""Java resource ownership checks execute the CLI without an MCP oracle."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA
from common import ToolError, connect
import android_contracts
import mobile_contracts


class AndroidContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('Android fixture has no MCP equivalence oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused.sqlite', self.state, oracle)

    def test_production_java_ownership_filters_unused_and_caps(self):
        android_contracts.plan_android(self.state, self.root)
        for feature in sorted(android_contracts.FEATURES):
            check = self.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?',
                                      (feature, 'disposable-java-android')).fetchone()
            self.fixture.evaluate(check)
            result = self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
            diff = json.loads(result['diff_json'] or '{}')
            self.assertEqual(result['verdict'], 'pass', {'feature': feature, 'error': result['error'],
                             'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertEqual([p.name for p in self.root.iterdir()], ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_full_inventory_presence_never_becomes_false_absence(self):
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'inapplicable')
        for path, content in (
                ('ignored/res/values-fr/strings.xml', '<resources/>'),
                ('ignored/AndroidManifest.xml', '<manifest/>'),
                ('build.gradle', "plugins { id 'com.android.library' }"),
                ('Foreign.kt', 'import android.view.View'),
                ('Use.java', 'class Use { int x = R.string.title; }'),
                ('Raw.java', 'class Raw { int x = R.raw.sound; }')):
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
            mobile_contracts.inventory(self.state, self.root)
            self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
            destination.unlink()
        link = self.root / 'unfollowed'
        link.symlink_to(self.directory, target_is_directory=True)
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
        self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='android_applicability_sha256'").fetchone())

    def test_absence_requires_inventory_and_bounded_unchanged_sources(self):
        self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
        source = self.root / 'Large.xml'
        with source.open('wb') as stream:
            stream.truncate(4 * 1024 * 1024 + 1)
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
        source.unlink()
        mobile_contracts.inventory(self.state, self.root)
        (self.root / 'Sentinel.java').write_text('class Changed {}\n')
        with self.assertRaisesRegex(ToolError, 'changed after inventory'):
            android_contracts.applicability(self.state, self.root)
        source = self.root / 'Marker.xml'
        source.write_text('<first/>')
        mobile_contracts.inventory(self.state, self.root)
        stamp = source.stat()
        source.write_text('<other/>')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaisesRegex(ToolError, 'fingerprint changed'):
            android_contracts.applicability(self.state, self.root)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            android_contracts.exercise(self.binary, Path('/private/tmp'))

    def test_corrupt_results_fail_and_applicable_target_stays_pending(self):
        (self.root / 'AndroidManifest.xml').write_text('<manifest/>')
        android_contracts.plan_android(self.state, self.root)
        for feature in android_contracts.FEATURES:
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertIn('not MCP equivalence', row['reason'])
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature + ':target',)).fetchone()[0], 'pending')
            check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            self.fixture._android_results = None
            with patch('android_contracts.exercise', return_value=(
                    {f: {'count': 1} for f in android_contracts.FEATURES},
                    {f: {'count': 0} for f in android_contracts.FEATURES})):
                self.fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='android:syntax-resolution'").fetchone()[0], 'pending')

    def test_preserved_stat_marker_edits_invalidate_absence_before_native_checks(self):
        for name in ('gradle.properties', 'libs.versions.toml', 'plugins/android.gradle',
                     'build/Generated.java'):
            with self.subTest(input=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                before, after = (('class Generated { /* desktop.feature */ }\n',
                                  'class Generated { /* android.feature */ }\n') if path.suffix == '.java'
                                 else ('desktop.feature = true\n', 'android.feature = true\n'))
                path.write_text(before)
                mobile_contracts.inventory(self.state, self.root)
                self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'inapplicable')
                stamp = path.stat()
                path.write_text(after)
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(path.stat().st_size, stamp.st_size)
                self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
                with self.assertRaisesRegex(ToolError, 'fingerprint changed'):
                    android_contracts.applicability(self.state, self.root)
                self.assertIsNone(self.state.execute("SELECT value FROM metadata WHERE key='android_applicability_sha256'").fetchone())
                mobile_contracts.inventory(self.state, self.root)
                self.assertEqual(android_contracts.applicability(self.state, self.root)[0], 'pending')
                path.unlink()


if __name__ == '__main__':
    unittest.main()
