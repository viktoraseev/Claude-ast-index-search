"""Java repair scope is explicit; excluded languages never count as passes."""
from pathlib import Path
import tempfile
import unittest

from audit import JAVA_EXCLUDED_FEATURES, SCHEMA, plan, next_check
from replay import problem_batch
from common import connect
import annotation_contracts
import android_contracts
import android_syntax_contracts
import java_resource_contracts
import android_dependency_contracts


class JavaScopeTests(unittest.TestCase):
    def test_mixed_legacy_xml_failures_leave_java_projection_pending_and_ids_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = connect(Path(temporary) / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            state.execute("INSERT INTO metadata VALUES ('audit_scope','java')")
            rows = [('mixed-' + feature, feature, 'captured', 'complete', 'fail')
                    for feature in sorted(android_dependency_contracts.LEGACY_FEATURES)]
            state.executemany('INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,?,?)', rows)
            android_dependency_contracts.plan_dependencies(state, Path(temporary), java_only=True)
            self.assertEqual(list(problem_batch(state, 100)), [])
            self.assertIn(next_check(state)['feature'], android_dependency_contracts.JAVA_FEATURES)
            for row in rows:
                retained = state.execute('SELECT id,feature,subject,status,verdict FROM checks WHERE id=?', (row[0],)).fetchone()
                self.assertEqual(tuple(retained), row)
                self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (row[1],)).fetchone()[0], 'out-of-scope')
            for feature in android_dependency_contracts.JAVA_FEATURES:
                check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
                state.execute("UPDATE checks SET status='complete',verdict='fail' WHERE id=?", (check['id'],))
            self.assertEqual({r['feature'] for r in problem_batch(state, 100)}, android_dependency_contracts.JAVA_FEATURES)

    def test_java_scope_excludes_xml_only_criteria_preserving_legacy_ids_and_java_ownership(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Sentinel.java').write_text('class Sentinel {}\n')
            (root / 'AndroidManifest.xml').write_text('<manifest/>\n')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            help_text = '  class  Classes\n  symbol  Symbols\n  file  Files'
            plan(state, [{'path': 'Sentinel.java'}], help_text, root=root)
            prior = {tuple(row) for row in state.execute('SELECT id,feature,subject FROM checks')}
            plan(state, [{'path': 'Sentinel.java'}], help_text, root=root, java_only=True)
            self.assertTrue(prior.issubset({tuple(row) for row in state.execute('SELECT id,feature,subject FROM checks')}))
            self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
            for feature in android_syntax_contracts.FEATURES:
                self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'out-of-scope')
            for feature in java_resource_contracts.FEATURES:
                self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'implemented')
            parent = state.execute("SELECT * FROM coverage WHERE feature='android:syntax-resolution'").fetchone()
            self.assertEqual(parent['status'], 'pending')
            self.assertIn('XML-only', parent['reason'])
            self.assertIn('out-of-scope', parent['reason'])
            self.assertEqual(state.execute("SELECT count(*) FROM checks WHERE verdict='pass'").fetchone()[0], 0)

    def test_legacy_foreign_pending_and_errors_do_not_schedule_or_hide_java_failures(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            state = connect(Path(temporary) / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            with state:
                state.execute("INSERT INTO metadata VALUES ('audit_scope','java')")
                state.executemany('INSERT INTO checks(id,feature,subject,status,verdict) VALUES (?,?,?,?,?)', [
                    ('foreign-pending', 'xml-usages:syntax', 'old', 'pending', None),
                    ('foreign-fail', 'resource-usages:xml-syntax', 'old', 'complete', 'fail'),
                    ('foreign-error', 'composables', 'old', 'complete', 'error'),
                    ('java-fail', 'graph', 'Java', 'complete', 'fail'),
                    ('java-error', 'resource-usages', 'Java', 'complete', 'error'),
                    ('outline', 'outline', 'A.java', 'pending', None)])
                state.execute("INSERT INTO coverage VALUES ('xml-usages:syntax','pending','legacy')")
            self.assertEqual([row['id'] for row in problem_batch(state, 1)], ['java-fail', 'java-error'])
            self.assertIsNone(next_check(state))
            with state:
                state.execute("UPDATE checks SET verdict='pass' WHERE id IN ('java-fail','java-error')")
            self.assertEqual(next_check(state)['id'], 'outline')
            self.assertEqual(state.execute('SELECT count(*) FROM checks').fetchone()[0], 6)
            self.assertEqual(state.execute("SELECT status,verdict FROM checks WHERE id='foreign-pending'").fetchone()['status'], 'pending')

    def test_java_audit_keeps_framework_contracts_pending_for_non_java_markers(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            (root / 'Sentinel.java').write_text('class Sentinel {}\n')
            resource = root / '.generated' / 'res' / 'values' / 'strings.xml'
            resource.parent.mkdir(parents=True)
            resource.write_text('<resources><string name="title">Sample</string></resources>')
            (root / 'build.gradle').write_text("plugins { id 'com.android.library' }\n")
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols',
                 [], root, java_only=True)
            self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 3)
            for feature in android_contracts.FEATURES:
                self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature + ':target',)).fetchone()[0], 'pending')
            self.assertEqual(state.execute("SELECT count(*) FROM checks WHERE subject='target-absence'").fetchone()[0], 0)
            self.assertEqual(state.execute("SELECT count(*) FROM checks WHERE verdict='pass'").fetchone()[0], 0)

    def test_every_existing_java_case_survives_and_shared_contracts_remain_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            states = [connect(root / name) for name in ('all.sqlite', 'java.sqlite')]
            for state in states:
                self.addCleanup(state.close)
                state.executescript(SCHEMA)
            help_text = '  class  Classes\n  symbol  Symbols\n  file  Files\n  module  Modules'
            plan(states[0], [{'path': 'Example.java'}], help_text, ['method'])
            plan(states[1], [{'path': 'Example.java'}], help_text, ['method'], java_only=True)
            prior = {tuple(row) for row in states[0].execute('SELECT id,feature,subject FROM checks')}
            current = {tuple(row) for row in states[1].execute('SELECT id,feature,subject FROM checks')}
            self.assertEqual(prior, current)
            coverage = {row['feature']: row['status'] for row in states[1].execute('SELECT * FROM coverage')}
            self.assertTrue(all(coverage[feature] == 'out-of-scope' for feature in JAVA_EXCLUDED_FEATURES))
            self.assertEqual(coverage['module'], 'pending')
            self.assertEqual(coverage['search:rank-presets'], 'implemented')
            self.assertEqual(states[1].execute("SELECT count(*) FROM checks WHERE verdict='pass'").fetchone()[0], 0)

    def test_foreign_sources_do_not_become_absent_or_java_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Example.java').write_text('class Example {}')
            (root / 'Script.kts').write_text('@Composable @Preview fun sample() {}')
            (root / 'Sample.pm').write_text('sub sample {}')
            state = connect(root / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            plan(state, [{'path': 'Example.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
                 [], root, java_only=True)
            for feature in JAVA_EXCLUDED_FEATURES:
                self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'out-of-scope')
                archived = list(state.execute('SELECT status,verdict,expected_json,actual_json FROM checks WHERE feature=?', (feature,)))
                if feature in android_dependency_contracts.LEGACY_FEATURES:
                    self.assertEqual([tuple(row) for row in archived], [('pending', None, None, None)])
                else:
                    self.assertEqual(archived, [])
            self.assertNotIn(next_check(state)['feature'], JAVA_EXCLUDED_FEATURES)
            self.assertEqual(annotation_contracts.applicability(state, 'composables')[0], 'out-of-scope')
            self.assertEqual([row['path'] for row in annotation_contracts.applicable_paths(state, 'provides')], ['Example.java'])
            self.assertEqual(state.execute("SELECT count(*) FROM file_inventory WHERE extension IN ('.kts','.pm')").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
