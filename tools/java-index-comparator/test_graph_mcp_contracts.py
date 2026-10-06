"""Production CLI tests for MCP-owner-to-graph entity normalization."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, required_features
import call_hierarchy_contracts as callers
import graph_mcp_contracts as graph
from common import adapter_digest, connect
from common import source_snapshot
from replay import replay


class GraphMcpContractsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='graph-mcp-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.source = '''package fixture;
class Probe {
 static int seed() { return seed(); }
 static int caller() { return seed(); }
 int initializer = seed();
 static final int CONSTANT = seed();
 int seed = 1;
}
'''
        (self.root / 'Probe.java').write_text(self.source)
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('graph must reuse completed oracle evidence')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'index.sqlite', self.state, oracle)
        self.fixture.text_cli('rebuild', '--force')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        self.owners = [['Probe.java', 3, 'seed'], ['Probe.java', 4, 'caller'],
                       ['Probe.java', 5, 'initializer'], ['Probe.java', 6, 'CONSTANT']]
        # Authored small oracle test double, not a live IDE-equivalence claim.
        truth = {'source': 'live MCP test double: authored caller-owner response', 'items': self.owners}
        self.state.execute("UPDATE checks SET status='complete',verdict='pass',expected_json=? WHERE feature=? AND subject='seed'",
                           (json.dumps(truth), callers.FEATURE))
        graph.plan(self.state)

    def evaluate(self):
        row = self.state.execute("SELECT * FROM checks WHERE feature=? AND subject='seed'", (graph.FEATURE,)).fetchone()
        self.fixture.evaluate(row)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (row['id'],)).fetchone()

    def test_production_edges_preserve_initializer_owners_and_exclude_internal_subjects(self):
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        truth = json.loads(result['expected_json'])
        self.assertEqual(truth['oracle_owners'], sorted(self.owners))
        self.assertEqual(truth['selected_seeds'], [['Probe.java', 3, 'seed']])
        self.assertEqual(truth['items'], sorted(self.owners[1:]))
        self.assertEqual((self.root / 'Probe.java').read_text(), self.source)
        self.assertEqual(self.fixture.client.call.call_count, 0)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_wrong_oracle_expectation_fails_actual_production_behavior(self):
        truth = {'source': 'live MCP test double', 'items': self.owners[:-1]}
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='seed'",
                           (json.dumps(truth), callers.FEATURE))
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'fail', result['error'])
        self.assertEqual(json.loads(result['diff_json'])['unexpected'], [['Probe.java', 6, 'CONSTANT']])

    def test_incomplete_oracle_and_native_failure_do_not_become_empty_passes(self):
        for status, verdict in [('pending', None), ('complete', 'unsupported'), ('complete', 'error')]:
            with self.subTest(status=status, verdict=verdict):
                self.state.execute("UPDATE checks SET status=?,verdict=? WHERE feature=? AND subject='seed'",
                                   (status, verdict, callers.FEATURE))
                result = self.evaluate()
                self.assertEqual(result['verdict'], 'unsupported')
        self.state.execute("UPDATE checks SET status='complete',verdict='fail' WHERE feature=? AND subject='seed'",
                           (callers.FEATURE,))
        self.assertEqual(self.evaluate()['verdict'], 'pass')

    def test_seed_mismatch_and_capped_page_are_explicitly_unsupported(self):
        original = graph.native_dependents
        for mutation in ('seed', 'cap', 'partial'):
            def native(fixture, check):
                doc = original(fixture, check)
                if mutation == 'seed':
                    doc['matched'][0]['kind'] = 'property'
                elif mutation == 'cap':
                    doc['pagination']['total'] = callers.MAX_CALLERS + 1
                else:
                    doc['pagination']['total'] += 1
                return doc
            with self.subTest(mutation=mutation), patch.object(graph, 'native_dependents', side_effect=native):
                self.assertEqual(self.evaluate()['verdict'], 'unsupported')

    def test_large_native_response_has_an_explicit_bounded_memory_verdict(self):
        with patch.object(graph, 'MAX_OUTPUT_BYTES', 16):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'unsupported')
        self.assertIn('bounded-memory', result['error'])

    def test_same_line_overloads_require_every_selected_declaration(self):
        self.source = self.source.replace(' static int seed() { return seed(); }',
            ' static int seed() { return seed(); } static int seed(int value) { return value; }')
        (self.root / 'Probe.java').write_text(self.source)
        self.fixture.text_cli('rebuild', '--force')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        self.assertEqual(self.state.execute("SELECT count(*) FROM call_hierarchy_anchors WHERE name='seed'").fetchone()[0], 2)
        self.assertEqual(self.evaluate()['verdict'], 'pass')
        original = graph.native_dependents
        def lose_overload(fixture, check):
            doc = original(fixture, check)
            self.assertEqual(len(doc['matched']), 2)
            doc['matched'].pop()
            return doc
        with patch.object(graph, 'native_dependents', side_effect=lose_overload):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'unsupported')
        self.assertIn('seed identities differ', result['error'])
        # Keep the injected owner payload below the budget to isolate the
        # independent declaration-count guard, before any native query.
        truth = {'source': 'live MCP budget test double', 'items': self.owners[:1]}
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='seed'",
                           (json.dumps(truth), callers.FEATURE))
        with patch.object(graph, 'MAX_CALLERS', 1), patch.object(graph, 'native_dependents') as native:
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'unsupported')
        self.assertIn('independent callable seed cap', result['error'])
        native.assert_not_called()

    def test_planning_is_resumable_and_supported_feature_is_required(self):
        before = list(self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id'))
        graph.plan(self.state)
        self.assertEqual([tuple(row) for row in before],
                         [tuple(row) for row in self.state.execute('SELECT id,feature,subject FROM checks ORDER BY id')])
        self.assertIn(graph.FEATURE, required_features())
        row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (graph.FEATURE,)).fetchone()
        self.assertEqual(row['status'], 'implemented')
        self.assertTrue(row['reason'].startswith('live MCP'))

    def test_adapter_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# contract change\n' if path.name == 'graph_mcp_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)

    def test_failed_graph_replays_with_its_oracle_dependency_and_no_extra_checks(self):
        self.state.executemany('INSERT INTO metadata VALUES (?,?)', {
            'project_root': str(self.root), 'snapshot_sha256': source_snapshot(self.root)[0],
            'audit_scope': 'java',
        }.items())
        self.state.execute("UPDATE checks SET status='complete',verdict='fail' WHERE feature=? AND subject='seed'",
                           (graph.FEATURE,))
        self.state.commit()
        result = replay(self.directory / 'evidence.sqlite', self.root, self.fixture.binary,
                        self.directory / 'replays')
        self.assertTrue(result['verified'], result['counts'])
        self.assertEqual(result['counts'], {'pass': 1})
        verification = connect(Path(result['verification']), read_only=True)
        try:
            self.assertEqual(verification.execute('SELECT count(*) FROM checks').fetchone()[0], 1)
            self.assertEqual(verification.execute('SELECT count(*) FROM graph_mcp_sources').fetchone()[0], 1)
        finally:
            verification.close()
        # Resume a new replay from its own dependency archive, not the old source
        # checks table. The original verification remains immutable.
        changed = connect(Path(result['verification']))
        try:
            with changed:
                changed.execute("UPDATE checks SET verdict='fail'")
        finally:
            changed.close()
        repeated = replay(Path(result['verification']), self.root, self.fixture.binary,
                          self.directory / 'repeated')
        self.assertTrue(repeated['verified'], repeated['counts'])
        self.assertEqual(repeated['counts'], {'pass': 1})


if __name__ == '__main__':
    unittest.main()
