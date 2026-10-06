"""Receiver contracts execute production; synthetic sources are not MCP truth."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_receiver_contracts as receivers
from root_contracts import Runner


class ReceiverContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        (self.root / 'Foreign.kt').write_text('// inventory only\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('source contracts must not call MCP')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self, feature):
        check = self.state.execute('SELECT * FROM checks WHERE feature=?', (feature,)).fetchone()
        self.assertIsNotNone(check)
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_shared_fixture_checks_binding_guards_and_exploration(self):
        with patch.object(receivers, 'exercise', wraps=receivers.exercise) as exercise:
            for feature in sorted(receivers.FEATURES):
                result = self.evaluate(feature)
                diff = json.loads(result['diff_json'] or '{}')
                self.assertEqual(result['verdict'], 'pass',
                                 {'feature': feature, 'error': result['error'],
                                  'missing': len(diff.get('missing', [])),
                                  'unexpected': len(diff.get('unexpected', []))})
                self.assertIn('not MCP equivalence', json.loads(result['expected_json'])['source'])
            self.assertEqual(exercise.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Foreign.kt', 'Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_applicable_contracts_cannot_be_silently_skipped(self):
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
        for feature in receivers.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            with patch.object(receivers, 'exercise', return_value=({feature: {'edges': []}},
                                                                  {feature: {'edges': ['wrong']}})):
                self.fixture._receiver_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            with patch.object(receivers, 'exercise', side_effect=ToolError('synthetic interrupted fixture')):
                self.fixture._receiver_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('graph', 'call-tree:semantic-resolution', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            receivers.exercise(self.binary, self.directory.parent.parent.parent)

    def test_normalization_keeps_unexpected_callable_names_and_duplicates(self):
        edge = {'other': {'path': 'Probe.java', 'line': 3, 'name': 'wrong', 'kind': 'function'},
                'confidence': 'ambiguous'}
        self.assertEqual(receivers.call_edges({'items': [edge, edge]}),
                         [(('Probe.java', 3, 'wrong'), 'ambiguous')] * 2)
        for doc in ({}, {'error': 'unbuilt graph', 'items': []}, {'items': [{'other': {}}]}):
            with self.assertRaises(ToolError):
                receivers.call_edges(doc)

    def test_same_line_zero_argument_calls_have_two_compiler_valid_targets(self):
        runner = Runner(self.binary, self.directory / 'distinct-sites')
        runner.root.mkdir(parents=True)
        sources = []
        for relative in ('a/Leaf.java', 'b/Leaf.java', 'local/Probe.java'):
            source = runner.root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            content = receivers.SOURCES[relative]
            if relative == 'local/Probe.java':
                line = receivers.SOURCE_IDS['fixture.local.Probe.collision'][1]
                content = ('package fixture.local;\nimport fixture.a.Leaf;\nclass Probe {\n'
                           + content.splitlines()[line - 1] + '\n}\n')
            source.write_text(content)
            sources.append(str(source))
        with (runner.directory / 'javac.log').open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                     *sources], stdout=log, stderr=log, timeout=30)
        self.assertEqual(result.returncode, 0)
        runner.command('rebuild', '--force')
        runner.command('graph', 'build')
        want = [(('a/Leaf.java', 3, 'ping'), 'scoped'), (('b/Leaf.java', 3, 'ping'), 'scoped')]
        for flags in ((), ('--include-ambiguous',)):
            self.assertEqual(receivers.call_edges(runner.json(
                'graph', 'dependencies', 'fixture.local.Probe.collision', *flags)), want)

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# receiver edit\n' if path.name == 'java_receiver_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
