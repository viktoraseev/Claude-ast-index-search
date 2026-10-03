"""Public synthetic XML checks execute production indexing and rendered CLI."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA
from common import connect
import android_syntax_contracts as contracts


class AndroidSyntaxTests(unittest.TestCase):
    def test_equivalent_character_references_preserve_class_and_resource_usages(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        # The live unified fixture includes encoded attributes/definitions;
        # its plain spelling must have identical production observations.
        layout = contracts.LAYOUT.replace('fixture.Outer&#36;Inner', 'fixture.Outer$Inner') \
            .replace('&#64;string/title', '@string/title') \
            .replace('&#x40;+id/inner', '@+id/inner')
        values = contracts.VALUES.replace('t&#x69;tle', 'title')
        self.assertNotEqual(layout, contracts.LAYOUT)
        self.assertNotEqual(values, contracts.VALUES)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary, \
                patch.object(contracts, 'LAYOUT', layout), patch.object(contracts, 'VALUES', values):
            expected, actual = contracts.exercise(binary, Path(temporary).resolve())
        failures = [(feature, key) for feature in contracts.FEATURES for key in expected[feature]
                    if expected[feature][key] != actual[feature][key]]
        self.assertEqual(failures, [])

    def test_production_xml_syntax_and_corrupt_output(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary)
            root = directory / 'target'
            root.mkdir()
            (root / 'Example.java').write_text('class Example {}')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            oracle = Mock()
            oracle.call.side_effect = AssertionError('independent fixture must not call MCP')
            fixture = Fixture(root, binary, directory / 'unused.sqlite', state, oracle)
            contracts.plan_syntax(state, root)
            failures = []
            for check in state.execute('SELECT * FROM checks ORDER BY feature'):
                fixture.evaluate(check)
                row = state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
                diff = json.loads(row['diff_json'] or '{}')
                if row['verdict'] != 'pass':
                    failures.append((check['feature'], row['verdict'], len(diff.get('missing', [])),
                                     len(diff.get('unexpected', [])), row['error']))
                self.assertIn('not MCP equivalence', json.loads(row['expected_json'] or '{}').get('source', ''))
            self.assertEqual(failures, [])
            self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
            self.assertEqual([p.name for p in root.iterdir()], ['Example.java'])
            self.assertFalse(fixture.database.exists())
            fixture._android_syntax_results = None
            with patch.object(contracts, 'exercise', return_value=(
                    {f: {'result': 1} for f in contracts.FEATURES},
                    {f: {'result': 0} for f in contracts.FEATURES})):
                for check in state.execute('SELECT * FROM checks'):
                    fixture.evaluate(check)
            self.assertEqual({r[0] for r in state.execute('SELECT verdict FROM checks')}, {'fail'})


if __name__ == '__main__':
    unittest.main()
