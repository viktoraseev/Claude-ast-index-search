"""Legacy Maven Java resource lookup stays inside declared owner scopes."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from android_contracts import observation
from root_contracts import Runner


class JavaResourceMavenOwnershipTests(unittest.TestCase):
    def test_declared_legacy_owners_do_not_become_workspace_fallbacks(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        cases = [('declared', ['library'], 1), ('no-edge', [], 0),
                 ('ambiguous', ['library', 'other'], 0), ('nontransitive', ['library'], 0),
                 ('shadow', ['library'], 0), ('sibling', ['library'], 0)]
        for label, dependencies, count in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(dir=artifacts) as temporary:
                runner = Runner(binary, Path(temporary))
                runner.root.mkdir()
                runner.environment['AST_INDEX_ROOT'] = str(runner.root)

                def write(path, content):
                    destination = runner.root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content)

                def module(name, edges):
                    xml = ''.join('<dependency><groupId>fixture</groupId><artifactId>' + edge
                                  + '</artifactId></dependency>' for edge in edges)
                    write(name + '/pom.xml', '<project><groupId>fixture</groupId><artifactId>'
                          + name + '</artifactId><dependencies>' + xml + '</dependencies></project>')

                module('app', dependencies)
                for owner in ('library', 'other') if label == 'ambiguous' else ('library',):
                    module(owner, [])
                    write(owner + '/Sentinel.java', 'package fixture.' + owner + '; class Sentinel {}')
                    write(owner + '/res/values/strings.xml', '<resources><string name="tail">Value</string></resources>')
                body = 'package app;\nclass Work {\n int value = R.string.tail;\n}\n'
                if label == 'shadow':
                    body += 'class R { static class string { static int tail; } }\n'
                if label == 'sibling':
                    body = 'package app; class Work {}\n'
                    module('app-extra', ['library'])
                    write('app-extra/Extra.java', 'package app;\nclass Extra {\n int value = R.string.tail;\n}\n')
                if label == 'nontransitive':
                    write('gradle.properties', 'android.nonTransitiveRClass=true\n')
                write('app/Work.java', body)
                # Ground representative positive and authored-shadow sources.
                # Generated stubs are compiler inputs, never indexed sources.
                if label in ('declared', 'shadow'):
                    javac = shutil.which('javac')
                    self.assertIsNotNone(javac, 'JDK required for Java source validation')
                    sources = [str(runner.root / 'app/Work.java')]
                    if label == 'declared':
                        stub = Path(temporary) / 'R.java'
                        stub.write_text('package app; public class R { public static class string { public static final int tail=1; } }')
                        sources.append(str(stub))
                    with (Path(temporary) / 'javac.log').open('wb') as log:
                        process = subprocess.run([javac, '-proc:none', '-d', str(Path(temporary) / 'classes'),
                                                  *sources], stdout=log, stderr=log, timeout=30)
                    self.assertEqual(process.returncode, 0, 'Authored Java did not compile; private javac log retained')
                runner.command('rebuild', '--force', '--max-files', 0)
                _, output = runner.command('resource-usages', 'tail', '--module', 'app')
                expected = {**observation(''), 'total': count}
                if count:
                    expected.update(locations=[('app/Work.java', 3)], groups=[('Kotlin/Java', 1)])
                self.assertEqual(observation(output), expected)
                if dependencies:
                    _, output = runner.command('unused-deps', 'app')
                    summary = re.search(r'^Total: (\d+) unused, (\d+) exported, (\d+) used of (\d+) dependencies$', output, re.M)
                    self.assertIsNotNone(summary)
                    self.assertEqual(tuple(map(int, summary.groups())), (len(dependencies) - count, 0, count, len(dependencies)))
                if count:
                    _, output = runner.command('unused-deps', 'app', '--no-resources')
                    self.assertIn('Total: 1 unused, 0 exported, 0 used of 1 dependencies', output)
                if label == 'sibling':
                    _, output = runner.command('resource-usages', 'tail', '--module', 'app-extra')
                    self.assertEqual(observation(output), {**observation(''), 'locations': [('app-extra/Extra.java', 3)],
                                    'groups': [('Kotlin/Java', 1)], 'total': 1})


if __name__ == '__main__':
    unittest.main()
