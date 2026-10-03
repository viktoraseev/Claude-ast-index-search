"""Declaration binding regressions execute the CLI on small synthetic sources."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from common import connect
import annotation_contracts
import mobile_contracts


class PagedAnnotationOracle:
    """Synthetic adapter evidence, not live-target MCP equivalence."""
    def __init__(self, root):
        self.root, self.requests, self.remaining = root, [], []

    def call(self, tool, arguments):
        assert tool == 'ide_search_text'
        self.requests.append(arguments)
        if 'cursor' in arguments:
            assert arguments['cursor'] == 'remaining'
            return {'matches': self.remaining}
        path, = arguments['paths']
        assert arguments['filePattern'] == '*' + Path(path).suffix
        matches = [{'file': path, 'line': number, 'column': match.start() + 1}
                   for number, line in enumerate((self.root / path).read_text().splitlines(), 1)
                   for match in re.finditer(arguments['query'], line)]
        self.remaining = matches[1:]
        return {'matches': matches[:1], 'hasMore': bool(self.remaining),
                **({'nextCursor': 'remaining'} if self.remaining else {})}


class AnnotationFunctionContracts(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.state = connect(self.directory / 'checks.sqlite')
        self.state.executescript(SCHEMA)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'index.sqlite', self.state, None)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def locations(self, command, *args):
        output = self.fixture.text_cli(command, *args)
        count = int(re.search(r'\((\d+)\):$', output.splitlines()[0])[1])
        rows = [match.groups() for line in output.splitlines()[1:]
                if (match := re.fullmatch(r'  (?:.+: )?(.+\.(?:java|kt|kts)):(\d+)', line))]
        self.assertEqual(count, len(rows), output)
        return [(path, int(line)) for path, line in rows]

    def test_compose_annotations_bind_exact_declarations_and_name_filters(self):
        (self.root / 'Example.kt').write_text('''@ComposableExtra fun falseComposable() {}
@PreviewExtra fun falsePreview() {}
// @Composable @Preview fun commented() {}
val description = "@Composable @Preview fun quoted() {}"
@androidx.compose.runtime.Composable
@androidx.compose.ui.tooling.preview.Preview(
    name = "long annotation",
    widthDp = 10,
    heightDp = 10,
    showBackground = true
)
fun <T> Box<T>.needle() {}
''')
        for command in ('composables', 'previews'):
            with self.subTest(command=command):
                self.assertEqual(self.locations(command, '--limit', '100'), [('Example.kt', 12)])
                self.assertEqual(self.locations(command, 'NEEDLE', '--limit', '1'), [('Example.kt', 12)])
                self.assertEqual(self.locations(command, 'Box', '--limit', '100'), [])
                self.assertEqual(self.locations(command, '[', '--limit', '100'), [])

    def test_kotlin_scripts_and_ordered_limits_are_not_silently_absent(self):
        (self.root / 'First.kts').write_text('@Composable @Preview fun first() {}\n@Composable @Preview fun second() {}\n')
        (self.root / 'Last.kt').write_text('@Composable @Preview fun last() {}\n')
        for command in ('composables', 'previews'):
            for limit in (0, 1, 2, 100):
                with self.subTest(command=command, limit=limit):
                    self.assertEqual(self.locations(command, '--limit', str(limit)),
                                     [('First.kts', 1), ('First.kts', 2), ('Last.kt', 1)][:limit])

    def test_provider_qualified_types_and_generic_arguments_have_distinct_scope(self):
        (self.root / 'Example.java').write_text('''class Example {
    @Provides example.Widget qualified() { return null; }
    @Provides java.util.List<Widget> boxed() { return null; }
    @Provides example.Widget[] array() { return null; }
}
''')
        (self.root / 'Example.kts').write_text('@Provides fun nullable(): example.Widget? = null\n')
        for query in ('Widget', 'example.Widget'):
            self.assertEqual(self.locations('provides', query, '--limit', '100'),
                             [('Example.java', 2), ('Example.java', 4), ('Example.kts', 1)])
        self.assertEqual(self.locations('provides', 'java.util.List', '--limit', '100'), [('Example.java', 3)])
        self.assertEqual(self.locations('provides', 'widget', '--limit', '100'), [])

    def test_same_line_functions_keep_names_multiplicity_and_source_order(self):
        (self.root / 'Example.kt').write_text('@Composable @Preview fun first() {}; @Composable @Preview fun second() {}\n')
        (self.root / 'Example.java').write_text('class Example { @Provides Widget first() { return null; } @Provides Widget second() { return null; } }\n')
        self.fixture.client = PagedAnnotationOracle(self.root)
        for feature in ('composables', 'previews'):
            self.assertEqual(self.locations(feature, '--limit', '100'), [('Example.kt', 1), ('Example.kt', 1)])
            self.assertEqual(self.locations(feature, 'SECOND', '--limit', '1'), [('Example.kt', 1)])
            self.assertEqual(self.evaluate(feature, None)['verdict'], 'pass')
            output = self.fixture.text_cli(feature, '--limit', '1')
            self.assertIn('first: Example.kt:1', output)
            def wrong_name(command, *arguments):
                header = '@' + annotation_contracts.ANNOTATIONS[feature][0] + ' functions'
                return header + (' (0):\n' if arguments[-1] == '0' else ' (1):\n  unrelated: Example.kt:1\n')
            with patch.object(self.fixture, 'text_cli', side_effect=wrong_name):
                self.assertEqual(self.evaluate(feature, None)['verdict'], 'fail')
        self.assertEqual(self.locations('provides', 'Widget', '--limit', '100'), [('Example.java', 1), ('Example.java', 1)])
        self.assertEqual(self.evaluate('provides', 'Widget')['verdict'], 'pass')

    def test_escaped_kotlin_function_names_are_preserved_by_the_adapter(self):
        (self.root / 'Example.kts').write_text('@Composable @Preview fun `colon: name`() {}\n')
        self.fixture.client = PagedAnnotationOracle(self.root)
        for feature in ('composables', 'previews'):
            self.assertEqual(self.evaluate(feature, 'NAME')['verdict'], 'pass')

    def test_providers_match_declared_return_type_and_exact_annotations(self):
        (self.root / 'Providers.java').write_text('''class Providers {
    @ProvidesExtra Widget fake() { return null; }
    @Provides Other parameterOnly(Widget argument) { return null; }
    // @Provides Widget commented() { return null; }
    @dagger.Provides
    public AppWidget actual() { return null; }
    @Binds abstract Widget bound(AppWidget implementation);
    @Provides$Extra Widget unrelated() { return null; }
}
''')
        (self.root / 'Providers.kts').write_text('''@Provides fun parameterOnly(widget: Widget): Other = Other()
@Provides fun actual(): AppWidget = AppWidget()
@Binds fun bound(implementation: AppWidget): Widget = implementation
''')
        self.assertEqual(self.locations('provides', 'Widget', '--limit', '100'),
                         [('Providers.java', 5), ('Providers.java', 7), ('Providers.kts', 2), ('Providers.kts', 3)])
        self.assertEqual(self.locations('provides', '[', '--limit', '100'), [])
        self.assertEqual(self.locations('provides', 'Widget', '--limit', '1'), [('Providers.java', 5)])

    def evaluate(self, feature, query):
        self.state.execute('INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)',
                           (feature, feature, json.dumps({'query': query})))
        self.state.commit()
        check = self.state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT verdict,error,diff_json,expected_json FROM checks WHERE id=?', (feature,)).fetchone()

    def test_java_only_providers_keep_java_results_after_foreign_rows_consume_limits(self):
        (self.root / 'A.kts').write_text('@Provides fun foreign(): Widget = Widget()\n')
        (self.root / 'Z.java').write_text('''class Z {
    @Provides Widget first() { return null; }
    @Provides Widget second() { return null; }
    @Provides Widget third() { return null; }
}
''')
        self.state.execute("INSERT INTO metadata VALUES ('audit_scope','java')")
        self.state.commit()
        oracle = PagedAnnotationOracle(self.root)
        self.fixture.client = oracle
        outcome = self.evaluate('provides', 'Widget')
        self.assertEqual(outcome['verdict'], 'pass', tuple(outcome))
        self.assertEqual(json.loads(outcome['expected_json'])['locations'],
                         [['Z.java', 2], ['Z.java', 3], ['Z.java', 4]])
        self.assertTrue(all(request.get('paths') == ['Z.java']
                            for request in oracle.requests if 'cursor' not in request))
        original = self.fixture.text_cli
        def missing_java(feature, *arguments):
            output = original(feature, *arguments)
            if arguments[-1] == '1000000':
                lines = output.splitlines()
                lines[0] = lines[0].replace('(4)', '(3)')
                return '\n'.join(lines[:-2]) + '\n'
            return output
        with patch.object(self.fixture, 'text_cli', side_effect=missing_java):
            self.assertEqual(self.evaluate('provides', 'Widget')['verdict'], 'fail')

    def test_hybrid_fixture_binds_paginated_anchors_and_exercises_production(self):
        (self.root / 'Example.kts').write_text('''// @Composable @Preview fun fake() {}
val note = "@Provides fun fake(): Widget = Widget()"
@Composable @Preview(name = "sample")
fun <T> Box<T>.needle() {}
@ComposableExtra @PreviewExtra fun unrelated() {}
@Provides fun parameterOnly(widget: Widget): Other = Other()
@Provides fun actual(): AppWidget = AppWidget()
@Binds fun bound(implementation: AppWidget): Widget = implementation
''')
        (self.root / 'Example.java').write_text('''class Example {
    @Provides Other parameterOnly(Widget widget) { return null; }
    @dagger.Provides AppWidget actual() { return null; }
    @Binds abstract Widget bound(AppWidget implementation);
    @ProvidesExtra Widget unrelated() { return null; }
}
''')
        oracle = PagedAnnotationOracle(self.root)
        self.fixture.client = oracle
        for feature, queries in (('provides', ('Widget', 'Other', '[', '')),
                                 ('composables', (None, '', 'NEEDLE', 'Box', '[')),
                                 ('previews', (None, '', 'NEEDLE', 'Box', '['))):
            for query in queries:
                with self.subTest(feature=feature, query=query):
                    outcome = self.evaluate(feature, query)
                    self.assertEqual(outcome['verdict'], 'pass', tuple(outcome))
                    self.assertTrue(json.loads(outcome['expected_json'])['source'].startswith('hybrid MCP/source'))
        self.assertTrue(any('cursor' in request for request in oracle.requests))
        self.assertGreater(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        # Missing production results cannot be turned into a contract-shape pass.
        with patch.object(self.fixture, 'text_cli', return_value='@Preview functions (0):\n'):
            self.assertEqual(self.evaluate('previews', None)['verdict'], 'fail')

    def test_inventory_proves_absence_but_does_not_erase_applicable_sources(self):
        (self.root / 'Example.java').write_text('class Example {}')
        help_text = '  class  Classes\n  symbol  Symbols\n  file  Files'
        plan(self.state, [{'path': 'Example.java'}], help_text, [], self.root)
        for feature in ('composables', 'previews'):
            self.assertEqual(annotation_contracts.applicability(self.state, feature)[0], 'inapplicable')
            self.assertEqual(self.evaluate(feature, None)['verdict'], 'pass')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertEqual(annotation_contracts.applicability(self.state, 'provides')[0], 'implemented')
        (self.root / 'Script.kts').write_text('@Composable @Preview fun sample() {}')
        plan(self.state, [{'path': 'Example.java'}], help_text, [], self.root)
        for feature in ('composables', 'previews'):
            self.assertEqual(annotation_contracts.applicability(self.state, feature)[0], 'implemented')
        (self.root / '.gitignore').write_text('Script.kts\n')
        plan(self.state, [{'path': 'Example.java'}], help_text, [], self.root)
        for feature in annotation_contracts.EXTENSIONS:
            self.assertEqual(annotation_contracts.applicability(self.state, feature)[0], 'pending')
        self.assertEqual(self.state.execute("SELECT count(*) FROM file_inventory WHERE extension IN ('.java','.kts')").fetchone()[0], 2)

    def test_unsupported_syntax_and_oracle_gaps_remain_unresolved(self):
        (self.root / 'Script.kts').write_text('@Provides fun complex(): (Int) -> Widget = TODO()\n')
        self.fixture.client = PagedAnnotationOracle(self.root)
        outcome = self.evaluate('provides', 'Widget')
        self.assertEqual(outcome['verdict'], 'unsupported', tuple(outcome))
        self.assertIn('return type', outcome['error'])
        self.assertEqual(annotation_contracts.applicability(self.state, 'provides')[0], 'implemented')
        (self.root / 'Script.kts').write_text('@Composable fun sample() {}\n')
        self.fixture._inventory_ready = False
        class MissingOracle:
            def call(self, tool, arguments):
                return {'matches': []}
        self.fixture.client = MissingOracle()
        self.assertEqual(self.evaluate('composables', None)['verdict'], 'unsupported')
        class WrongScopeOracle:
            def call(self, tool, arguments):
                return {'matches': [{'file': 'Outside.kt', 'line': 1}]}
        self.fixture.client = WrongScopeOracle()
        self.assertEqual(self.evaluate('composables', None)['verdict'], 'unsupported')
        class CappedOracle:
            def call(self, tool, arguments):
                return {'matches': [{'file': 'Script.kts', 'line': 1}] * 5000}
        self.fixture.client = CappedOracle()
        self.assertEqual(self.evaluate('composables', None)['verdict'], 'unsupported')
        for source in ('@[Composable Preview] fun sample() {}',
                       'val text = "${run { @Composable fun nested() {} }}"'):
            (self.root / 'Script.kts').write_text(source)
            self.fixture._inventory_ready = False
            self.fixture.client = PagedAnnotationOracle(self.root)
            self.assertEqual(self.evaluate('composables', None)['verdict'], 'unsupported')

    def test_inventory_relevant_links_and_suffixes_are_not_absence_evidence(self):
        for name in ('Example.Kts', 'Example.kt'):
            path = self.root / name
            if name.endswith('.kt'):
                path.symlink_to(self.directory / 'missing.kt')
            else:
                path.write_text('@Composable fun sample() {}')
            mobile_contracts.inventory(self.state, self.root)
            for feature in annotation_contracts.EXTENSIONS:
                self.assertEqual(annotation_contracts.applicability(self.state, feature)[0], 'pending')
            path.unlink()

    def test_independent_ignore_scope_requires_every_relevant_source_to_remain_visible(self):
        subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
        (self.root / 'Example.java').write_text('class Example { @Provides Widget actual() { return null; } }')
        (self.root / 'Example.kts').write_text('@Composable @Preview fun sample() {}')
        (self.root / '.gitignore').write_text('*.class\n')
        mobile_contracts.inventory(self.state, self.root)
        for feature in annotation_contracts.EXTENSIONS:
            self.assertEqual(annotation_contracts.applicability(self.state, feature, self.root)[0], 'implemented')
            proof = json.loads(self.state.execute('SELECT value FROM metadata WHERE key=?', ('annotation_scope:' + feature,)).fetchone()[0])
            self.assertTrue(proof['complete'])
            self.assertEqual(proof['ignored'], [])
        (self.root / '.gitignore').write_text('*.kts\n*.java\n')
        mobile_contracts.inventory(self.state, self.root)
        for feature in annotation_contracts.EXTENSIONS:
            self.assertEqual(annotation_contracts.applicability(self.state, feature, self.root)[0], 'pending')

    def test_kotlin_nested_comments_do_not_create_a_false_production_mismatch(self):
        (self.root / 'Example.kt').write_text('''/* outer comment /* nested comment */
@Composable @Preview fun fake() {}
*/
@Composable @Preview fun actual() {}
''')
        self.fixture.client = PagedAnnotationOracle(self.root)
        for feature in ('composables', 'previews'):
            self.assertEqual(self.evaluate(feature, None)['verdict'], 'pass')


if __name__ == '__main__':
    unittest.main()
