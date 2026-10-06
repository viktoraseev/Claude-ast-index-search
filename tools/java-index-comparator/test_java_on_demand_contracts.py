"""Execute source expectations and reject silent Java applicability skips."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, connect, source_snapshot
from replay import replay
import java_on_demand_contracts as contracts


class OnDemandContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()

    def test_production_import_family_matches_javac_validated_identities(self):
        expected, actual = contracts.exercise(self.binary, self.directory)
        differences = {feature: [key for key in expected[feature]
                                 if expected[feature][key] != actual[feature].get(key)]
                       for feature in sorted(contracts.FEATURES)}
        self.assertEqual(differences, {feature: [] for feature in sorted(contracts.FEATURES)})

    def test_applicable_contract_cannot_skip_or_claim_mcp_equivalence(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (root / 'marker.xml').write_text('<marker/>\n')
        state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('on-demand contract has no MCP oracle')
        fixture = Fixture(root, self.binary, self.directory / 'unused-index', state, oracle)
        plan(state, [{'path': 'Sentinel.java'}], '  class  Classes\n  file  Files', root=root, java_only=True)
        self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in sorted(contracts.FEATURES):
            self.assertIn(feature, required_features())
            row = state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertIn('not MCP equivalence', row['reason'])
            check = state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
            for observed in ({}, {'usage': 'inapplicable'}, {'usage': ['wrong']}, {'usage': ['alpha']}):
                fixture._java_on_demand_results = None
                with patch.object(contracts, 'exercise', return_value=(
                        {feature: {'usage': ['alpha']}}, {feature: observed})):
                    fixture.evaluate(check)
                verdict = state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0]
                self.assertEqual(verdict, 'pass' if observed == {'usage': ['alpha']} else 'fail')
        self.assertEqual(state.execute("SELECT status FROM coverage WHERE feature='unused-deps:semantic-resolution'").fetchone()[0], 'pending')
        self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        self.assertFalse(fixture.database.exists())
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['Sentinel.java', 'marker.xml'])

    def test_incomplete_inventory_and_external_mutation_are_rejected(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.txt'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, Path('/private/tmp'))

    def test_failed_family_replays_production_without_mcp(self):
        root = self.directory / 'read-only-target'
        root.mkdir()
        (root / 'Sentinel.java').write_text('class Sentinel {}\n')
        state = connect(self.directory / 'evidence.sqlite')
        state.executescript(SCHEMA)
        contracts.plan_imports(state, root)
        state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', {
            'project_root': str(root), 'snapshot_sha256': source_snapshot(root)[0], 'audit_scope': 'java',
        }.items())
        state.execute("UPDATE checks SET status='complete',verdict='fail'")
        state.commit()
        state.close()
        result = replay(self.directory / 'evidence.sqlite', root, self.binary, self.directory / 'replays')
        self.assertTrue(result['verified'], result['counts'])
        self.assertEqual(result['counts'], {'pass': len(contracts.FEATURES)})
        verification = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(verification.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
            for row in verification.execute('SELECT expected_json FROM checks'):
                self.assertIn('not MCP equivalence', json.loads(row[0])['source'])
        finally:
            verification.close()
