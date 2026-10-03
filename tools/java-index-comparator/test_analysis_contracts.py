"""Production checks for analysis and management, with no MCP oracle claim."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect


class AnalysisContracts(unittest.TestCase):
    def test_missing_contracts_execute_cli_and_reject_wrong_results(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Example.java').write_text('''class Example {
    void unused() {}
    void used() {}
    void consume() { used(); }
}
''')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'analysis')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(root, binary, database, state, None)
                for feature in ('unused-symbols', 'version', 'list-roots', 'subtree:list'):
                    state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                                  (feature, feature, 'index-state'))
                    state.commit()
                    check = state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
                    fixture.evaluate(check)
                    result = state.execute('SELECT verdict,error FROM checks WHERE id=?', (feature,)).fetchone()
                    self.assertEqual(result[0], 'pass', tuple(result))
                # Valid JSON with the wrong analysis result must fail.
                check = state.execute("SELECT * FROM checks WHERE feature='unused-symbols'").fetchone()
                with patch.object(fixture, 'cli', return_value=[]):
                    fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
                # The Maven family now has source contracts; unsupported Java
                # Gradle models must still remain pending.
                (root / 'build.gradle').write_text("plugins { id 'java' }\n")
                plan(state, [{'path': 'Example.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files', [], root)
                reasons = dict(state.execute('SELECT feature,reason FROM coverage'))
                for feature in ('unused-symbols', 'version', 'list-roots', 'subtree:list'):
                    self.assertTrue(reasons[feature].startswith('internal CLI'), reasons[feature])
                self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='module'").fetchone()[0], 'pending')
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
