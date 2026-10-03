"""Small synthetic fixture checks actual Java indexing and rendered commands."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, required_features
from common import connect, ToolError
import java_resource_contracts as contracts


class JavaResourceContractsTests(unittest.TestCase):
    def test_production_locations_ownership_and_corrupt_output(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary).resolve()
            root = directory / 'target'
            root.mkdir()
            (root / 'Sentinel.java').write_text('class Sentinel {}')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            oracle = Mock()
            oracle.call.side_effect = AssertionError('independent source fixture must not call MCP')
            fixture = Fixture(root, binary, directory / 'unused.sqlite', state, oracle)
            contracts.plan_java_resources(state, root)
            failures = []
            for check in state.execute('SELECT * FROM checks ORDER BY feature'):
                fixture.evaluate(check)
                row = state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                if row['verdict'] != 'pass':
                    diff = json.loads(row['diff_json'] or '{}')
                    failures.append((check['feature'], row['verdict'],
                                     len(diff.get('missing', [])), len(diff.get('unexpected', []))))
                self.assertIn('not MCP equivalence', json.loads(row['expected_json'] or '{}')['source'])
            self.assertEqual(failures, [])
            self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
            self.assertEqual([p.name for p in root.iterdir()], ['Sentinel.java'])
            self.assertFalse(fixture.database.exists())
            fixture._java_resource_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {f: {'value': 1} for f in contracts.FEATURES},
                    {f: {'value': 0} for f in contracts.FEATURES})):
                for check in state.execute('SELECT * FROM checks'):
                    fixture.evaluate(check)
            self.assertEqual({r[0] for r in state.execute('SELECT verdict FROM checks')}, {'fail'})

    def test_required_contracts_and_disposable_boundary(self):
        self.assertTrue(contracts.FEATURES <= required_features())
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(Path('target/release/ast-index'), Path('/private/tmp'))


if __name__ == '__main__':
    unittest.main()
