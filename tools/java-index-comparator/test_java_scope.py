"""Java repair scope is explicit; excluded languages never count as passes."""
from pathlib import Path
import tempfile
import unittest

from audit import JAVA_EXCLUDED_FEATURES, SCHEMA, plan
from common import connect
import annotation_contracts


class JavaScopeTests(unittest.TestCase):
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
            self.assertEqual(coverage['search:rank-presets'], 'pending')
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
                self.assertEqual(state.execute('SELECT count(*) FROM checks WHERE feature=?', (feature,)).fetchone()[0], 0)
            self.assertEqual(annotation_contracts.applicability(state, 'composables')[0], 'out-of-scope')
            self.assertEqual([row['path'] for row in annotation_contracts.applicable_paths(state, 'provides')], ['Example.java'])
            self.assertEqual(state.execute("SELECT count(*) FROM file_inventory WHERE extension IN ('.kts','.pm')").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
