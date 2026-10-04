"""Synthetic source/MCP adapter cases exercise the current production CLI."""
import json
import unittest

import test_call_hierarchy_contracts as base_contracts
import call_hierarchy_contracts as contracts


class JavaCallerOwnerTests(unittest.TestCase):
    setUp = base_contracts.CallHierarchyContractsTests.setUp
    source = base_contracts.CallHierarchyContractsTests.source
    plan = base_contracts.CallHierarchyContractsTests.plan
    response = base_contracts.CallHierarchyContractsTests.response

    def check_name(self, name):
        check = self.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?',
                                   (contracts.FEATURE, name)).fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_ordinary_method_field_initializers_and_recursion_supplement_hierarchy(self):
        (self.root / 'Probe.java').write_text('class Probe {\n'
            ' static int leaf(int depth) { return depth == 0 ? 1 : leaf(depth - 1); }\n'
            ' int value = leaf(0);\n'
            ' java.util.function.IntUnaryOperator callback = Probe::leaf;\n}\n')
        self.fixture.text_cli('rebuild', '--force')
        self.plan()
        entries = self.fixture.structure('Probe.java')
        usages = [entry for entry in entries if entry.get('usage_kind') in {'call', 'method_reference'}]
        def response(tool, request):
            if tool == 'ide_call_hierarchy':
                return {'element': {'file': 'Probe.java', 'line': 2}, 'calls': []}
            self.assertEqual(tool, 'ide_find_references')
            return {'resolvedSymbol': {'kind': 'method', 'name': 'leaf', 'file': 'Probe.java', 'line': 2},
                    'usages': [{'file': 'Probe.java', 'line': entry['line'], 'column': entry['column'],
                                'type': 'REFERENCE'} for entry in usages],
                    'totalIsExact': True, 'hasMore': False}
        self.oracle.call.side_effect = response
        result = self.check_name('leaf')
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(json.loads(result['expected_json'])['items'],
                         [['Probe.java', 2, 'leaf'], ['Probe.java', 3, 'value'], ['Probe.java', 4, 'callback']])
        self.assertIn('supplements hierarchy', json.loads(result['expected_json'])['source'])
        # Successful reference normalization must still detect native omissions.
        from unittest.mock import patch
        with patch.object(contracts, 'native_callers', return_value=set()):
            self.assertEqual(self.check_name('leaf')['verdict'], 'fail')

    def test_enum_constants_are_constructor_calls_with_constant_owners(self):
        (self.root / 'Probe.java').write_text('enum Probe {\n'
            ' FIRST("one"),\n SECOND("two");\n Probe(String value) {}\n}\n')
        self.fixture.text_cli('rebuild', '--force')
        self.plan()
        entries = self.fixture.structure('Probe.java')
        usages = [entry for entry in entries if entry.get('usage_kind') == 'constructor_call']
        self.assertEqual([(entry['name'], entry['line']) for entry in usages], [('Probe', 2), ('Probe', 3)])
        self.assertTrue(all(entry.get('implicit') is True for entry in usages))
        def response(tool, request):
            if tool == 'ide_call_hierarchy':
                return {'element': {'file': 'Probe.java', 'line': 4}, 'calls': []}
            return {'resolvedSymbol': {'kind': 'constructor', 'name': 'Probe', 'file': 'Probe.java', 'line': 4},
                    'usages': [{'file': 'Probe.java', 'line': entry['line'], 'column': entry['column'],
                                'type': 'REFERENCE'} for entry in usages],
                    'totalIsExact': True, 'hasMore': False}
        self.oracle.call.side_effect = response
        result = self.check_name('Probe')
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(json.loads(result['expected_json'])['items'],
                         [['Probe.java', 2, 'FIRST'], ['Probe.java', 3, 'SECOND']])


if __name__ == '__main__':
    unittest.main()
