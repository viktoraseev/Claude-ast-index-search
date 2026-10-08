"""Java resource totals stay complete while rendered locations stay bounded."""
import os
from pathlib import Path
import tempfile
import unittest

from android_contracts import observation
from root_contracts import Runner


class JavaResourceUsageTotalsTests(unittest.TestCase):
    def test_more_than_one_hundred_java_sites_keep_true_totals_and_omissions(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            runner = Runner(binary, Path(temporary))
            runner.root.mkdir()
            runner.environment['AST_INDEX_ROOT'] = str(runner.root)
            for module in ('app', 'library'):
                directory = runner.root / module
                directory.mkdir()
                (directory / 'build.gradle').write_text("plugins { id 'com.android.library' }\n"
                    + "android { namespace 'fixture." + module + "' }\n"
                    + ('dependencies { implementation(project(":library")) }\n' if module == 'app' else ''))
            values = runner.root / 'library/src/main/res/values'
            values.mkdir(parents=True)
            (values / 'strings.xml').write_text('<resources><string name="shared">Value</string></resources>')
            count = 128
            paths = ['app/Case' + str(index) + '.java' for index in range(count)]
            for index, path in enumerate(paths):
                (runner.root / path).write_text('package fixture.app;\nimport fixture.library.R;\n'
                    + 'class Case' + str(index) + ' { int value = R.string.shared; }\n')
            runner.command('rebuild', '--force', '--max-files', 0)
            for flags, expected_count in (((), count), (('--module', 'app'), count),
                                          (('--module', 'library'), 0)):
                with self.subTest(flags=flags):
                    _, output = runner.command('resource-usages', '@string/shared', *flags)
                    actual = observation(output)
                    expected = {**observation(''), 'total': expected_count}
                    if expected_count:
                        expected.update(groups=[('Kotlin/Java', count)], omitted=[count - 10],
                                        locations=[(path, 3) for path in sorted(paths)[:10]])
                    self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
