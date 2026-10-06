"""Production two-level CLI fixture with authored oracle test doubles."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import Mock, patch
from contextlib import closing

from audit import Fixture, SCHEMA, required_features
from common import ToolError, adapter_digest, connect, source_snapshot
import call_hierarchy_contracts as callers
import call_tree_mcp_contracts as trees
import mobile_contracts
from replay import replay


class CallTreeMcpTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='call-tree-mcp-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.source = '''class Probe {
 static void seed() {}
 static void branch() { seed(); }
 static void other() { seed(); }
 static void top() { branch(); other(); }
 static void recursive() { recursive(); }
 static void empty() {}
}
'''
        (self.root / 'Probe.java').write_text(self.source)
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        oracle = Mock()
        oracle.call.side_effect = AssertionError('depth2 must reuse completed MCP truth')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'index.sqlite', self.state, oracle)
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        truth = {'seed': [[3, 'branch'], [4, 'other']], 'branch': [[5, 'top']],
                 'other': [[5, 'top']], 'top': [], 'recursive': [[6, 'recursive']], 'empty': []}
        for name, owners in truth.items():
            doc = {'source': 'live MCP test double; authored owners, not live IDE equivalence',
                   'items': [['Probe.java', line, owner] for line, owner in owners]}
            self.state.execute("UPDATE checks SET status='complete',verdict='pass',expected_json=? WHERE feature=? AND subject=?",
                               (json.dumps(doc), callers.FEATURE, name))
        trees.plan(self.state)

    def evaluate(self, subject='seed'):
        check = self.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?',
                                   (trees.FEATURE, subject)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_actual_two_level_tree_preserves_repeated_owners_empty_and_root_recursion(self):
        for name in ('seed', 'empty', 'recursive'):
            result = self.evaluate(name)
            self.assertEqual(result['verdict'], 'pass', result['error'])
        seed = json.loads(self.evaluate()['expected_json'])
        self.assertEqual(seed['items'].count([2, 'Probe.java', 5, 'top', 'shown']), 2)
        recursive = json.loads(self.evaluate('recursive')['expected_json'])
        self.assertEqual(recursive['items'], [[1, 'Probe.java', 6, 'recursive', 'expanded_above']])
        self.assertEqual((self.root / 'Probe.java').read_text(), self.source)
        self.assertEqual(self.fixture.client.call.call_count, 0)

    def test_missing_repeated_native_owner_is_a_failure_not_set_equality(self):
        original = trees.native_tree
        def lost(fixture, check):
            doc = original(fixture, check)
            self.assertEqual(len(doc['items']), 4)
            doc['items'].pop()
            doc['count'] -= 1
            return doc
        with patch.object(trees, 'native_tree', side_effect=lost):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual(len(json.loads(result['diff_json'])['missing']), 1)

    def test_mixed_language_scope_removes_foreign_branches_without_losing_java_repeats(self):
        original = trees.native_tree
        def mixed(fixture, check):
            doc = original(fixture, check)
            doc['items'][:0] = [
                {'depth': 1, 'path': 'Foreign.java.txt', 'line': 1, 'name': 'foreign', 'status': 'shown'},
                {'depth': 2, 'path': 'Probe.java', 'line': 1, 'name': 'foreignDescendant', 'status': 'shown'}]
            doc['count'] += 2
            return doc
        with patch.object(trees, 'native_tree', side_effect=mixed):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(len(json.loads(result['actual_json'])['items']), 4)

    def test_second_level_recursion_is_bound_to_the_actual_owner_identity(self):
        # Recursion is independent of this node's parent branch.
        (self.root / 'Probe.java').write_text(self.source.replace('branch() { seed(); }',
                                                                'branch() { seed(); branch(); }'))
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='branch'",
            (json.dumps({'source': 'live MCP test double',
                         'items': [['Probe.java', 3, 'branch'], ['Probe.java', 5, 'top']]}), callers.FEATURE))
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertIn([2, 'Probe.java', 3, 'branch', 'recursive'], json.loads(result['expected_json'])['items'])

    def test_missing_or_ambiguous_child_truth_remains_unsupported(self):
        # A missing scope must remain explicit rather than empty truth.
        self.state.execute("UPDATE checks SET status='pending' WHERE feature=? AND subject='branch'", (callers.FEATURE,))
        self.assertEqual(self.evaluate()['verdict'], 'unsupported')
        self.state.execute("UPDATE checks SET status='complete' WHERE feature=? AND subject='branch'", (callers.FEATURE,))
        self.state.execute("INSERT INTO call_hierarchy_anchors VALUES ('branch','Other.java',1,1,'method')")
        with patch.object(trees, 'native_tree', side_effect=AssertionError('ambiguous oracle must not reach native')):
            self.assertEqual(self.evaluate()['verdict'], 'unsupported')

    def test_colliding_child_names_use_position_bound_oracle_not_name_union(self):
        (self.root / 'Peer.java').write_text('''class Peer {
 static void branch() {}
 static void unrelated() { branch(); }
}
''')
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        callers.plan_methods(self.state, self.root,
                            [{'path': 'Probe.java'}, {'path': 'Peer.java'}], self.fixture.structure)
        def response(tool, request):
            self.assertEqual((request['file'], request['line']), ('Probe.java', 3))
            self.assertIsInstance(request['column'], int)
            if tool == 'ide_find_references':
                return {'resolvedSymbol': {'kind': 'method', 'name': 'branch',
                        'file': 'Probe.java', 'line': 3},
                        'usages': [{'file': 'Probe.java', 'line': 5, 'type': 'METHOD_CALL'}],
                        'totalIsExact': True, 'hasMore': False}
            self.assertEqual(tool, 'ide_call_hierarchy')
            return {'element': {'file': request['file'], 'line': request['line'],
                                'column': request['column']},
                    'calls': [{'file': 'Probe.java', 'line': 5, 'children': []},
                              {'file': 'Peer.java', 'line': 3, 'children': []}]}
        self.fixture.client.call.side_effect = response
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        expected = json.loads(result['expected_json'])['items']
        self.assertEqual(expected.count([2, 'Probe.java', 5, 'top', 'shown']), 2)
        self.assertEqual(self.fixture.client.call.call_count, 2)
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages WHERE check_id=?',
                                           (result['id'],)).fetchone()[0], 2)
        self.state.execute("UPDATE checks SET verdict='fail' WHERE id=?", (result['id'],))
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
            [('project_root', str(self.root)), ('snapshot_sha256', source_snapshot(self.root)[0]),
             ('audit_scope', 'java')])
        self.state.commit()
        summary = replay(self.directory / 'evidence.sqlite', self.root, self.binary,
                         self.directory / 'position-replay', 1)
        self.assertEqual(summary['counts'], {'pass': 1})
        with closing(connect(Path(summary['verification']), read_only=True)) as replayed:
            self.assertEqual(replayed.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 2)

    def test_field_initializer_does_not_borrow_same_named_method_truth(self):
        (self.root / 'Probe.java').write_text(self.source.replace(' static void empty() {}',
            ' static int field = init();\n static int init() { seed(); return 1; }'))
        (self.root / 'Peer.java').write_text('''class Peer {
 static void field() {}
 static void unrelated() { field(); }
}
''')
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        callers.plan_methods(self.state, self.root,
                            [{'path': 'Probe.java'}, {'path': 'Peer.java'}], self.fixture.structure)
        trees.plan(self.state)
        self.state.execute("UPDATE checks SET status='complete',verdict='pass',expected_json=? WHERE feature=? AND subject='init'",
            (json.dumps({'source': 'live MCP test double', 'items': [['Probe.java', 7, 'field']]}), callers.FEATURE))
        result = self.evaluate('init')
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(json.loads(result['expected_json'])['items'], [[1, 'Probe.java', 7, 'field', 'shown']])
        self.assertEqual(self.fixture.client.call.call_count, 0)

    def test_noncallable_initializer_owners_remain_visible_without_expansion(self):
        source = '''class Probe {
 static int leaf() { return 1; }
 static int field = leaf();
 static final int CONSTANT = leaf();
 static int reader() { return field; }
 static int constantReader() { return CONSTANT; }
}
class Peer {
 static void field() {}
 static void wrong() { field(); }
}
'''
        (self.root / 'Probe.java').write_text(source)
        compiled = self.directory / 'javac'
        compiled.mkdir()
        result = subprocess.run(['javac', '-d', str(compiled), str(self.root / 'Probe.java')],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'synthetic initializer fixture must compile')
        for built in (False, True):
            self.fixture.cli('rebuild', '--force', '--max-files', '0')
            if built:
                self.fixture.cli('graph', 'build')
            for format in ('json', 'text'):
                args = ('call-tree', 'leaf', '--depth', '3', '--limit', '100', '--in-file', '.java')
                if format == 'json':
                    doc = self.fixture.cli(*args)
                    self.assertEqual(doc['items'], [
                        {'depth': 1, 'name': 'field', 'path': 'Probe.java', 'line': 3, 'status': 'shown'},
                        {'depth': 1, 'name': 'CONSTANT', 'path': 'Probe.java', 'line': 4, 'status': 'shown'}])
                else:
                    text = self.fixture.text_cli(*args)
                    self.assertIn('← field (Probe.java:3)', text)
                    self.assertIn('← CONSTANT (Probe.java:4)', text)
                    self.assertNotIn('reader', text)
                    self.assertNotIn('constantReader', text)
                    self.assertNotIn('wrong', text)

    def test_same_line_callable_and_field_owner_collision_is_not_a_fake_pass(self):
        (self.root / 'Probe.java').write_text('''class Probe {
 static int leaf() { return 1; }
 static int field = leaf(); static int field() { return 2; }
}
''')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        trees.plan(self.state)
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='leaf'",
            (json.dumps({'source': 'live MCP test double', 'items': [['Probe.java', 3, 'field']]}), callers.FEATURE))
        self.assertEqual(self.evaluate('leaf')['verdict'], 'unsupported')
        self.assertEqual(self.fixture.client.call.call_count, 0)

    def test_full_inventory_and_java_planning_cannot_turn_applicable_case_inapplicable(self):
        (self.root / 'Foreign.txt').write_text('inventory sentinel\n')
        mobile_contracts.inventory(self.state, self.root)
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        trees.plan(self.state)
        self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                           (trees.FEATURE,)).fetchone()[0], 'implemented')
        self.assertGreater(self.state.execute('SELECT count(*) FROM checks WHERE feature=?',
                                             (trees.FEATURE,)).fetchone()[0], 0)
        with patch.object(trees, 'native_tree', side_effect=ToolError('interrupted applicable fixture')):
            self.assertEqual(self.evaluate()['verdict'], 'error')

    def test_incomplete_native_page_budget_and_wrong_oracle_never_pass(self):
        original = trees.native_tree
        def partial(fixture, check):
            doc = original(fixture, check)
            doc['count'] += 1
            return doc
        with patch.object(trees, 'native_tree', side_effect=partial):
            self.assertEqual(self.evaluate()['verdict'], 'unsupported')
        with patch.object(trees, 'MAX_BYTES', 16):
            self.assertEqual(self.evaluate()['verdict'], 'unsupported')
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='branch'",
            (json.dumps({'source': 'live MCP test double', 'items': []}), callers.FEATURE))
        self.assertEqual(self.evaluate()['verdict'], 'fail')

    def test_planning_fingerprint_and_dependency_copy_are_idempotent(self):
        count = self.state.execute('SELECT count(*) FROM checks WHERE feature=?', (trees.FEATURE,)).fetchone()[0]
        trees.plan(self.state)
        self.assertEqual(self.state.execute('SELECT count(*) FROM checks WHERE feature=?', (trees.FEATURE,)).fetchone()[0], count)
        self.assertIn(trees.FEATURE, required_features())
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            return read(path) + (b'\n# edit\n' if path.name == 'call_tree_mcp_contracts.py' else b'')
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)
        copy = connect(self.directory / 'copy.sqlite')
        self.addCleanup(copy.close)
        copy.executescript(SCHEMA)
        trees.copy_dependencies(self.state, copy, 'seed')
        trees.copy_dependencies(self.state, copy, 'seed')
        self.assertEqual([row[0] for row in copy.execute('SELECT subject FROM graph_mcp_sources ORDER BY subject')],
                         ['branch', 'other', 'seed'])
        self.assertEqual(copy.execute('SELECT count(*) FROM checks').fetchone()[0], 0)
        second = connect(self.directory / 'second.sqlite')
        self.addCleanup(second.close)
        second.executescript(SCHEMA)
        trees.copy_dependencies(copy, second, 'seed')
        self.assertEqual(second.execute('SELECT count(*) FROM graph_mcp_sources').fetchone()[0], 3)

    def test_swapped_grandchildren_cannot_move_to_another_parent_branch(self):
        (self.root / 'Probe.java').write_text(self.source.replace(
            'top() { branch(); other(); }',
            'top() { branch(); }\n static void peer() { other(); }'))
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        callers.plan_methods(self.state, self.root, [{'path': 'Probe.java'}], self.fixture.structure)
        self.state.execute("UPDATE checks SET expected_json=? WHERE feature=? AND subject='other'",
            (json.dumps({'source': 'live MCP test double',
                         'items': [['Probe.java', 6, 'peer']]}), callers.FEATURE))
        self.assertEqual(self.evaluate()['verdict'], 'pass')
        original = trees.native_tree
        def swap(fixture, check):
            doc = original(fixture, check)
            children = [index for index, row in enumerate(doc['items']) if row['depth'] == 2]
            self.assertEqual(len(children), 2)
            first, second = children
            doc['items'][first], doc['items'][second] = doc['items'][second], doc['items'][first]
            return doc
        with patch.object(trees, 'native_tree', side_effect=swap):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'fail')
        diff = json.loads(result['diff_json'])
        self.assertEqual(len(diff['missing']), 2)
        self.assertEqual(len(diff['unexpected']), 2)

    def test_orphan_second_level_row_is_malformed_not_filtered_away(self):
        original = trees.native_tree
        def orphan(fixture, check):
            doc = original(fixture, check)
            self.assertEqual(doc['items'][0]['depth'], 1)
            self.assertEqual(doc['items'][1]['depth'], 2)
            doc['items'].pop(0)
            doc['count'] -= 1
            return doc
        with patch.object(trees, 'native_tree', side_effect=orphan):
            result = self.evaluate()
        self.assertEqual(result['verdict'], 'error')
        self.assertIn('no first-level owner', result['error'])

    def test_actual_replay_keeps_child_truth_without_extra_checks_or_oracle_queries(self):
        self.evaluate()
        self.state.execute("UPDATE checks SET verdict='fail' WHERE feature=? AND subject='seed'", (trees.FEATURE,))
        snapshot, _ = source_snapshot(self.root)
        self.state.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                              [('project_root', str(self.root)), ('snapshot_sha256', snapshot), ('audit_scope', 'java')])
        self.state.commit()
        destination = self.directory / 'replay'
        summary = replay(self.directory / 'evidence.sqlite', self.root, self.binary, destination, 1)
        self.assertEqual(summary['counts'], {'pass': 1})
        with closing(connect(Path(summary['verification']))) as result:
            self.assertEqual(result.execute('SELECT count(*) FROM checks').fetchone()[0], 1)
            self.assertEqual(result.execute('SELECT count(*) FROM graph_mcp_sources').fetchone()[0], 3)
            result.execute("UPDATE checks SET verdict='fail'")
            result.commit()
        # Force a second actual evaluation while retaining all oracle truth.
        again = replay(Path(summary['verification']), self.root, self.binary, self.directory / 'again', 1)
        self.assertEqual(again['counts'], {'pass': 1})


if __name__ == '__main__':
    unittest.main()
