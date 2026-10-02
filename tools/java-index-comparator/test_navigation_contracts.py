"""Exercise audit contracts against the production CLI on a tiny Java project."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect


class Oracle:
    """Synthetic oracle whose declaration set is independent of the index."""
    declarations = [
        {'name': 'Base', 'kind': 'CLASS', 'file': 'Base.java', 'line': 2, 'column': 11, 'qualifiedName': 'example.Base'},
        {'name': 'Child', 'kind': 'CLASS', 'file': 'Child.java', 'line': 3, 'column': 7, 'qualifiedName': 'example.Child'},
        {'name': 'work', 'kind': 'METHOD', 'file': 'Child.java', 'line': 4, 'column': 10, 'qualifiedName': 'example.Child.work'},
        {'name': 'caller', 'kind': 'METHOD', 'file': 'Child.java', 'line': 5, 'column': 10, 'qualifiedName': 'example.Child.caller'},
        {'name': 'task', 'kind': 'SYMBOL', 'file': 'Child.java', 'line': 6, 'column': 14, 'qualifiedName': 'example.Child.task'},
    ]

    def call(self, tool, arguments):
        if tool in {'ide_find_symbol', 'ide_find_class'}:
            field = 'symbols' if tool == 'ide_find_symbol' else 'classes'
            return {field: [d for d in self.declarations if
                (d['name'] == arguments['query'] if tool == 'ide_find_class' else arguments['query'] in d['name'])]}
        if tool == 'ide_find_implementations':
            return {'implementations': [self.declarations[1]]}
        if tool == 'ide_type_hierarchy':
            return {'supertypes': [], 'subtypes': [self.declarations[1]]}
        if tool == 'ide_find_references':
            return {'references': [{'file': 'Child.java', 'line': line, 'type': 'REFERENCE'} for line in (5, 6)]}
        if tool == 'ide_search_text':
            return {'matches': [{'file': 'Child.java', 'line': 2, 'column': 1}]}
        raise AssertionError('unexpected oracle operation: ' + tool)


class NavigationContractTests(unittest.TestCase):
    def test_same_line_overloads_detect_one_removed_production_index_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'A.java').write_text('package example;\nclass A { void run() {} void run(int x) {} }\n')
            item = {'name': 'run', 'kind': 'METHOD', 'file': 'A.java', 'line': 2,
                    'qualifiedName': 'example.A.run'}
            oracle = Oracle()
            oracle.declarations = [item, item]
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'synthetic-overloads')
            state = connect(directory / 'evidence.sqlite')
            native = connect(database)
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('overloads','symbol','run')")
                state.commit()
                check = state.execute("SELECT * FROM checks WHERE id='overloads'").fetchone()
                fixture = Fixture(root, binary, database, state, oracle)
                fixture.evaluate(check)
                baseline = state.execute("SELECT verdict,diff_json,error FROM checks WHERE id='overloads'").fetchone()
                self.assertEqual(baseline[0], 'pass', tuple(baseline))
                with native:
                    native.execute("DELETE FROM symbols WHERE id=(SELECT id FROM symbols WHERE name='run' LIMIT 1)")
                fixture.evaluate(check)
                outcome = state.execute("SELECT verdict,diff_json FROM checks WHERE id='overloads'").fetchone()
                self.assertEqual(outcome[0], 'fail')
                self.assertEqual(len(json.loads(outcome[1])['missing']), 1)
            finally:
                native.close()
                state.close()

    def test_hierarchy_prefers_exact_interface_over_class_substring(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Base.java').write_text('package example;\ninterface Base {}\n')
            (root / 'Child.java').write_text('package example;\nclass BaseAdapter implements Base {}\n')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'interface-shadow')
            state = connect(directory / 'evidence.sqlite')
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('hierarchy','hierarchy','Base')")
                state.commit()
                class HierarchyOracle(Oracle):
                    def call(self, tool, arguments):
                        if tool == 'ide_type_hierarchy':
                            return {'supertypes': [], 'subtypes': [
                                {'name': 'example.BaseAdapter', 'file': 'Child.java', 'line': 2}]}
                        return super().call(tool, arguments)
                fixture = Fixture(root, binary, database, state, HierarchyOracle())
                fixture.evaluate(state.execute("SELECT * FROM checks WHERE id='hierarchy'").fetchone())
                outcome = state.execute('SELECT verdict,diff_json,error FROM checks').fetchone()
                self.assertEqual(outcome[0], 'pass', tuple(outcome))
            finally:
                state.close()

    def test_missing_handlers_execute_production_and_detect_a_removed_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Base.java').write_text('package example;\ninterface Base {}\n')
            (root / 'Child.java').write_text('package example;\nimport java.util.List;\nclass Child implements Base {\n    void work() {}\n    void caller() { work(); }\n    Runnable task = this::work;\n}\n')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'synthetic')
            state = connect(directory / 'evidence.sqlite')
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(root, binary, database, state, Oracle())
                for feature, subject in [('outline', 'Child.java'), ('imports', 'Child.java'),
                                         ('search', 'work'), ('implementations', 'Base'), ('hierarchy', 'Base'),
                                         ('refs', 'work'), ('usages', 'work'), ('callers', 'work'),
                                         ('stats', 'index-state'), ('query', 'index-state'), ('schema', 'index-state'), ('db-path', 'index-state')]:
                    with self.subTest(feature=feature):
                        state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)', (feature, feature, subject))
                        state.commit()
                        check = state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
                        fixture.evaluate(check)
                        outcome = state.execute('SELECT verdict,diff_json,error FROM checks WHERE id=?', (feature,)).fetchone()
                        self.assertEqual(outcome[0], 'pass', (outcome[1], outcome[2]))
                (root / 'Child.java').write_text('package example;\nclass Child {}\n')
                check = state.execute("SELECT * FROM checks WHERE feature='outline'").fetchone()
                fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
