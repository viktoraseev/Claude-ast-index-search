"""Small production fixtures for Java injection and suppression coverage."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect


class TextOracle:
    """Synthetic paginated MCP adapter; not real-project MCP evidence."""
    def __init__(self, root):
        self.root = root

    def call(self, tool, arguments):
        assert tool == 'ide_search_text'
        matches = [{'file': path.name, 'line': number} for path in sorted(self.root.glob('*.java'))
                   for number, line in enumerate(path.read_text().splitlines(), 1)
                   if re.search(arguments['query'], line)]
        if 'cursor' in arguments:
            return {'matches': matches[1:]}
        return {'matches': matches[:1], 'hasMore': len(matches) > 1,
                **({'nextCursor': 'remaining'} if len(matches) > 1 else {})}


class AnnotationContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.database = self.directory / 'index.sqlite'
        self.state = connect(self.directory / 'checks.sqlite')
        self.state.executescript(SCHEMA)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def evaluate(self, feature, subject, oracle=None):
        self.state.execute('INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)',
                           (feature, feature, subject))
        self.state.commit()
        fixture = Fixture(self.root, self.binary, self.database, self.state, oracle)
        fixture.evaluate(self.state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone())
        row = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (feature,)).fetchone()
        self.assertEqual(row[0], 'pass', tuple(row))
        return fixture

    def test_suppress_executes_production_with_pagination_filters_and_boundaries(self):
        (self.root / 'Example.java').write_text('''class Example {
    @SuppressWarnings("unchecked") Object first;
    @java.lang.SuppressWarnings("unchecked") Object second;
    // @SuppressWarnings("unchecked") documented lexical match
    @SuppressWarningsExtra("unchecked") Object unrelated;
}
''')
        # Pagination is request-bound: retain the initial query for the next page.
        class Oracle(TextOracle):
            def call(self, tool, arguments):
                if 'cursor' in arguments:
                    return super().call(tool, {**self.first, **arguments})
                self.first = arguments
                return super().call(tool, arguments)
        build_ast_index(str(self.binary), self.root, self.database, 'suppression')
        oracle = Oracle(self.root)
        for query in (None, '', 'UNCHECKED', '[missing]'):
            with self.subTest(query=query):
                self.evaluate('suppress', json.dumps({'query': query}), oracle)
        fixture = self.evaluate('suppress', json.dumps({'query': None}), oracle)
        with patch.object(fixture, 'text_cli', return_value='@Suppress annotations (0):\n'):
            fixture.evaluate(self.state.execute("SELECT * FROM checks WHERE feature='suppress'").fetchone())
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE feature='suppress'").fetchone()[0], 'fail')

    def test_inject_checks_types_instead_of_annotation_arguments_names_or_bodies(self):
        (self.root / 'Example.java').write_text('''class Service {}
class Example {
    @Inject Service field;
    @Autowired(required = false)
    Service optional;
    @javax.inject.Inject
    Example(
        Service first,
        java.util.List<Service> second) {}
    @Inject void set(Service value) { Service local = null; }
    @Inject void many(Service... values) {}
    void parameter(@Inject Service[] values) {}
    void varargs(@Inject Service... values) {}
    @Inject Service[] array;
    @Inject java.util.List<@TypeLabel("Service") Object> labelled;
    @Inject Object Service;
    // @Inject Service documented;
}
''')
        build_ast_index(str(self.binary), self.root, self.database, 'injection')
        fixture = self.evaluate('inject', 'Service')
        labelled_line = next(number for number, line in enumerate(
            (self.root / 'Example.java').read_text().splitlines(), 1) if 'labelled' in line)
        expected = json.loads(self.state.execute("SELECT expected_json FROM checks WHERE feature='inject'").fetchone()[0])
        self.assertNotIn({'file': 'Example.java', 'line': labelled_line}, expected)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        # A correct shape with no results still fails the independent syntax contract.
        with patch.object(fixture, 'text_cli', return_value="Injection points for 'Service' (0):\n"):
            fixture.evaluate(self.state.execute("SELECT * FROM checks WHERE feature='inject'").fetchone())
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE feature='inject'").fetchone()[0], 'fail')

    def test_planning_keeps_non_java_contracts_pending_and_labels_sources(self):
        (self.root / 'Example.java').write_text('class Example { @SuppressWarnings("unchecked") Object value; }')
        plan(self.state, [{'path': 'Example.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files', ['Object'], self.root)
        for feature, prefix in (('suppress', 'live MCP text'), ('inject', 'independent JDK syntax')):
            reason = self.state.execute('SELECT reason FROM coverage WHERE feature=?', (feature,)).fetchone()[0]
            self.assertTrue(reason.startswith(prefix), reason)
            self.assertGreater(self.state.execute('SELECT count(*) FROM checks WHERE feature=?', (feature,)).fetchone()[0], 0)
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature + ':non-java',)).fetchone()[0], 'pending')


if __name__ == '__main__':
    unittest.main()
