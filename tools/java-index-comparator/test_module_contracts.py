"""Small Maven/Java production regressions, with independent XML expectations."""
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect
import mobile_contracts
import module_contracts


class ModuleContracts(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '--template=', str(self.root)], capture_output=True, check=True)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.database = self.directory / 'index.sqlite'
        self.state = connect(self.directory / 'checks.sqlite')
        self.state.executescript(SCHEMA)
        self.fixture = Fixture(self.root, self.binary, self.database, self.state, None)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def write(self, path, body):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body)

    def prepare(self):
        build_ast_index(str(self.binary), self.root, self.database, 'modules', rebuild=True)
        mobile_contracts.inventory(self.state, self.root)
        module_contracts.plan_modules(self.state, self.root)

    def evaluate(self, feature, verdict='pass'):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        result = self.state.execute('SELECT verdict,error FROM checks WHERE id=?', (check['id'],)).fetchone()
        self.assertEqual(result[0], verdict, tuple(result))
        return check

    def test_completed_edgeless_java_graph_is_not_unindexed(self):
        self.write('pom.xml', '''<project xmlns="http://maven.apache.org/POM/4.0.0">
<groupId>fixture</groupId><artifactId>single</artifactId>
<dependencies><dependency><groupId>external</groupId><artifactId>library</artifactId></dependency></dependencies>
</project>''')
        self.write('Example.java', 'class Example {}')
        self.prepare()
        for feature in sorted(module_contracts.FEATURES):
            with self.subTest(feature=feature):
                check = self.evaluate(feature)
                source = json.loads(self.state.execute('SELECT expected_json FROM checks WHERE id=?', (check['id'],)).fetchone()[0])['source']
                self.assertIn('not MCP equivalence', source)
        self.assertIn("has no dependencies", self.fixture.text_cli('unused-deps', 'single', '--strict'))
        check = self.evaluate('module-route')
        with patch.object(self.fixture, 'cli', return_value={'from': 'single', 'to': 'single', 'paths': [], 'count': 0, 'truncated': False, 'empty_reason': 'not_indexed'}):
            self.fixture.evaluate(check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_unused_dependencies_keep_java_enums_large_type_sets_and_exact_module_scope(self):
        # One authored fixture family, not one test per project database row.
        for variant in ('enum', 'crowded', 'scope'):
            with self.subTest(variant=variant):
                library, consumer = variant + '-lib', variant + '-consumer'
                dependency = ('<dependencies><dependency><groupId>fixture</groupId>'
                              f'<artifactId>{library}</artifactId></dependency></dependencies>')
                for path, name, deps in (('lib', library, ''), ('consumer', consumer, dependency)):
                    self.write(f'{variant}/{path}/pom.xml', '<project><groupId>fixture</groupId>'
                               f'<artifactId>{name}</artifactId>{deps}</project>')
                if variant == 'enum':
                    declaration = 'public enum Value { ONE }'
                    usage = 'import dep.Value; public class Use { Value value; }'
                elif variant == 'crowded':
                    padding = ''.join(f'public static class Padding{i:03d} {{}}' for i in range(120))
                    declaration = 'public class Value {' + padding + 'public static class Wanted {}}'
                    usage = 'import dep.Value.Wanted; public class Use { Wanted value; }'
                else:
                    declaration = 'public class Value {}'
                    usage = 'public class Use {}'
                    self.write(f'{variant}/consumer-shadow/pom.xml', '<project><groupId>fixture</groupId>'
                               f'<artifactId>{variant}-shadow</artifactId>{dependency}</project>')
                    self.write(f'{variant}/consumer-shadow/Other.java',
                               'package sibling; import dep.Value; public class Other { Value value; }')
                self.write(f'{variant}/lib/Value.java', 'package dep; ' + declaration)
                self.write(f'{variant}/consumer/Use.java', 'package app; ' + usage)
                self.prepare()
                # Nested Maven module identities use their directory path;
                # artifactId supplies dependency coordinates, not CLI names.
                output = self.fixture.text_cli('unused-deps', variant + '.consumer', '--strict')
                summary = re.search(r'^Total: (\d+) unused, (\d+) exported, (\d+) used of (\d+) dependencies$',
                                    output, re.MULTILINE)
                self.assertIsNotNone(summary)
                expected = (1, 0, 0, 1) if variant == 'scope' else (0, 0, 1, 1)
                self.assertEqual(tuple(map(int, summary.groups())), expected)

    def test_unused_dependency_android_checks_do_not_drop_tail_entries_or_include_siblings(self):
        for variant in ('xml-cap', 'xml-suffix', 'xml-package', 'xml-nested', 'xml-dollar-package',
                        'xml-dollar-nested', 'resource-cap', 'resource-scope'):
            with self.subTest(variant=variant):
                library, consumer = variant + '-lib', variant + '-consumer'
                dependency = ('<dependencies><dependency><groupId>fixture</groupId>'
                              f'<artifactId>{library}</artifactId></dependency></dependencies>')
                for path, name, deps in (('lib', library, ''), ('consumer', consumer, dependency)):
                    self.write(f'{variant}/{path}/pom.xml', '<project><groupId>fixture</groupId>'
                               f'<artifactId>{name}</artifactId>{deps}</project>')
                padding = ''.join(f'class Padding{i:03d} {{}}\n' for i in range(75)) if variant == 'xml-cap' else ''
                nested = variant in ('xml-nested', 'xml-dollar-nested')
                declaration = ('public class Outer { public static class Widget {} }'
                               if nested else 'public class Widget {}')
                filename = 'Outer.java' if nested else 'Widget.java'
                package = {'xml-dollar-package': 'fixture.sub',
                           'xml-dollar-nested': 'fixture$sub'}.get(variant, 'fixture')
                self.write(f'{variant}/lib/{filename}', f'package {package};\n' + padding + declaration + '\n')
                self.write(f'{variant}/consumer/Work.java', 'package app; public class Work {}\n')
                if variant.startswith('xml-'):
                    if variant == 'xml-suffix':
                        self.write(f'{variant}/consumer/OtherWidget.java', 'package app; public class OtherWidget {}')
                    if variant == 'xml-package':
                        self.write(f'{variant}/consumer/Widget.java', 'package app; public class Widget {}')
                    if variant == 'xml-dollar-package':
                        self.write(f'{variant}/consumer/Widget.java', 'package fixture$sub; public class Widget {}')
                    tag = {'xml-suffix': 'app.OtherWidget', 'xml-package': 'app.Widget',
                           'xml-dollar-package': 'fixture$sub.Widget',
                           'xml-dollar-nested': 'fixture$sub.Outer$Widget',
                           'xml-nested': 'fixture.Outer$Widget'}.get(variant, 'fixture.Widget')
                    self.write(f'{variant}/consumer/res/layout/main.xml', f'<{tag}/>')
                else:
                    count = 150 if variant == 'resource-cap' else 1
                    resource_prefix = 'title_' + variant.replace('-', '_') + '_'
                    self.write(f'{variant}/lib/res/values/strings.xml', '<resources>' + ''.join(
                        f'<string name="{resource_prefix}{i:03d}">text</string>' for i in range(count)) + '</resources>')
                    usage_path = 'consumer' if variant == 'resource-cap' else 'consumer-shadow'
                    if variant == 'resource-scope':
                        self.write(f'{variant}/consumer-shadow/pom.xml', '<project><groupId>fixture</groupId>'
                                   f'<artifactId>{variant}-shadow</artifactId>{dependency}</project>')
                    self.write(f'{variant}/{usage_path}/Work.java',
                               f'package app; public class Work {{ int value = R.string.{resource_prefix}{count - 1:03d}; }}\n')
                self.prepare()
                # Nested Maven module names follow directory paths, while
                # artifact IDs resolve dependency coordinates only. Establish
                # the edge before testing its Java/XML resource usage.
                module_name = variant + '.consumer'
                self.assertEqual(
                    module_contracts.edge_rows(self.fixture.text_cli('deps', module_name), 'deps'),
                    [(variant + '.lib', variant + '/lib', 'compile')])
                self.assertEqual(self.fixture.text_cli('unused-deps', consumer),
                                 f"Module '{consumer}' not found in index.\n")
                output = self.fixture.text_cli('unused-deps', module_name)
                summary = re.search(r'^Total: (\d+) unused, (\d+) exported, (\d+) used of (\d+) dependencies$',
                                    output, re.MULTILINE)
                self.assertIsNotNone(summary)
                unused = variant in ('resource-scope', 'xml-suffix', 'xml-package', 'xml-dollar-package')
                expected = (1, 0, 0, 1) if unused else (0, 0, 1, 1)
                self.assertEqual(tuple(map(int, summary.groups())), expected)
                # A used dependency must be attributed to the branch under
                # test, and disabling that branch must make it unused.
                branch = 'XML' if variant.startswith('xml-') else 'Resources'
                self.assertIn(f'  - {branch}: {expected[2]}\n', output)
                flag = '--no-xml' if variant.startswith('xml-') else '--no-resources'
                disabled = self.fixture.text_cli('unused-deps', module_name, flag)
                self.assertIn('Total: 1 unused, 0 exported, 0 used of 1 dependencies\n', disabled)

    def test_java_dependency_syntax_respects_the_configured_read_budget(self):
        for directory, dependency in (('lib', ''), ('consumer',
                '<dependencies><dependency><groupId>fixture</groupId>'
                '<artifactId>lib</artifactId></dependency></dependencies>')):
            self.write(f'{directory}/pom.xml', '<project><groupId>fixture</groupId>'
                       f'<artifactId>{directory}</artifactId>{dependency}</project>')
        self.write('lib/Value.java', 'package dep; public class Value {}\n')
        source = 'package app; import dep.Value; public class Use { Value value; }\n'
        self.write('consumer/Use.java', source)
        # Unrelated source is not parser input for this consumer, even if it
        # would exceed the read budget. Index it before applying the budget.
        self.write('outside/Other.java', 'class Other {\n' + ' // padding\n' * 100 + '}\n')
        self.prepare()
        for budget, succeeds in ((len(source.encode()), True), (8, False)):
            with self.subTest(budget=budget):
                environment = {**self.fixture.environment, 'AST_INDEX_MAX_FILE_SIZE': str(budget)}
                result = subprocess.run([str(self.binary), 'unused-deps', 'consumer', '--strict'],
                                        cwd=self.root, env=environment, capture_output=True,
                                        text=True, timeout=30)
                if succeeds:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('Total: 0 unused, 0 exported, 1 used of 1 dependencies', result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Java syntax source exceeds the 8 byte budget', result.stderr)

    def test_reactor_coordinates_direct_dependencies_and_scope_drive_all_navigation(self):
        # Root artifact comes after the parent/comment; directory and artifact
        # names differ, and identical artifact names belong to different groups.
        self.write('pom.xml', '''<project><parent><groupId>fixture</groupId><artifactId>parent</artifactId></parent>
<!-- <artifactId>comment</artifactId> --><artifactId>reactor</artifactId></project>''')
        self.write('Example.java', 'class Example {}')
        self.write('consumer/pom.xml', '''<project><groupId>fixture</groupId><artifactId>app</artifactId>
<dependencyManagement><dependencies><dependency><groupId>fixture</groupId><artifactId>unused</artifactId></dependency></dependencies></dependencyManagement>
<dependencies><dependency><groupId>fixture</groupId><artifactId>shared</artifactId><scope>test</scope></dependency></dependencies></project>''')
        self.write('consumer/Consumer.java', 'class Consumer {}')
        for directory, group, artifact in [('library', 'fixture', 'shared'), ('other', 'other', 'shared'), ('unused', 'fixture', 'unused')]:
            self.write(directory + '/pom.xml', f'<project><groupId>{group}</groupId><artifactId>{artifact}</artifactId></project>')
            self.write(directory + '/Example.java', 'class Example {}')
        self.prepare()
        out = self.fixture.text_cli('module', '', '--limit', '20')
        self.assertIn('  reactor: ', out)
        deps = self.fixture.text_cli('deps', 'consumer')
        self.assertEqual(module_contracts.edge_rows(deps, 'deps'), [('library', 'library', 'test')])
        dependents = self.fixture.text_cli('dependents', 'library')
        self.assertEqual(module_contracts.edge_rows(dependents, 'dependents'), [('consumer', 'consumer', 'test')])
        route = self.fixture.cli('module-route', '--from', 'consumer', '--to', 'library')
        self.assertEqual(route['paths'][0]['hops'], [{'from': 'consumer', 'to': 'library', 'kind': 'test'}])
        self.assertEqual(self.fixture.cli('module-route', '--from', 'consumer', '--to', 'unused')['empty_reason'], 'unreachable')
        # Remove the external parent to run all independent live contracts on
        # this reactor without claiming effective-model inheritance support.
        self.write('pom.xml', '<project><groupId>fixture</groupId><artifactId>reactor</artifactId></project>')
        self.prepare()
        for feature in sorted(module_contracts.FEATURES):
            self.evaluate(feature)

    def test_mixed_module_output_does_not_consume_java_scope(self):
        self.write('pom.xml', '<project><groupId>fixture</groupId><artifactId>zjava</artifactId></project>')
        self.write('Example.java', 'class Example {}')
        # Foreign module output is normalized; its parser is not under repair.
        self.write('Foreign.pm', 'package AForeign;\n1;\n')
        self.prepare()
        self.evaluate('module')
        check = self.evaluate('module')
        with patch.object(self.fixture, 'text_cli', return_value="Modules matching '%%':\n  wrong: \n"):
            self.fixture.evaluate(check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
        with patch.object(module_contracts, 'MAX_OUTPUT_BYTES', 1):
            self.fixture.evaluate(check)
        self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'unsupported')

    def test_full_inventory_cannot_hide_an_applicable_build_or_effective_model(self):
        self.write('Example.java', 'class Example {}')
        self.prepare()
        # No build descriptors is a checked empty graph, not a fake pass based
        # solely on a Java-only source list.
        for feature in module_contracts.FEATURES:
            self.evaluate(feature)
        self.write('build.gradle', "plugins { id 'java' }")
        mobile_contracts.inventory(self.state, self.root)
        module_contracts.plan_modules(self.state, self.root)
        self.assertEqual(set(r[0] for r in self.state.execute("SELECT status FROM coverage WHERE feature IN ('module','deps','dependents','module-route')")), {'pending'})
        self.evaluate('module', 'unsupported')
        (self.root / 'build.gradle').unlink()
        self.write('pom.xml', '<project><groupId>fixture</groupId><artifactId>profiled</artifactId><profiles><profile><dependencies><dependency><groupId>fixture</groupId><artifactId>conditional</artifactId></dependency></dependencies></profile></profiles></project>')
        mobile_contracts.inventory(self.state, self.root)
        module_contracts.plan_modules(self.state, self.root)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='module'").fetchone()[0], 'pending')
        # An external parent can contribute dependencies even to one module.
        self.write('pom.xml', '<project><parent><groupId>fixture</groupId><artifactId>parent</artifactId></parent><artifactId>child</artifactId></project>')
        mobile_contracts.inventory(self.state, self.root)
        module_contracts.plan_modules(self.state, self.root)
        self.assertEqual(self.state.execute("SELECT status FROM coverage WHERE feature='deps'").fetchone()[0], 'pending')


if __name__ == '__main__':
    unittest.main()
