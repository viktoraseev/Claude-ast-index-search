"""Java resource scope runs production and cannot be silently inapplicable."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import java_resource_scope_contracts as contracts
from audit import Fixture, SCHEMA, plan
from common import ToolError, connect


class JavaResourceScopeTests(unittest.TestCase):
    def test_production_resource_and_java_class_ownership_selectors(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests/java-resource-scope'
        base.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        expected, actual = contracts.exercise(binary, base)
        missing = sum(expected[contracts.FEATURE].get(k) != actual[contracts.FEATURE].get(k)
                      for k in expected[contracts.FEATURE])
        self.assertEqual(missing, 0, {'failed_assertions': missing})
        self.assertEqual(set(expected[contracts.FEATURE]), set(actual[contracts.FEATURE]))

    def test_applicable_java_resource_contract_cannot_be_silently_excluded(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests/java-resource-scope'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            directory = Path(temporary)
            root = directory / 'read-only-target'
            root.mkdir()
            (root / 'Sentinel.java').write_text('class Sentinel {}\n')
            (root / 'Inventory.kt').write_text('// inventory only\n')
            state = connect(directory / 'evidence.sqlite')
            self.addCleanup(state.close)
            state.executescript(SCHEMA)
            oracle = Mock()
            oracle.call.side_effect = AssertionError('source proof has no MCP equivalent')
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            fixture = Fixture(root, binary, directory / 'unused-index', state, oracle)
            plan(state, [{'path': 'Sentinel.java'}], '', root=root, java_only=True)
            check = state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
            self.assertIsNotNone(check)
            self.assertEqual(state.execute('SELECT count(distinct extension) FROM file_inventory').fetchone()[0], 2)
            expected = {contracts.FEATURE: {'applicable-inventory': {'.java': 1}, 'root-selection': 'correct'}}
            for observed in ({}, {'applicable-inventory': 'inapplicable'}, {'root-selection': 'wrong'}):
                fixture._java_resource_scope_results = None
                with patch.object(contracts, 'exercise', return_value=(expected, {contracts.FEATURE: observed})):
                    fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
            fixture._java_resource_scope_results = None
            with patch.object(contracts, 'exercise', side_effect=ToolError('incomplete fixture inventory')):
                fixture.evaluate(check)
            self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'error')
            self.assertEqual(state.execute('SELECT status FROM coverage WHERE feature=?',
                                          ('global:scope-command-matrix',)).fetchone()[0], 'pending')
            self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
            self.assertFalse(fixture.database.exists())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['Inventory.kt', 'Sentinel.java'])
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(binary, Path('/private/tmp'))


if __name__ == '__main__':
    unittest.main()
