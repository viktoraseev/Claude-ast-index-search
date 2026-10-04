"""Completeness guards for the MCP component-reference accessor oracle."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from audit import SCHEMA
import call_hierarchy_contracts as contracts
from common import connect
from oracle_store import Metrics, OracleStore


class AccessorPaginationTests(unittest.TestCase):
    def setUp(self):
        boundary = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        boundary.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='accessor-pagination-', dir=boundary)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = connect(root / 'evidence.sqlite')
        self.addCleanup(state.close)
        state.executescript(SCHEMA)
        with state:
            state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                          ('check', contracts.FEATURE, 'value'))
        self.fixture = SimpleNamespace(root=root, state=state, client=Mock(),
            oracle_store=OracleStore(state, Metrics(state)), structure=Mock())
        self.fixture.structure.return_value = [
            {'kind': 'method', 'name': 'first', 'line': 3, 'end_line': 5},
            {'kind': 'method', 'name': 'second', 'line': 7, 'end_line': 9},
        ]
        self.anchor = {'path': 'Box.java', 'name': 'value', 'line': 1, 'column': 16, 'kind': 'accessor'}
        self.check = {'id': 'check'}

    def first_page(self, usages, **flags):
        return {'resolvedSymbol': {'kind': 'record component', 'name': 'value',
                                  'file': 'Box.java', 'line': 1},
                'usages': usages, 'totalIsExact': True, **flags}

    def usage(self, line, reference_type='METHOD_CALL'):
        return {'file': 'Use.java', 'line': line, 'column': 12, 'type': reference_type}

    def owners(self):
        return contracts.callable_reference_owners(self.fixture, self.check, self.anchor)

    def test_every_page_contributes_and_keeps_exact_requests(self):
        self.fixture.client.call.side_effect = [
            self.first_page([self.usage(4)], hasMore=True, nextCursor='page-two'),
            {'usages': [self.usage(8)], 'hasMore': False, 'totalIsExact': True},
        ]
        self.assertEqual(self.owners(), {('Use.java', 3, 'first'), ('Use.java', 7, 'second')})
        self.assertEqual(self.fixture.client.call.call_args_list[1].args,
            ('ide_find_references', {'project_path': str(self.fixture.root),
                                     'pageSize': 500, 'cursor': 'page-two'}))
        rows = self.fixture.state.execute('SELECT page,tool FROM pages ORDER BY page').fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [(0, 'ide_find_references'), (1, 'ide_find_references')])

    def test_incomplete_terminal_page_never_becomes_empty_truth(self):
        for flags in ({'truncated': True}, {'stale': True}, {'incomplete': True},
                      {'hasMore': True}, {'totalIsExact': False}):
            with self.subTest(flags=flags):
                self.fixture.client.call.side_effect = None
                self.fixture.client.call.return_value = self.first_page([], **flags)
                with self.assertRaises(contracts.UnsupportedHierarchy):
                    self.owners()

    def test_repeated_cursor_is_rejected(self):
        page = self.first_page([], hasMore=True, nextCursor='repeated')
        self.fixture.client.call.return_value = page
        with self.assertRaises(contracts.UnsupportedHierarchy):
            self.owners()
        self.assertEqual(self.fixture.client.call.call_count, 2)

    def test_independent_syntax_distinguishes_calls_from_field_values(self):
        self.fixture.structure.return_value += [
            {'kind': 'usage', 'name': 'value', 'line': 4, 'column': 12, 'usage_kind': 'value'},
            {'kind': 'usage', 'name': 'value', 'line': 8, 'column': 12, 'usage_kind': 'call'},
        ]
        self.fixture.client.call.return_value = self.first_page(
            [self.usage(4, 'REFERENCE'), self.usage(8, 'REFERENCE')])
        self.assertEqual(self.owners(), {('Use.java', 7, 'second')})

    def test_unknown_reference_syntax_stays_explicitly_unsupported(self):
        self.fixture.client.call.return_value = self.first_page([self.usage(4, 'REFERENCE')])
        with self.assertRaises(contracts.UnsupportedHierarchy):
            self.owners()

    def test_component_value_alias_does_not_invent_an_accessor_call(self):
        self.fixture.structure.return_value += [
            {'kind': 'usage', 'name': 'argument', 'line': 4, 'column': 12, 'usage_kind': 'value'},
        ]
        self.fixture.client.call.return_value = self.first_page([self.usage(4, 'REFERENCE')])
        self.assertEqual(self.owners(), set())

    def test_different_callable_name_is_never_silently_ignored(self):
        self.fixture.structure.return_value += [
            {'kind': 'usage', 'name': 'different', 'line': 4, 'column': 12, 'usage_kind': 'call'},
        ]
        self.fixture.client.call.return_value = self.first_page([self.usage(4, 'REFERENCE')])
        with self.assertRaises(contracts.UnsupportedHierarchy):
            self.owners()

    def constructor_reference(self, syntax):
        self.anchor = {'path': 'Item.java', 'name': 'Item', 'line': 2,
                       'column': 2, 'kind': 'constructor'}
        self.fixture.structure.return_value = [
            {'kind': 'property', 'name': 'item', 'line': 4, 'end_line': 6},
            {'kind': 'usage', 'name': 'Item', 'line': 5, 'column': 12, 'usage_kind': syntax},
        ]
        self.fixture.client.call.return_value = {
            'resolvedSymbol': {'kind': 'constructor', 'name': 'Item', 'file': 'Item.java', 'line': 2},
            'usages': [self.usage(5, 'REFERENCE')], 'totalIsExact': True, 'hasMore': False,
        }

    def test_constructor_call_in_field_initializer_keeps_its_source_owner(self):
        self.constructor_reference('constructor_call')
        self.assertEqual(self.owners(), {('Use.java', 4, 'item')})
        self.assertFalse(self.fixture.client.call.call_args.args[1]['includeGenerated'])

    def test_constructor_type_reference_does_not_invent_an_invocation(self):
        self.constructor_reference('value')
        self.assertEqual(self.owners(), set())


if __name__ == '__main__':
    unittest.main()
