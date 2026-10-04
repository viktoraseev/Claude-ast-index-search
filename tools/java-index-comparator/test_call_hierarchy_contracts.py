import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA
import call_hierarchy_contracts as contracts
from common import adapter_digest, connect


class CallHierarchyContractsTests(unittest.TestCase):
    def setUp(self):
        boundary = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        boundary.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='call-hierarchy-', dir=boundary)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.source('Probe.java', 'Probe', 'entry')
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.oracle = Mock()
        self.oracle.call.side_effect = self.response
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'index.sqlite', self.state, self.oracle)
        self.fixture.text_cli('rebuild', '--force')
        self.plan()

    def source(self, path, class_name, caller):
        (self.root / path).write_text(f'package fixture;\nclass {class_name} {{\n'
            f' int leaf() {{ return 1; }}\n int {caller}() {{ return leaf(); }}\n}}\n')

    def plan(self):
        contracts.plan_methods(self.state, self.root,
            [{'path': path.name} for path in sorted(self.root.glob('*.java'))], self.fixture.structure)

    def response(self, tool, request):
        self.assertEqual(tool, 'ide_call_hierarchy')
        self.assertEqual(request['depth'], 1)
        self.assertEqual(request['scope'], 'project_files')
        self.assertEqual(request['direction'], 'callers')
        calls = []
        if request['line'] == 3:
            calls = [{'file': request['file'], 'line': 4, 'children': []}]
        return {'element': {'file': request['file'], 'line': request['line']}, 'calls': calls}

    def evaluate(self):
        check = self.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?',
                                   (contracts.FEATURE, 'leaf')).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_real_cli_behavior_and_request_bound_evidence(self):
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        expected = json.loads(result['expected_json'])
        self.assertEqual(expected['items'], [['Probe.java', 4, 'entry']])
        self.assertTrue(expected['source'].startswith('live MCP'))
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 1)
        self.assertEqual(self.oracle.call.call_count, 1)

    def test_wrong_oracle_edges_cannot_pass_on_json_shape_alone(self):
        self.oracle.call.side_effect = lambda tool, request: {
            'element': {'file': request['file'], 'line': request['line']}, 'calls': []}
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual(json.loads(result['diff_json'])['unexpected'], [['Probe.java', 4, 'entry']])

    def test_same_name_method_declarations_are_unioned(self):
        self.source('Peer.java', 'Peer', 'other')
        self.fixture.text_cli('rebuild', '--force')
        self.plan()
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(json.loads(result['expected_json'])['queries'], 2)
        self.assertEqual(json.loads(result['expected_json'])['items'],
                         [['Peer.java', 4, 'other'], ['Probe.java', 4, 'entry']])

    def test_incomplete_or_wrong_anchor_is_explicitly_unsupported(self):
        for mutation in ('truncated', 'anchor'):
            def response(tool, request):
                value = self.response(tool, request)
                if mutation == 'truncated':
                    value['truncated'] = True
                else:
                    value['element']['line'] = 999
                return value
            self.oracle.call.side_effect = response
            self.assertEqual(self.evaluate()['verdict'], 'unsupported')

    def test_caller_cap_is_not_a_silent_prefix(self):
        with patch.object(contracts, 'MAX_CALLERS', 0):
            self.assertEqual(self.evaluate()['verdict'], 'unsupported')

    def test_planning_resume_preserves_ids_and_completed_evidence(self):
        result = self.evaluate()
        self.plan()
        row = self.state.execute('SELECT * FROM checks WHERE id=?', (result['id'],)).fetchone()
        self.assertEqual(row['verdict'], 'pass')
        self.assertEqual(self.state.execute('SELECT count(*) FROM checks WHERE feature=?',
                                           (contracts.FEATURE,)).fetchone()[0], 2)

    def test_adapter_change_invalidates_evidence(self):
        before = adapter_digest()
        original = Path.read_bytes
        def changed(path):
            value = original(path)
            return value + b'\n# changed contract\n' if path.name == 'call_hierarchy_contracts.py' else value
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)

    def test_implicit_record_accessor_uses_an_exact_usage_anchor(self):
        (self.root / 'Leaf.java').write_text('package fixture;\nrecord Leaf(int leaf) {}\n')
        (self.root / 'Use.java').write_text('package fixture;\nclass Use {\n int read(Leaf value) { return value.leaf(); }\n}\n')
        self.fixture.text_cli('rebuild', '--force')
        self.plan()
        anchor = self.state.execute("SELECT * FROM call_hierarchy_anchors WHERE kind='accessor'").fetchone()
        def response(tool, request):
            if request['file'] == 'Use.java':
                return {'element': {'file': 'Leaf.java', 'line': anchor['line'], 'column': anchor['column']},
                        'calls': [{'file': 'Use.java', 'line': 3, 'children': []}]}
            return self.response(tool, request)
        self.oracle.call.side_effect = response
        result = self.evaluate()
        self.assertEqual(result['verdict'], 'pass', {'error': result['error'], 'diff': result['diff_json']})
        self.assertEqual(json.loads(result['expected_json'])['declarations'], 2)
        self.assertEqual(json.loads(result['expected_json'])['items'],
                         [['Probe.java', 4, 'entry'], ['Use.java', 3, 'read']])

    def test_unresolved_implicit_accessor_cannot_be_fabricated_as_empty(self):
        (self.root / 'Leaf.java').write_text('package fixture;\nrecord Leaf(int leaf) {}\n')
        self.fixture.text_cli('rebuild', '--force')
        self.plan()
        self.assertEqual(self.evaluate()['verdict'], 'unsupported')

    def test_owner_identity_respects_public_overload_union_and_exact_columns(self):
        entries = [{'kind': 'method', 'name': 'entry', 'line': 4, 'column': 7},
                   {'kind': 'method', 'name': 'entry', 'line': 4, 'column': 40}]
        with patch.object(self.fixture, 'structure', return_value=entries):
            self.assertEqual(contracts.owner(self.fixture, {'file': 'Probe.java', 'line': 4}),
                             ('Probe.java', 4, 'entry'))
        entries[1]['name'] = 'other'
        with patch.object(self.fixture, 'structure', return_value=entries):
            with self.assertRaises(contracts.UnsupportedHierarchy):
                contracts.owner(self.fixture, {'file': 'Probe.java', 'line': 4})
            self.assertEqual(contracts.owner(self.fixture, {'file': 'Probe.java', 'line': 4, 'column': 40}),
                             ('Probe.java', 4, 'other'))


if __name__ == '__main__':
    unittest.main()
