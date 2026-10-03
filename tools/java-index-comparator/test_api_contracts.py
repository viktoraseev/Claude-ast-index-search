"""Independent javac visibility checks against the production public API command."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect


class ApiContracts(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / 'settings.gradle').write_text("rootProject.name = 'api-fixture'\n")
        (self.root / 'Public.java').write_text('''package example;
// public class CommentOnly {}
@Deprecated
public final class Public {
    public Public() {}
    @Deprecated
    public
    void exposed() {}
    public static final int VALUE = 1;
    protected void inheritedOnly() {}
    private void hidden() {}
    void packageOnly() {}
    private class Hidden { public void nested() {} }
    public class Nested { public void visible() {} }
    public void local() { class Local { public void hidden() {} } }
    public String text = "public class StringOnly {}";
}
class PackageOnly { public void hidden() {} }
''')
        (self.root / 'Service.java').write_text('''package example;
public interface Service {
    int VALUE = 1;
    void work();
    private void helper() {}
    class Nested { public void work() {} }
}
''')
        (self.root / 'Mode.java').write_text('''package example;
public enum Mode {
    ON, OFF;
    Mode() {}
    public void run() {}
}
''')
        (self.root / 'Value.java').write_text('''package example;
public record Value(
    int number,
    String... tags
) {
    public Value {}
}
''')
        (self.root / 'Flag.java').write_text('''package example;
public @interface Flag {
    String value();
}
''')
        (self.root / 'Other.kt').write_text('class Foreign\n')
        scoped = self.root / 'nested' / 'sub'
        scoped.mkdir(parents=True)
        (scoped / 'Facade.java').write_text('package nested.sub;\npublic class Facade { public void work() {} }\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.database = self.directory / 'index.sqlite'
        build_ast_index(str(self.binary), self.root, self.database, 'public-api')
        self.state = connect(self.directory / 'checks.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('api','api',?)",
                           (json.dumps({'module': ''}),))
        self.state.commit()
        self.fixture = Fixture(self.root, self.binary, self.database, self.state, None)
        self.check = self.state.execute("SELECT * FROM checks WHERE id='api'").fetchone()

    def test_public_java_api_executes_production_with_javac_visibility(self):
        self.fixture.evaluate(self.check)
        result = self.state.execute("SELECT verdict,error,diff_json FROM checks WHERE id='api'").fetchone()
        self.assertEqual(result[0], 'pass', tuple(result))
        declarations = {(entry['name'], entry['line']) for entry in self.fixture.structure('Public.java')
                        if entry.get('public_api')}
        self.assertIn(('exposed', 8), declarations)
        self.assertIn(('VALUE', 9), declarations)
        self.assertNotIn(('nested', 13), declarations)
        self.assertNotIn(('hidden', 18), declarations)

    def test_valid_text_with_wrong_java_locations_fails(self):
        with patch.object(self.fixture, 'text_cli', return_value="Public API of '.' (0):\n  No public API found.\n"):
            self.fixture.evaluate(self.check)
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='api'").fetchone()[0], 'fail')

    def test_varargs_record_components_and_accessors_reach_outline_and_index(self):
        with self.state:
            self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('record','outline:constructors','Value.java')")
        check = self.state.execute("SELECT * FROM checks WHERE id='record'").fetchone()
        self.fixture.evaluate(check)
        result = self.state.execute("SELECT verdict,error,diff_json FROM checks WHERE id='record'").fetchone()
        self.assertEqual(result[0], 'pass', tuple(result))
        items = self.fixture.cli('symbol', 'tags', '--in-file', 'Value.java')['items']
        self.assertEqual({item['kind'] for item in items}, {'property', 'function'})
        accessor = next(item for item in items if item['kind'] == 'function')
        self.assertEqual(accessor['signature'], 'String[] tags()')

    def test_directory_scope_excludes_other_public_declarations(self):
        with self.state:
            self.state.execute("UPDATE checks SET subject=? WHERE id='api'", (json.dumps({'module': 'nested/sub'}),))
        check = self.state.execute("SELECT * FROM checks WHERE id='api'").fetchone()
        self.fixture.evaluate(check)
        result = self.state.execute("SELECT verdict,error,diff_json FROM checks WHERE id='api'").fetchone()
        self.assertEqual(result[0], 'pass', tuple(result))

    def test_api_reads_java_source_without_an_index(self):
        output = subprocess.run([str(self.binary), 'api', 'nested.sub', '--limit', '3'],
            cwd=self.root, env={**os.environ, 'NO_COLOR': '1',
                'AST_INDEX_DB_PATH': str(self.directory / 'absent.sqlite'),
                'AST_INDEX_CACHE_DIR': str(self.directory / 'absent-cache')},
            capture_output=True, text=True, check=True)
        self.assertEqual(output.stdout.splitlines()[0], "Public API of 'nested.sub' (1):")
        self.assertIn('  nested/sub/Facade.java:2\n', output.stdout)
        self.assertNotIn('Public.java:', output.stdout)

    def test_full_inventory_does_not_make_applicable_api_inapplicable(self):
        plan(self.state, [{'path': 'Public.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             [], self.root, java_only=True)
        coverage = self.state.execute("SELECT status,reason FROM coverage WHERE feature='api'").fetchone()
        self.assertEqual(coverage[0], 'implemented')
        self.assertTrue(coverage[1].startswith('independent JDK'), tuple(coverage))
        self.assertEqual(self.state.execute("SELECT count(*) FROM file_inventory WHERE extension='.kt'").fetchone()[0], 1)
        # An applicable API with an entirely wrong result cannot be skipped.
        def foreign_only(*arguments):
            return ("Public API of '.' (0):\n" if arguments[-1] == '0' else
                    "Public API of '.' (1):\n  Other.kt:1\n    class Foreign\n")
        with patch.object(self.fixture, 'text_cli', side_effect=foreign_only):
            self.fixture.evaluate(self.check)
        self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='api'").fetchone()[0], 'fail')


if __name__ == '__main__':
    unittest.main()
