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
    def temporary_directory(self):
        boundary = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        boundary.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix='navigation-', dir=boundary)

    def test_enum_reference_position_uses_the_declaration_anchor(self):
        with self.temporary_directory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Mode.java').write_text('package example;\nenum Mode { ON(1), OFF(2); Mode(int n) {} }\n')
            (root / 'Client.java').write_text('package example;\nclass Client { Mode mode = Mode.ON; }\n')
            class EnumOracle:
                def call(self, tool, arguments):
                    if tool == 'ide_find_symbol':
                        return {'symbols': [{'name': 'ON', 'kind': 'CLASS', 'file': 'Mode.java',
                            'line': 2, 'column': 30, 'qualifiedName': 'example.Mode.ON'}]}
                    if tool == 'ide_find_references':
                        if 'symbol' not in arguments:
                            if arguments['column'] != 13:
                                raise AssertionError('wrong declaration anchor')
                            return {'resolvedSymbol': {'name': 'Mode', 'kind': 'constructor'}, 'usages': []}
                        if arguments['symbol'] != 'example.Mode#ON':
                            raise AssertionError('wrong qualified member')
                        return {'resolvedSymbol': {'name': 'ON', 'kind': 'constant field'},
                                'usages': [{'file': 'Client.java', 'line': 2, 'type': 'REFERENCE'}]}
                    raise AssertionError(tool)
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'enum-anchor')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('enum','refs','ON')")
                state.commit()
                fixture = Fixture(root, binary, database, state, EnumOracle())
                fixture.evaluate(state.execute('SELECT * FROM checks').fetchone())
                result = state.execute('SELECT verdict,error,diff_json FROM checks').fetchone()
                self.assertEqual(result[0], 'pass', tuple(result))
            finally:
                state.close()

    def test_implicit_enum_constructor_sites_do_not_become_lexical_type_mentions(self):
        with self.temporary_directory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Mode.java').write_text('package example;\nenum Mode {\n    ON(1);\n    Mode(int n) {}\n}\n')
            class ImplicitOracle:
                def call(self, tool, arguments):
                    if tool == 'ide_find_symbol':
                        return {'symbols': [{'name': 'Mode', 'kind': 'CLASS', 'file': 'Mode.java',
                            'line': 2, 'column': 6, 'qualifiedName': 'example.Mode'}]}
                    if tool == 'ide_find_references':
                        return {'resolvedSymbol': {'name': 'Mode', 'kind': 'enum'},
                                'usages': [{'file': 'Mode.java', 'line': 3, 'type': 'REFERENCE'}]}
                    raise AssertionError(tool)
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'implicit-enum')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('enum','refs','Mode')")
                state.commit()
                fixture = Fixture(root, binary, database, state, ImplicitOracle())
                fixture.evaluate(state.execute('SELECT * FROM checks').fetchone())
                result = state.execute('SELECT verdict,error,diff_json FROM checks').fetchone()
                self.assertEqual(result[0], 'pass', tuple(result))
            finally:
                state.close()

    def test_enum_normalization_preserves_explicit_mentions_and_detects_native_omissions(self):
        with self.temporary_directory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Mode.java').write_text('package example;\nenum Mode {\n'
                '    ON(1),\n    OFF(2) { Mode self() { return Mode.ON; } };\n'
                '    Mode(int n) {}\n}\nclass Client { Mode value = Mode.ON; }\n')

            class EnumOracle:
                def call(self, tool, arguments):
                    if tool == 'ide_find_symbol':
                        return {'symbols': [{'name': 'Mode', 'kind': 'CLASS', 'file': 'Mode.java',
                            'line': 2, 'column': 6, 'qualifiedName': 'example.Mode'}]}
                    if tool == 'ide_find_references':
                        return {'resolvedSymbol': {'name': 'Mode', 'kind': 'enum'},
                                'usages': [{'file': 'Mode.java', 'line': line, 'type': 'REFERENCE'}
                                           for line in (3, 4, 7)]}
                    raise AssertionError(tool)

            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'explicit-enum')
            state, native = connect(directory / 'checks.sqlite'), connect(database)
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(root, binary, database, state, EnumOracle())
                for feature in ('refs', 'usages'):
                    with state:
                        state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                                      (feature, feature, 'Mode'))
                    check = state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
                    fixture.evaluate(check)
                    result = state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?',
                                           (feature,)).fetchone()
                    self.assertEqual(result[0], 'pass', tuple(result))
                    expected = json.loads(state.execute('SELECT expected_json FROM checks WHERE id=?',
                                                        (feature,)).fetchone()[0])
                    self.assertEqual({entry['line'] for entry in expected['usages']}, {4, 7})
                # Mutate only the disposable native index. Same-line enum calls
                # must not hide a missing explicit type mention after normalization.
                with native:
                    native.execute("DELETE FROM refs WHERE name='Mode' AND line=4")
                for check in state.execute('SELECT * FROM checks'):
                    fixture.evaluate(check)
                    result = state.execute('SELECT verdict,diff_json FROM checks WHERE id=?',
                                           (check['id'],)).fetchone()
                    self.assertEqual(result[0], 'fail')
                    missing = json.loads(result[1])['missing']
                    expected = ['usage', 'Mode.java', 4] if check['feature'] == 'refs' else ['Mode.java', 4]
                    self.assertIn(expected, missing)
            finally:
                native.close()
                state.close()

    def test_same_line_overloads_detect_one_removed_production_index_row(self):
        with self.temporary_directory() as temporary:
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
        with self.temporary_directory() as temporary:
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
        with self.temporary_directory() as temporary:
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
