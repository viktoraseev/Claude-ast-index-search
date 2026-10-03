import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from audit import Fixture, InvocationOracle, SCHEMA, next_check
from common import connect


class CheckScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = connect(self.root / 'evidence.sqlite')
        self.addCleanup(self.state.close)
        self.state.executescript(SCHEMA)
        with self.state:
            self.state.executemany('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)', [
                ('outline', 'outline', 'A.java'), ('constructors', 'outline:constructors', 'A.java'),
                ('search', 'search', 'Alpha'), ('symbol', 'symbol', 'Alpha')])

    def passed(self, identity):
        with self.state:
            self.state.execute("UPDATE checks SET status='complete',verdict='pass' WHERE id=?", (identity,))

    def test_all_other_checks_run_before_both_outline_variants_without_losing_ids(self):
        seen = []
        while check := next_check(self.state):
            seen.append(check['id'])
            self.passed(check['id'])
        self.assertEqual(seen, ['search', 'symbol', 'outline', 'constructors'])
        self.assertEqual(self.state.execute('SELECT count(*) FROM checks').fetchone()[0], 4)

    def test_newly_discovered_followups_still_precede_outline(self):
        self.passed('search')
        self.passed('symbol')
        self.assertEqual(next_check(self.state)['id'], 'outline')
        with self.state:
            self.state.execute("INSERT INTO checks(id,feature,subject) VALUES ('discovered','refs','Alpha')")
        self.assertEqual(next_check(self.state)['id'], 'discovered')
        self.passed('discovered')
        self.assertEqual(next_check(self.state)['id'], 'outline')

    def test_unimplemented_contracts_defer_outline_but_never_mark_it_passed(self):
        with self.state:
            self.state.execute("INSERT INTO coverage VALUES ('other','pending','unsupported contract')")
        self.assertEqual(next_check(self.state)['id'], 'search')
        self.passed('search')
        self.passed('symbol')
        self.assertIsNone(next_check(self.state))
        self.assertEqual(self.state.execute("SELECT count(*) FROM checks WHERE status='pending'").fetchone()[0], 2)
        with self.state:
            self.state.execute("UPDATE coverage SET status='inapplicable' WHERE feature='other'")
        self.assertEqual(next_check(self.state)['id'], 'outline')

    def test_one_failure_or_unfinished_check_is_enough_to_defer_outline(self):
        self.passed('search')
        self.passed('symbol')
        for status, verdict in (('complete', 'fail'), ('complete', 'unsupported'),
                                ('complete', 'error'), ('complete', None), ('running', None)):
            with self.subTest(status=status, verdict=verdict):
                with self.state:
                    self.state.execute('UPDATE checks SET status=?,verdict=? WHERE id=?', (status, verdict, 'symbol'))
                self.assertIsNone(next_check(self.state))
        self.passed('symbol')
        self.assertEqual(next_check(self.state)['id'], 'outline')

    def test_large_case_populations_use_indexes_not_repeated_full_scans(self):
        schedule = self.state.execute("""EXPLAIN QUERY PLAN SELECT * FROM checks WHERE status='pending'
            ORDER BY (feature='outline' OR feature GLOB 'outline:*'),feature,subject LIMIT 1""").fetchall()
        blockers = self.state.execute("""EXPLAIN QUERY PLAN SELECT 1 FROM checks
            WHERE NOT (feature='outline' OR feature GLOB 'outline:*')
            AND (status!='complete' OR verdict IS NOT 'pass') LIMIT 1""").fetchall()
        self.assertTrue(any('checks_schedule' in row[3] for row in schedule), [tuple(row) for row in schedule])
        self.assertTrue(any('checks_outline_blockers' in row[3] for row in blockers), [tuple(row) for row in blockers])

    def test_non_outline_navigation_primes_shared_symbol_queries_once(self):
        client = Mock()
        client.parallel_safe = True
        client.call.return_value = {'symbols': [{'name': 'Alpha', 'file': 'A.java', 'line': 1}]}
        oracle = InvocationOracle(client, self.state)
        fixture = Fixture(self.root, self.root / 'unused', self.root / 'unused', self.state, oracle,
                          symbol_initials={'A', '$', 'Ω'})
        with patch.object(oracle, 'prefetch', wraps=oracle.prefetch) as prefetch:
            self.assertEqual(len(fixture.oracle_symbols({'id': 'search'}, 'Alpha')), 1)
            self.assertEqual(len(fixture.oracle_symbols({'id': 'symbol'}, 'Alpha')), 1)
            self.assertEqual(prefetch.call_count, 1)
        self.assertEqual(client.call.call_count, 3)
        self.assertEqual({call.args[1]['query'] for call in client.call.call_args_list}, {'A', '$', 'Ω'})


if __name__ == '__main__':
    unittest.main()
