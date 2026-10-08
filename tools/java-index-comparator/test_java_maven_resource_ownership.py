"""Authored Maven R ownership and javac checks; no MCP equivalence claim."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from android_contracts import observation
from root_contracts import Runner


class JavaMavenResourceOwnershipTests(unittest.TestCase):
    def test_legacy_declared_owner_and_explicit_r_modes(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            runner = Runner(binary, Path(temporary))
            runner.root.mkdir()
            (runner.root / '.git').mkdir()

            def write(path, content):
                destination = runner.root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content)

            def pom(module, dependencies=()):
                return ('<project><groupId>fixture</groupId><artifactId>' + module
                        + '</artifactId><dependencies>' + ''.join(
                            '<dependency><groupId>fixture</groupId><artifactId>' + name
                            + '</artifactId></dependency>' for name in dependencies)
                        + '</dependencies></project>')

            for module in ('library', 'decoy'):
                write(module + '/pom.xml', pom(module))
                write(module + '/res/values/strings.xml',
                      '<resources><string name="shared">Value</string></resources>')
            write('app/Use.java', 'package fixture.app; class Use { int value=R.string.shared; }\n')
            # The generated R class is compiler input only, outside the index.
            stub = Path(temporary) / 'R.java'
            javac = shutil.which('javac')
            self.assertIsNotNone(javac)
            for mode, dependencies, count in (
                    ('legacy', ('library',), 1), ('false', ('library',), 1),
                    ('true', ('library',), 0), ('ambiguous', ('library', 'decoy'), 0)):
                with self.subTest(mode=mode):
                    write('app/pom.xml', pom('app', dependencies))
                    properties = runner.root / 'app/gradle.properties'
                    properties.unlink(missing_ok=True)
                    if mode in ('false', 'true'):
                        properties.write_text('android.nonTransitiveRClass=' + mode + '\n')
                    stub.write_text('package fixture.app; public class R { public static class string { '
                                    + ('public static final int shared=1;' if mode != 'true' else '')
                                    + ' } }\n')
                    compiled = subprocess.run(
                        [javac, '-proc:none', '-d', str(Path(temporary) / 'classes'),
                         str(stub), str(runner.root / 'app/Use.java')],
                        capture_output=True, timeout=30)
                    self.assertEqual(compiled.returncode == 0, mode != 'true')
                    runner.command('rebuild', '--force', '--max-files', 0)
                    _, output = runner.command('resource-usages', 'shared', '--module', 'app')
                    expected = {**observation(''), 'total': count}
                    if count:
                        expected.update(locations=[('app/Use.java', 1)], groups=[('Kotlin/Java', 1)])
                    self.assertEqual(observation(output), expected)
                    for module in ('library', 'decoy'):
                        _, output = runner.command('resource-usages', '--unused', '--module', module)
                        unused = [] if module == 'library' and count else ['string/shared']
                        self.assertEqual(observation(output),
                                         {**observation(''), 'unused': unused, 'unused_total': len(unused)})
                    _, output = runner.command('unused-deps', 'app', '--no-xml')
                    self.assertIn(f'  - Resources: {count}\n', output)


if __name__ == '__main__':
    unittest.main()
