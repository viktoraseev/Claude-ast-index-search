"""Execute Java generic field binding and preserve unresolved obligations."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ToolError, connect
from audit import Fixture, SCHEMA, required_features
import java_generic_field_contracts as fields


class GenericFieldContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_generic_fields(self):
        # Retain the reviewed population even if a source/fixture edit removes
        # the corresponding body or expectation. Later cases may be added.
        self.assertTrue(set('direct ordered nested sourceNested subtype rawBound captured reference '
                            'colliding wildcardBound wildcardNested'.split()) <= fields.ORIGINAL_CALLS.keys())
        self.assertTrue(set('inherited reordered relayed nestedParent inheritedBound inheritedRaw '
                            'rawSubclass inheritedWildcard inheritedNested inheritedCaptured '
                            'inheritedReference inheritedHiding inheritedLocal crossFile'.split())
                        <= fields.INHERITED_CALLS.keys())
        self.assertTrue(set('inheritedBare inheritedThis inheritedSuper inheritedLambda'.split())
                        <= fields.LEXICAL_CALLS.keys())
        expected, actual = fields.exercise(self.binary, self.directory)
        differences = {feature: [key for key, want in expected[feature].items()
                                 if actual[feature].get(key) != want] for feature in sorted(fields.FEATURES)}
        self.assertEqual(differences, {feature: [] for feature in sorted(fields.FEATURES)})
        # Every inherited field obligation executes each graph page and a path,
        # in both roots. The original population remains a separate full pass.
        for prefix in ('', 'extra/'):
            for phase, calls in (('inherited/', fields.INHERITED_CALLS), ('lexical/', fields.LEXICAL_CALLS)):
                for name, targets in calls.items():
                    for limit in (0, 1, 100):
                        for ambiguous in (False, True):
                            key = f'{phase}{prefix}{name}:{limit}:{ambiguous}'
                            self.assertIn(key, expected[fields.GRAPH])
                    for target in targets:
                        self.assertIn(f'{phase}{prefix}{name}:path:{target[2]}', expected[fields.GRAPH])
            for target in ('fieldAlpha', 'fieldBeta'):
                for phase in ('', 'inherited/', 'lexical/'):
                    key = phase + prefix + target + ':callers'
                    self.assertIn(key, expected[fields.EXPLORE])
                    self.assertLessEqual(len(expected[fields.EXPLORE][key]), 10)
        for guard in ('inherited-raw', 'inherited-wildcard', 'inherited-arity',
                      'inherited-raw-nested', 'inherited-hidden', 'inherited-local-shadow',
                      'inherited-static-bare', 'inherited-static-this', 'inherited-static-super'):
            for ambiguous in (False, True):
                self.assertEqual(expected[fields.GRAPH][guard + ':guard:' + str(ambiguous)], [])

    def test_incomplete_inventory_cannot_skip_an_applicable_contract(self):
        inventory = fields.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(fields.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                fields.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            fields.exercise(self.binary, '/private/tmp')

    def test_existing_cases_and_pending_parents_survive_planning(self):
        state = connect(self.directory / 'plan.sqlite')
        self.addCleanup(state.close)
        state.executescript('CREATE TABLE coverage(feature TEXT PRIMARY KEY,status TEXT,reason TEXT);'
                           'CREATE TABLE checks(id TEXT PRIMARY KEY,feature TEXT,subject TEXT);')
        for parent in ('graph', 'explore:semantic-resolution'):
            state.execute('INSERT INTO coverage VALUES (?,\'pending\',\'recorded obligations\')', (parent,))
        state.execute("INSERT INTO checks VALUES ('existing-case','graph','existing criterion')")
        fields.plan_fields(state, self.directory)
        fields.plan_fields(state, self.directory)
        self.assertEqual(state.execute("SELECT id FROM checks WHERE subject='existing criterion'").fetchone()[0], 'existing-case')
        self.assertEqual(state.execute('SELECT count(*) FROM checks').fetchone()[0], 3)
        for parent in ('graph', 'explore:semantic-resolution'):
            row = state.execute('SELECT * FROM coverage WHERE feature=?', (parent,)).fetchone()
            self.assertEqual(row['status'], 'pending')
            self.assertIn('generic arrays', row['reason'])

    def test_audit_requires_each_feature_and_rejects_missing_results(self):
        state = connect(self.directory / 'audit.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        fields.plan_fields(state, self.directory)
        self.assertTrue(fields.FEATURES <= required_features())
        fixture = Fixture(self.directory, self.binary, self.directory / 'unused-index', state, None)
        expected = {feature: {'direct': 'authored-owner'} for feature in fields.FEATURES}
        actual = {feature: {} for feature in fields.FEATURES}
        with patch.object(fields, 'exercise', return_value=(expected, actual)) as exercise:
            for row in state.execute('SELECT * FROM checks'):
                fixture.evaluate(row)
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(dict(state.execute('SELECT verdict,count(*) FROM checks GROUP BY verdict')), {'fail': 2})
        self.assertFalse(fixture.database.exists())


if __name__ == '__main__':
    unittest.main()
