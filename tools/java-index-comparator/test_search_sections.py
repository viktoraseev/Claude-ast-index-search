"""Small live CLI contracts for the search sections and annotation grep."""
import os
import re
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
    def test_grep_contract_exposes_collected_locations_beyond_twenty(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {\n' + '    // TODO repair\n' * 21 + '    @Deprecated void old() {}\n}\n')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'grep')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                class GrepOracle:
                    def call(self, tool, arguments):
                        pattern = re.compile(arguments['query'])
                        return {'matches': [{'file': 'Example.java', 'line': i} for i, line in
                            enumerate((root / 'Example.java').read_text().splitlines(), 1) if pattern.search(line)]}
                fixture = Fixture(root, binary, database, state, GrepOracle())
                for feature in ('todo', 'deprecated'):
                    state.execute("INSERT INTO checks(id,feature,subject) VALUES (?,?, 'patterns')", (feature, feature))
                    state.commit()
                    fixture.evaluate(state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone())
                    result = state.execute('SELECT verdict,error FROM checks WHERE id=?', (feature,)).fetchone()
                    self.assertEqual(result[0], 'pass', tuple(result))
            finally:
                state.close()

    def test_capped_text_search_partitions_by_file_and_executes_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            for name in ('A', 'B'):
                (root / (name + '.java')).write_text('class ' + name + ' { String text = "needle"; }\n')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'partition')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('text','search:content','needle')")
                state.commit()
                class CappedOracle:
                    def call(self, tool, arguments):
                        if 'paths' not in arguments:
                            return {'matches': [{'file': 'A.java', 'line': 1}] * 5000, 'totalCollected': 5000}
                        return {'matches': [{'file': arguments['paths'][0], 'line': 1}]}
                fixture = Fixture(root, binary, database, state, CappedOracle())
                fixture.evaluate(state.execute('SELECT * FROM checks').fetchone())
                result = state.execute('SELECT verdict,diff_json,error FROM checks').fetchone()
                self.assertEqual(result[0], 'pass', tuple(result))
                self.assertEqual(state.execute('SELECT count(*) FROM pages').fetchone()[0], 3)
            finally:
                state.close()

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
