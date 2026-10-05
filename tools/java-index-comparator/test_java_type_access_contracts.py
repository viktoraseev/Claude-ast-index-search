"""Type access contracts execute production; synthetic sources are not MCP truth."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, SCHEMA, plan, required_features
from common import ToolError, adapter_digest, connect
import java_type_access_contracts as types


class TypeAccessContractsTests(unittest.TestCase):
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
        with patch.object(types, 'exercise', wraps=types.exercise) as exercise:
            for feature in sorted(types.FEATURES):
                with self.subTest(feature=feature):
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
        for feature in types.FEATURES:
            self.assertIn(feature, required_features())
            row = self.state.execute('SELECT * FROM coverage WHERE feature=?', (feature,)).fetchone()
            self.assertEqual(row['status'], 'implemented')
            self.assertTrue(row['reason'].startswith('independent source/state:'))
            with patch.object(types, 'exercise', return_value=({feature: {'edges': []}},
                                                             {feature: {'edges': ['wrong']}})):
                self.fixture._type_access_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'fail')
            with patch.object(types, 'exercise', side_effect=ToolError('synthetic interrupted fixture')):
                self.fixture._type_access_results = None
                self.assertEqual(self.evaluate(feature)['verdict'], 'error')
        for feature in ('graph', 'call-tree:semantic-resolution', 'explore:semantic-resolution'):
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?',
                                               (feature,)).fetchone()[0], 'pending')
        with self.assertRaisesRegex(ToolError, 'inside repository'):
            types.exercise(self.binary, self.directory.parent.parent.parent)

    def test_positive_fixture_is_valid_java(self):
        paths = []
        for relative, source in types.SOURCES.items():
            # Guard sources intentionally use inaccessible or nonstatic types.
            if relative.startswith(types.NEGATIVE_DIRS):
                continue
            path = self.directory / 'compiler-source' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            paths.append(str(path))
        result = subprocess.run(['javac', '-proc:none', '-d', str(self.directory / 'classes'), *paths],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, 'synthetic positive Java type bindings do not compile')
        for prefix in types.NEGATIVE_DIRS:
            guard_paths = []
            for relative, source in types.SOURCES.items():
                if relative.startswith(prefix):
                    path = self.directory / 'compiler-source' / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source)
                    guard_paths.append(str(path))
            result = subprocess.run(['javac', '-proc:none', '-XDrawDiagnostics', '-d',
                                     str(self.directory / 'classes'), *paths, *guard_paths],
                                    capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0, 'inaccessible/nonstatic Java type guard compiled')
            self.assertIn(b'compiler.err.', result.stderr)

    def test_normalization_keeps_unexpected_types_callables_and_duplicates(self):
        for kind in ('class', 'function'):
            edge = {'other': {'path': 'Probe.java', 'line': 3, 'name': 'wrong', 'kind': kind},
                    'confidence': 'ambiguous'}
            self.assertEqual(types.edges({'items': [edge, edge]}),
                             [(('Probe.java', 3, 'wrong'), 'ambiguous')] * 2)
        for doc in ({}, {'error': 'unbuilt graph', 'items': []}, {'items': [{'other': {}}]}):
            with self.assertRaises(ToolError):
                types.edges(doc)

    def test_pages_allow_only_ties_at_the_documented_path_line_boundary(self):
        first, left, right = ('A.java', 2, 'Before'), ('A.java', 3, 'Open'), ('A.java', 3, 'Deep')
        targets = [first, left, right]
        for tied in (left, right):
            page = types.page_observation(targets, [(first, 'scoped'), (tied, 'scoped')], 2, 3)
            self.assertEqual(page, {'total': 3, 'returned': 2, 'valid': True, 'edges': None})
        for observed in ([(left, 'scoped'), (right, 'scoped')],
                         [(first, 'scoped'), (first, 'scoped')],
                         [(first, 'scoped'), (left, 'ambiguous')],
                         [(first, 'scoped'), (('B.java', 1, 'Wrong'), 'scoped')]):
            self.assertFalse(types.page_observation(targets, observed, 2, 3)['valid'])
        page = types.page_observation(targets, [(first, 'scoped')], 2, 3)
        self.assertEqual(page['returned'], 1)
        self.assertEqual(types.page_observation(targets, [], 0, 3)['returned'], 0)

    def test_contract_changes_invalidate_evidence(self):
        before = adapter_digest()
        read = Path.read_bytes
        def changed(path):
            content = read(path)
            return content + b'\n# type-binding edit\n' if path.name == 'java_type_access_contracts.py' else content
        with patch.object(Path, 'read_bytes', changed):
            self.assertNotEqual(adapter_digest(), before)


if __name__ == '__main__':
    unittest.main()
