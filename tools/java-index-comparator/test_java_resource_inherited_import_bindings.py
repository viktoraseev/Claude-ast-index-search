"""Compact production regressions for inherited and imported Java R shadows."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import java_resource_binding_contracts as contracts
import android_dependency_contracts
from audit import SCHEMA
from common import connect, stable_id


CASES = {
    'inherited-field': ('import fixture.library.R;', '''class Base {
 Shadow R = new Shadow();
}
class Use extends Base { int shadow() { return R.string.hit; } }
class Other { int real() { return R.string.hit; } }
''', [5]),
    'imported-field': (
        'import static plain.string.hit;\nimport static fixture.library.R.string.*;',
        '''class Use { int shadow() { return hit; } }
''', []),
}


class JavaResourceInheritedImportBindingsTests(unittest.TestCase):
    def test_fresh_java_plan_retains_legacy_ids_without_counting_xml_as_passed(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            state = connect(directory / 'scope.sqlite')
            try:
                state.executescript(SCHEMA)
                android_dependency_contracts.plan_dependencies(state, directory, java_only=True)
                for feature in android_dependency_contracts.LEGACY_FEATURES:
                    with self.subTest(feature=feature):
                        identity = stable_id({'feature': feature,
                            'subject': 'disposable-java-android-dependency-ownership'})
                        row = state.execute('SELECT verdict FROM checks WHERE id=?', (identity,)).fetchone()
                        self.assertIsNotNone(row, 'scope migration must preserve legacy case IDs')
                        self.assertNotEqual(row['verdict'], 'pass')
                        self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?',
                                                       (feature,)).fetchone()[0], 'out-of-scope')
            finally:
                state.close()

    def test_inherited_and_explicit_nonresource_fields_keep_resource_ownership(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

        class DeclaredDependencyRunner(contracts.Runner):
            def command(self, *arguments, **kwargs):
                if arguments and arguments[0] == 'rebuild':
                    build = self.root / 'app/build.gradle'
                    dependency = 'dependencies { implementation(project(":library")) }\n'
                    content = build.read_text()
                    if dependency not in content:
                        build.write_text(content + dependency)
                return super().command(*arguments, **kwargs)

        # javac and the indexed module graph must agree that the imported
        # source owner is visible to app. Do not infer member kinds from an
        # unrelated module omitted from the consumer's classpath.
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary, \
                patch.dict(contracts.CASES, CASES, clear=True), \
                patch.object(contracts, 'Runner', DeclaredDependencyRunner):
            expected, actual = contracts.exercise(binary, Path(temporary))
        want, got = expected[contracts.FEATURE], actual[contracts.FEATURE]
        self.assertEqual(want['javac'], got['javac'])
        self.assertEqual(want['inventory'], got['inventory'])
        # Both whole results and unused ownership must agree. The positive
        # unrelated-class site prevents a fix that drops every R expression.
        for label in (*CASES, 'unused:app', 'unused:library'):
            with self.subTest(label=label):
                self.assertEqual(want[label], got[label])


if __name__ == '__main__':
    unittest.main()
