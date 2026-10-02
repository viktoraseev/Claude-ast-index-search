"""Small live CLI contracts for the search sections and annotation grep."""
import os
from pathlib import Path
import tempfile
import unittest

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect


class TextOracle:
    def call(self, tool, arguments):
        if tool == 'ide_find_file':
            return {'files': [{'name': 'Example.java', 'path': 'Example.java'}]}
        if tool == 'ide_search_text':
            lines = [2, 3, 4] if arguments['query'] == 'needle' else [2, 3]
            return {'matches': [{'file': 'Example.java', 'line': line} for line in lines]}
        raise AssertionError(tool)


class SearchSections(unittest.TestCase):
    def test_sections_execute_cli_and_annotation_scope_includes_comments(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            source = root / 'Example.java'
            source.write_text('''class Example {
    // @Label needle
    @Label String text = "needle";
    void needle() {}
}
''')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'search-sections')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': 'Example.java'}], '  class  Find classes', ['needle'], root)
                fixture = Fixture(root, binary, database, state, TextOracle())
                for feature in ['search:files', 'search:content', 'annotations']:
                    with self.subTest(feature=feature):
                        check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                        self.assertIsNotNone(check)
                        fixture.evaluate(check)
                        outcome = state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                        self.assertEqual(outcome[0], 'pass', tuple(outcome))
                source.write_text('class Example {}\n')
                check = state.execute("SELECT * FROM checks WHERE feature='annotations'").fetchone()
                fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
