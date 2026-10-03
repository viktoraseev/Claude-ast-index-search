"""Source structure is independently parsed by javac, then checked against the CLI."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit import Fixture, SCHEMA
from build_index import build_ast_index
from common import connect
from java_structure import structure_server


class StructureContracts(unittest.TestCase):
    def test_annotated_enum_constants_keep_identifier_anchors_and_ordinary_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / 'Probe.java'
            source.write_text('''enum Probe {
    @Deprecated
    FIRST(1),
    @SuppressWarnings("unused") SECOND(2) { @Override int code() { return 2; } };
    final int value;
    static final Object helper = new Object();
    Probe(int value) { this.value = value; }
    int code() { return value; }
}
''')
            entries = structure_server(directory).read(source)
            constants = [(entry['name'], entry['line'], entry.get('qualified_name'))
                         for entry in entries if entry['kind'] == 'constant']
            self.assertEqual(constants, [('FIRST', 3, 'Probe.FIRST'), ('SECOND', 4, 'Probe.SECOND')])
            fields = [(entry['name'], entry['line']) for entry in entries if entry['kind'] == 'property']
            self.assertEqual(fields, [('value', 5), ('helper', 6)])

    def test_records_constructors_annotations_and_mutation_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Example.java').write_text('''package example;
record Example(int value) {
    Example {}
    int value(int extra) { return extra; }
    @java.lang.Override public String toString() { return "ok"; }
}
class Plain { Plain() {} Plain(int n) {} }
''')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'structure')
            state = connect(directory / 'checks.sqlite')
            native = connect(database)
            try:
                state.executescript(SCHEMA)
                state.execute("INSERT INTO checks(id,feature,subject) VALUES ('structure','outline:constructors','Example.java')")
                state.commit()
                check = state.execute("SELECT * FROM checks WHERE id='structure'").fetchone()
                fixture = Fixture(root, binary, database, state, Mock())
                fixture.evaluate(check)
                outcome = state.execute('SELECT verdict,diff_json,error FROM checks').fetchone()
                self.assertEqual(outcome[0], 'pass', tuple(outcome))
                # Neither the source oracle nor expected multiplicity depends on the native DB.
                with native:
                    native.execute("DELETE FROM symbols WHERE kind='function' AND name='value' AND line=2")
                fixture.evaluate(check)
                outcome = state.execute('SELECT verdict,diff_json FROM checks').fetchone()
                self.assertEqual(outcome[0], 'fail')
                self.assertEqual(json.loads(outcome[1])['missing'], [['index', 'value', 'function', 2]])
            finally:
                native.close()
                state.close()

    def test_record_navigation_keeps_the_field_and_checks_accessor_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'Example.java').write_text('record Example(int value) {}\n')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(directory, Path('/binary'), directory / 'index.sqlite', state, Mock())
                field = {'name': 'value', 'line': 1, 'path': 'Example.java', 'kind': 'property'}
                accessor = {**field, 'kind': 'function'}
                self.assertEqual(fixture.native_navigation_items([field, accessor]), [field])
                # Plain methods with the same name are never normalized as record accessors.
                (directory / 'Example.java').write_text('class Example { void value() {} void value(int n) {} }\n')
                self.assertEqual(fixture.native_navigation_items([accessor, accessor]), [accessor, accessor])
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
