"""Java outline acceptance executes the CLI; it makes no MCP claim."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import connect
from root_contracts import Runner


FEATURE = 'outline:java-spans'


class OutlineSpanContracts(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='outline-contract-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.state = connect(self.directory / 'checks.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        self.state.execute('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                           ('spans', FEATURE, 'disposable-java-outline-spans'))
        self.state.commit()
        self.oracle = Mock()
        self.oracle.call.side_effect = AssertionError('independent source/CLI, not MCP')
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.fixture = Fixture(self.root, binary, self.directory / 'unused-index',
                               self.state, self.oracle)

    def evaluate(self):
        self.fixture.evaluate(self.state.execute("SELECT * FROM checks WHERE id='spans'").fetchone())
        return self.state.execute("SELECT * FROM checks WHERE id='spans'").fetchone()

    def test_actual_production_spans_and_multiplicity_in_both_formats_and_full_modes(self):
        outcome = self.evaluate()
        diff = json.loads(outcome['diff_json'] or '{}')
        self.assertEqual(outcome['verdict'], 'pass', {
            'verdict': outcome['verdict'], 'error': outcome['error'],
            'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual([p.name for p in self.root.iterdir()], ['Sentinel.java'])
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_incomplete_spans_or_duplicate_loss_cannot_pass(self):
        original = Runner.command
        for defect in ('span', 'duplicate'):
            def corrupted(runner, *arguments, **kwargs):
                code, output = original(runner, *arguments, **kwargs)
                if 'outline' in arguments and 'json' in arguments and 'Outline.java' in arguments:
                    value = json.loads(output)
                    if defect == 'span':
                        value['symbols'][0]['end_line'] = None
                    else:
                        rows = value['symbols']
                        for index, row in enumerate(rows):
                            if row['name'] == 'work':
                                del rows[index]
                                break
                    output = json.dumps(value)
                return code, output
            with self.subTest(defect=defect), patch.object(Runner, 'command', corrupted):
                self.fixture._outline_span_results = None
                self.assertEqual(self.evaluate()['verdict'], 'fail')

    def test_registered_acceptance_and_inventory_never_silently_skip_java(self):
        self.assertIn(FEATURE, required_features())
        plan(self.state, [{'path': 'Sentinel.java'}], '  outline  File symbols',
             root=self.root, java_only=True)
        coverage = self.state.execute('SELECT * FROM coverage WHERE feature=?', (FEATURE,)).fetchone()
        self.assertEqual(coverage['status'], 'implemented')
        self.assertIn('not MCP equivalence', coverage['reason'])
        with patch('outline_contracts.mobile_contracts.inventory', return_value='incomplete'):
            self.assertEqual(self.evaluate()['verdict'], 'error')


if __name__ == '__main__':
    unittest.main()
