"""Small lexical Kotlin/Swift fixtures exercising the production CLI."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from common import connect
import mobile_contracts


class PagedTextOracle:
    """Synthetic adapter test; this is not live-project MCP evidence."""
    def __init__(self, root):
        self.root = root
        self.requests = []
        self.remaining = None

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


class MobileTextContracts(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()  # Stop CLI project discovery at this fixture.
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'checks.sqlite')
        self.state.executescript(SCHEMA)
        self.fixture = Fixture(self.root, self.binary, self.directory / 'index.sqlite', self.state, None)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def count(self, command, *arguments):
        output = self.fixture.text_cli(command, *arguments)
        return int(re.search(r'\((\d+)\):$', output.splitlines()[0])[1]), output

    def test_suspend_limit_counts_names_and_extracted_declarations(self):
        (self.root / 'Example.kt').write_text('''suspend fun unrelated() { val needle = 1 }
// suspend fun needle is described without a signature
suspend fun needle() {}
suspend fun needleAgain() {}
// nonsuspend fun needleFake() is not a suspend function
''')
        count, output = self.count('suspend', 'NEEDLE', '--limit', '1')
        self.assertEqual(count, 1, output)
        self.assertIn('needle: Example.kt:3', output)

    def test_extensions_use_exact_receiver_and_language_scope(self):
        (self.root / 'Example.kt').write_text('''fun String.good() {}
// extension String { } is Swift text, not a Kotlin extension
fun StringExtra.unrelated() {}
// notfun String.unrelated() is not an extension function
''')
        (self.root / 'Example.swift').write_text('''extension StringExtra {}
extension String {}
// fun String.fake() is Kotlin text, not a Swift extension
''')
        count, output = self.count('extensions', 'String', '--limit', '100')
        self.assertEqual(count, 2, output)
        self.assertNotIn('fake', output)
        self.assertNotIn('StringExtra', output)

    def test_publisher_annotation_and_type_declarations_and_actor_boundary(self):
        (self.root / 'Example.swift').write_text('''@Published private(set) var count = 0
let updates: AnyPublisher<Int, Never>
let events = PassthroughSubject<Int, Never>()
let ignored: AnyPublisherExtra<Int, Never>
@MainActorExtra class Unrelated {}
@MainActor class Example {}
''')
        with self.subTest(command='publishers'):
            count, output = self.count('publishers', '--limit', '100')
            self.assertEqual(count, 3, output)
            self.assertNotIn('Extra', output)
            self.assertEqual(self.count('publishers', 'COUNT', '--limit', '1')[0], 1)
        with self.subTest(command='main-actor'):
            count, output = self.count('main-actor', '--limit', '1')
            self.assertEqual(count, 1, output)
            self.assertIn('Example.swift:6', output)

    def test_kotlin_scripts_are_applicable_source(self):
        (self.root / 'Example.kts').write_text('''suspend fun load() {}
val updates: StateFlow<Int>
fun String.decorate() {}
''')
        for command, args in (('suspend', []), ('flows', []), ('extensions', ['String'])):
            with self.subTest(command=command):
                self.assertEqual(self.count(command, *args, '--limit', '100')[0], 1)

    def evaluate(self, feature, query):
        subject = json.dumps({'query': query})
        self.state.execute('INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)',
                           (feature, feature, subject))
        self.state.commit()
        check = self.state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
        self.fixture.evaluate(check)
        result = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (feature,)).fetchone()
        self.assertEqual(result[0], 'pass', tuple(result))
        return check

    def test_live_fixture_normalizes_pagination_line_identity_filters_and_limits(self):
        (self.root / 'Example.kt').write_text('''suspend fun unrelated() { val needle = 1 }
suspend fun <T> Box<T>.needle() {}
val needle: StateFlow<Int> = MutableStateFlow<Int>(0)
fun String.needle() {}
''')
        (self.root / 'Second.kts').write_text('suspend fun needleAgain() {}\nval updates: Flow <Int>\n')
        (self.root / 'Example.swift').write_text('''@Published var needle = 0
let needleEvents = PassthroughSubject<Int, Never>()
@MainActor class Needle {}
extension String {}
''')
        oracle = PagedTextOracle(self.root)
        self.fixture.client = oracle
        for feature in ('suspend', 'flows', 'publishers', 'main-actor'):
            for query in (None, '', 'NEEDLE', '[missing]'):
                with self.subTest(feature=feature, query=query):
                    self.evaluate(feature, query)
        check = self.evaluate('extensions', 'String')
        self.assertTrue(any('cursor' in request for request in oracle.requests))
        self.assertGreater(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        expected = json.loads(self.state.execute("SELECT expected_json FROM checks WHERE id='flows'").fetchone()[0])
        self.assertTrue(expected['source'].startswith('live MCP lexical'))
        with patch.object(self.fixture, 'text_cli', return_value='Extensions for String (0):\n'):
            self.fixture.evaluate(check)
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='extensions'").fetchone()[0], 'fail')

    def test_inventory_proves_absence_and_cannot_hide_applicable_languages(self):
        (self.root / 'Example.java').write_text('class Example {}')
        help_text = '  class  Classes\n  symbol  Symbols\n  file  Files'
        plan(self.state, [{'path': 'Example.java'}], help_text, [], self.root)
        for feature in mobile_contracts.EXTENSIONS:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'inapplicable')
            self.evaluate(feature, 'String' if feature == 'extensions' else None)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        # Java-only input to plan cannot erase a script or Swift source that
        # is present elsewhere in the full inventory, even in an ignored tree.
        nested = self.root / 'build'
        nested.mkdir()
        (nested / 'Example.kts').write_text('suspend fun load() {}')
        (nested / 'Example.swift').write_text('@MainActor class Example {}')
        (self.root / '.gitignore').write_text('build/\n')
        plan(self.state, [{'path': 'Example.java'}], help_text, [], self.root)
        for feature in mobile_contracts.EXTENSIONS:
            status = self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0]
            self.assertEqual(status, 'pending', feature)
        self.assertEqual(self.state.execute("SELECT count(*) FROM file_inventory WHERE extension IN ('.java','.kts','.swift')").fetchone()[0], 3)

    def test_inventory_links_and_mixed_case_suffixes_leave_scope_pending(self):
        self.assertEqual(mobile_contracts.applicability(self.state, 'flows')[0], 'pending')
        (self.root / 'Example.Kt').write_text('val updates: Flow<Int>')
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(mobile_contracts.applicability(self.state, 'flows')[0], 'pending')
        (self.root / 'Example.Kt').unlink()
        # Relevant source links must not become false absence assertions.
        (self.root / 'Example.kt').symlink_to(self.directory / 'unknown.kt')
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(mobile_contracts.applicability(self.state, 'flows')[0], 'pending')
        (self.root / 'Example.kt').unlink()
        (self.directory / 'linked-project').mkdir()
        (self.root / 'linked').symlink_to(self.directory / 'linked-project', target_is_directory=True)
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(mobile_contracts.applicability(self.state, 'flows')[0], 'pending')

    def test_oracle_caps_and_wrong_file_scope_stay_unresolved(self):
        (self.root / 'Example.kts').write_text('val updates: Flow<Int>\n')
        class Oracle:
            def __init__(self, matches):
                self.matches = matches
            def call(self, tool, arguments):
                assert arguments['paths'] == ['Example.kts']
                return {'matches': self.matches}
        for matches in ([{'file': 'Example.java', 'line': 1}],
                        [{'file': 'Example.kts', 'line': 1}] * 5000):
            self.state.execute("INSERT OR REPLACE INTO checks(id,feature,subject) VALUES ('cap','flows',?)",
                               (json.dumps({'query': None}),))
            self.state.commit()
            self.fixture.client = Oracle(matches)
            check = self.state.execute("SELECT * FROM checks WHERE id='cap'").fetchone()
            self.fixture.evaluate(check)
            self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='cap'").fetchone()[0], 'unsupported')


if __name__ == '__main__':
    unittest.main()
