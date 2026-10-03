"""Java project profiling: source identities, import counts and inventory scope."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import connect
import mobile_contracts
import profile_contracts


class ProfileContracts(unittest.TestCase):
    def test_inventory_never_turns_present_or_linked_jvm_markers_into_absence(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                with self.assertRaises(profile_contracts.Unresolved):
                    profile_contracts.jvm_markers(state, root)
                profile_contracts.plan_profiles(state, root)
                self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='detect-stacks'").fetchone()[0], 'pending')
                (root / 'pom.xml').write_text('<project/>')
                (root / 'nested').mkdir()
                (root / 'nested/build.gradle').write_text("plugins { id 'java' }")
                (root / 'tests').mkdir()
                (root / 'tests/pom.xml').write_text('<project/>')
                (root / 'other.swift').write_text('// inventory only; no foreign parser assertion\n')
                mobile_contracts.inventory(state, root)
                self.assertEqual(profile_contracts.jvm_markers(state, root), ['nested/build.gradle', 'pom.xml'])
                self.assertEqual(state.execute("SELECT count(*) FROM file_inventory WHERE extension='.swift'").fetchone()[0], 1)
                (root / 'build.gradle').symlink_to('nested/build.gradle')
                mobile_contracts.inventory(state, root)
                with self.assertRaises(profile_contracts.Unresolved):
                    profile_contracts.jvm_markers(state, root)
                profile_contracts.plan_profiles(state, root)
                self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='detect-stacks'").fetchone()[0], 'pending')
            finally:
                state.close()

    def test_java_conventions_use_complete_imports_once_and_case_sensitive_suffixes(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            sources = {
                'domain/FirstService.java': '''import org.junit.Test;
import static org.junit.Assert.*;
import com.fasterxml.jackson /* prefix */ . databind . ObjectMapper;
class FirstService {}
''',
                'data/SecondService.java': '''import org.junit.*;
import org.springframework.context.ApplicationContext;
class SecondService {}
''',
                'presentation/ThirdService.java': '''import com.google.gson.Gson;
class ThirdService {}
class WrongSERVICE {}
''',
            }
            for path, content in sources.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content)
            (root / 'pom.xml').write_text('<project><groupId>sample</groupId><artifactId>sample</artifactId></project>')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'profile')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                fixture = Fixture(root, binary, database, state, None)
                result = fixture.cli('conventions')
                self.assertEqual(result['frameworks'], {
                    'Testing': [{'name': 'JUnit', 'count': 3}],
                    'DI': [{'name': 'Spring', 'count': 1}],
                    'Serialization': [{'name': 'Gson', 'count': 1}, {'name': 'Jackson', 'count': 1}],
                })
                self.assertEqual(result['naming_patterns'], [{'suffix': 'Service', 'count': 3}])
                self.assertEqual(result['architecture'], ['Clean Architecture'])
                plan(state, [{'path': path} for path in sources],
                     '  class  Classes\n  symbol  Symbols\n  file  Files', root=root, java_only=True)
                for feature in ('conventions', 'detect-stacks'):
                    check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                    self.assertIsNotNone(check)
                    fixture.evaluate(check)
                    evidence = state.execute('SELECT verdict,diff_json,error FROM checks WHERE id=?', (check['id'],)).fetchone()
                    self.assertEqual(evidence[0], 'pass', (feature, tuple(evidence)))
                    real_cli = fixture.cli

                    def omit(*args):
                        result = real_cli(*args)
                        if args[0] == 'conventions':
                            result['frameworks'] = {}
                        else:
                            result['stacks'] = []
                        return result

                    with patch.object(fixture, 'cli', side_effect=omit):
                        fixture.evaluate(check)
                    self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
                    reason = state.execute('SELECT reason FROM coverage WHERE feature=?', (feature,)).fetchone()[0]
                    self.assertIn('not MCP equivalence', reason)
                self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
                # Foreign rows are a normalization baseline only. They must
                # not hide a missing Java package or count as foreign coverage.
                native = connect(database)
                try:
                    with native:
                        file_id = native.execute("INSERT INTO files(path,mtime,size) VALUES ('foreign.data',0,0)").lastrowid
                        native.executemany('INSERT INTO symbols(file_id,name,kind,line) VALUES (?,?,?,?)',
                                           [(file_id, 'FourthSERVICE', 'class', 1),
                                            (file_id, 'org.junit.Foreign', 'import', 2)])
                finally:
                    native.close()
                check = state.execute("SELECT * FROM checks WHERE feature='conventions'").fetchone()
                fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'pass')
                combined = fixture.cli('conventions')
                self.assertEqual(combined['naming_patterns'], [{'suffix': 'Service', 'count': 4}])
                self.assertEqual(combined['frameworks']['Testing'], [{'name': 'JUnit', 'count': 4}])
                # A compact generated DB exercises the former 50,000-path
                # cap without thousands of files or repeated test cases.
                native = connect(database)
                try:
                    with native:
                        native.executemany('INSERT INTO files(path,mtime,size) VALUES (?,0,0)',
                                           ((f'aaa/{i:05}.data',) for i in range(50001)))
                finally:
                    native.close()
                fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'pass')
                self.assertEqual(fixture.cli('conventions')['architecture'], ['Clean Architecture'])
                # Exhausted scanner budgets cannot establish marker absence.
                check = state.execute("SELECT * FROM checks WHERE feature='detect-stacks'").fetchone()
                with patch.object(fixture, 'cli', return_value={'scan_truncated': True, 'stacks': []}):
                    fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'unsupported')
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
