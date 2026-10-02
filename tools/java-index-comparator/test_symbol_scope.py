"""Public, synthetic cases for differences in IDE navigation scope."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit import Fixture, SCHEMA
from common import connect
from build_index import build_ast_index


class ProductionScopeTests(unittest.TestCase):
    def test_symbol_navigation_preserves_methods_but_excludes_constructors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            (root / 'Example.java').write_text('''package example;
class Example {
    @Label(names = {"a", "b"}) Example() {}
    int Example(int value) { return value; }
    Runnable task = new Runnable() {
        public void run() {}
    };
}
''')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = Path(temporary) / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'synthetic')
            state = connect(Path(temporary) / 'evidence.sqlite')
            try:
                state.executescript(SCHEMA)
                client = Mock()
                fixture = Fixture(root, binary, database, state, client)
                for name, expected in [
                    ('Example', [
                        {'name': 'Example', 'kind': 'CLASS', 'file': 'Example.java', 'line': 2, 'qualifiedName': 'example.Example'},
                        {'name': 'Example', 'kind': 'METHOD', 'file': 'Example.java', 'line': 4, 'qualifiedName': 'example.Example.Example'},
                    ]),
                    ('run', [{'name': 'run', 'kind': 'METHOD', 'file': 'Example.java', 'line': 6, 'qualifiedName': 'example.Example.run'}]),
                ]:
                    with self.subTest(name=name):
                        client.call.return_value = {'symbols': expected}
                        state.execute("INSERT INTO checks(id,feature,subject) VALUES (?,'symbol',?)", (name, name))
                        state.commit()
                        fixture.evaluate(state.execute('SELECT * FROM checks WHERE id=?', (name,)).fetchone())
                        self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (name,)).fetchone()[0], 'pass')
                # The CLI still supports explicit constructor lookup; the IDE's
                # Go-to-Symbol contract is narrower than ast-index's symbol command.
                self.assertEqual(len(fixture.cli('symbol', 'Example')['items']), 3)
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
