"""Preset support must exercise production, never manufacture MCP coverage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import rank_contracts


class RankContractsTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'read-only-target'
        self.root.mkdir()
        (self.root / 'Sentinel.java').write_text('class Sentinel {}\n')
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        oracle = Mock()
        oracle.call.side_effect = AssertionError('ranking is not an MCP oracle')
        self.fixture = Fixture(self.root, self.binary, self.directory / 'unused-target-index', self.state, oracle)
        plan(self.state, [{'path': 'Sentinel.java'}], '  class  Classes\n  symbol  Symbols\n  file  Files',
             root=self.root, java_only=True)

    def evaluate(self):
        check = self.state.execute("SELECT * FROM checks WHERE feature='search:rank-presets'").fetchone()
        self.fixture.evaluate(check)
        return self.state.execute('SELECT * FROM checks WHERE id=?', (check['id'],)).fetchone()

    def test_formulas_substance_lineage_and_bounded_pools_execute_production(self):
        result = self.evaluate()
        diff = json.loads(result['diff_json'] or '{}')
        self.assertEqual(result['verdict'], 'pass', {'verdict': result['verdict'], 'error': result['error'],
                         'missing': len(diff.get('missing', [])), 'unexpected': len(diff.get('unexpected', []))})
        samples = json.loads(result['expected_json'])['samples']
        self.assertEqual(sum(k.startswith('substance:') for k in samples), 10)
        self.assertTrue(any(k.startswith('budget:') for k in samples))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['Sentinel.java'])
        self.assertFalse(self.fixture.database.exists())
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)

    def test_java_contract_cannot_be_classified_absent_or_fake_pass(self):
        self.assertIn('search:rank-presets', required_features())
        row = self.state.execute("SELECT * FROM coverage WHERE feature='search:rank-presets'").fetchone()
        self.assertEqual(row['status'], 'implemented')
        self.assertTrue(row['reason'].startswith('independent source/state:'))
        self.assertIn('not MCP equivalence', row['reason'])
        self.assertEqual(self.state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 1)
        # No target history or graph is not proof that the Java CLI contract is
        # inapplicable: the disposable state exercises both features.
        with patch('rank_contracts.exercise', return_value=({'value': 1}, {'value': 2})), \
                patch('rank_contracts.budget', return_value=({}, {})):
            self.assertEqual(self.evaluate()['verdict'], 'fail')
        self.fixture._rank_results = None
        with patch('rank_contracts.exercise', side_effect=ToolError('synthetic interrupted fixture')):
            self.assertEqual(self.evaluate()['verdict'], 'error')
        for exercise in (rank_contracts.exercise, rank_contracts.budget):
            with self.assertRaisesRegex(ToolError, 'inside repository'):
                exercise(self.binary, self.root.parent.parent.parent.parent.parent)

    def test_rank_contract_changes_invalidate_evidence(self):
        previous = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# changed contract\n' if path.name == 'rank_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), previous)


if __name__ == '__main__':
    unittest.main()
