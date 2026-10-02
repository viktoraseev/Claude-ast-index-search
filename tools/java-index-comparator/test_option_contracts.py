"""Independent source expectations for production filters, bodies and search pages."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from audit import Fixture, SCHEMA, LIVE_FEATURES, plan
from build_index import build_ast_index
from common import connect


class OptionContracts(unittest.TestCase):
    def test_qualified_patterns_and_fuzzy_kind_filters_execute_production(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Example.java').write_text('''package example;
class Example {
    int value;
    void run() {} void run(int n) {}
    class Nested { void run() {} }
    void outer() { class Local { void run() {} } }
}
class OtherExample { void useExample() {} }
''')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'qualified-options')
            state = connect(directory / 'checks.sqlite')
            native = connect(database)
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': 'Example.java'}], '  class  Find classes', [], root)
                fixture = Fixture(root, binary, database, state, None)
                for feature in ('symbol:qualified-pattern', 'class:qualified-pattern'):
                    with self.subTest(feature=feature):
                        check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                        fixture.evaluate(check)
                        result = state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                        self.assertEqual(result[0], 'pass', tuple(result))
                page = fixture.cli('symbol', '--pattern', 'example.Example.*', '--type', 'function',
                                   '--in-file', 'Example.java', '--limit', '1')
                self.assertEqual(len(page['items']), 1)
                self.assertTrue(page['items'][0]['qualified_name'].startswith('example.Example.'))
                self.assertEqual(page['pagination']['total'], 4)
                self.assertTrue(page['pagination']['truncated'])
                with native:
                    native.execute("UPDATE symbols SET qualified_name='wrong.Nested' WHERE name='Nested'")
                check = state.execute("SELECT * FROM checks WHERE feature='class:qualified-pattern'").fetchone()
                fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            finally:
                native.close()
                state.close()

    def test_pending_contracts_execute_production_and_detect_deleted_index_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Example.java').write_text("""package example;
class Example {
    int value;
    Example() {}
    void run() { helper(); }
    void helper() {}
}
class ExampleExtra { void run() {} }
""")
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'options')
            state = connect(directory / 'checks.sqlite')
            native = connect(database)
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': 'Example.java'}], '  class  Find classes', ['Example'], root)
                fixture = Fixture(root, binary, database, state, None)
                for feature in ('symbol:options', 'class:options', 'search:references', 'search:ranking'):
                    with self.subTest(feature=feature):
                        self.assertIn(feature, LIVE_FEATURES)
                        check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                        fixture.evaluate(check)
                        result = state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                        self.assertEqual(result[0], 'pass', tuple(result))
                with native:
                    native.execute("DELETE FROM symbols WHERE name='value'")
                check = state.execute("SELECT * FROM checks WHERE feature='symbol:options'").fetchone()
                fixture.evaluate(check)
                result = state.execute('SELECT verdict,diff_json FROM checks WHERE id=?', (check['id'],)).fetchone()
                self.assertEqual(result[0], 'fail')
                self.assertTrue(json.loads(result[1])['missing'])
            finally:
                native.close()
                state.close()


if __name__ == '__main__':
    unittest.main()
