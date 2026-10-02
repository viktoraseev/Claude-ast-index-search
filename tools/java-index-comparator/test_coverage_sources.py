import tempfile
import unittest
from pathlib import Path

from audit import SCHEMA, coverage_sources, plan
from common import connect


class CoverageSourcesTests(unittest.TestCase):
    def test_hybrid_and_internal_checks_are_not_counted_as_pure_mcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = connect(Path(temporary) / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                plan(state, [], '  class  Classes\n  symbol  Symbols\n  file  Files')
                reasons = dict(state.execute('SELECT feature,reason FROM coverage'))
                for feature in ('hierarchy', 'refs', 'usages', 'callers'):
                    self.assertTrue(reasons[feature].startswith('hybrid MCP/JDK'), reasons[feature])
                for feature in ('stats', 'query', 'schema', 'db-path', 'search:references',
                                'search:ranking', 'symbol:options', 'class:options', 'outline:constructors'):
                    self.assertFalse(reasons[feature].startswith('live MCP'), reasons[feature])
                counts = coverage_sources(state)
                implemented = state.execute("SELECT count(*) FROM coverage WHERE status='implemented'").fetchone()[0]
                self.assertEqual(sum(counts.values()), implemented)
                self.assertEqual(counts['hybrid MCP/JDK'], 4)
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
