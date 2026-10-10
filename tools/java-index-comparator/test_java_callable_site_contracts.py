"""Compiled source contracts for call trees with colliding callable coordinates."""
import unittest
import json
from unittest.mock import patch

import test_call_tree_mcp_contracts as tree_tests
import java_callable_site_contracts as contracts
from common import ToolError
from audit import plan


class CallableSiteTests(unittest.TestCase):
    def setUp(self):
        tree_tests.CallTreeMcpTests.setUp(self)

    def test_production_callable_sites_preserve_each_overload_branch(self):
        contracts.plan_sites(self.state, self.root)
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        self.fixture.evaluate(check)
        result = self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()
        self.assertEqual(result['verdict'], 'pass', result['error'])
        self.assertEqual(len(json.loads(result['expected_json'])['samples']), 50)
        self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
        self.assertEqual(self.fixture.client.call.call_count, 0)

    def test_applicable_java_cannot_be_silently_skipped(self):
        inventory = contracts.mobile_contracts.inventory
        def incomplete(state, root):
            inventory(state, root)
            state.execute("DELETE FROM file_inventory WHERE extension='.xml'")
        with patch.object(contracts.mobile_contracts, 'inventory', side_effect=incomplete):
            with self.assertRaisesRegex(ToolError, 'inventory incomplete'):
                contracts.exercise(self.binary, self.directory)
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            contracts.exercise(self.binary, '/private/tmp')

    def test_same_line_branches_and_empty_page_metadata_are_required(self):
        contracts.plan_sites(self.state, self.root)
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
        expected, actual = contracts.exercise(self.binary, self.directory)
        # Native omissions and changed metadata are failures, not normalized passes.
        for key in ('forward:alphaSeed:3:100', 'reverse:betaSeed:0:0'):
            changed = json.loads(json.dumps(actual))
            changed[key]['count'] += 1
            with patch.object(contracts, 'exercise', return_value=(expected, changed)):
                self.fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?',
                                               (check['id'],)).fetchone()[0], 'fail')

    def test_pending_semantic_parent_has_executed_acceptance_mechanism(self):
        plan(self.state, [{'path': 'Probe.java'}], '', root=self.root, java_only=True)
        self.assertIsNotNone(self.state.execute(
            "SELECT id FROM checks WHERE feature='call-tree:semantic-resolution'").fetchone(),
            'pending Java parent must have an executable acceptance check')


if __name__ == '__main__':
    unittest.main()
