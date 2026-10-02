"""Compact production regressions for map scopes, identity and stable limits."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from build_index import build_ast_index
from common import canonical_json, connect


class MapContracts(unittest.TestCase):
    def test_map_preserves_declaration_parents_literal_scopes_and_limited_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            # Equal directory sizes exercise the tie breaker. Duplicate names
            # and wildcard-looking paths exercise independent identities/scopes.
            sources = {
                'a_one/Shared.java': 'package first; class Shared extends ParentA {}\n',
                'abone/Shared.java': 'package second; class Shared extends ParentB {}\n',
                'A_one/Other.java': 'class Other {}\n',
                'b_two/Last.java': 'class Last {}\n',
                'rate%/Percent.java': 'class Percent {}\n',
                'rateX/Plain.java': 'class Plain {}\n',
            }
            for path, content in sources.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content)
            binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
            database = directory / 'index.sqlite'
            build_ast_index(str(binary), root, database, 'map-contracts')
            state = connect(directory / 'checks.sqlite')
            try:
                state.executescript(SCHEMA)
                plan(state, [{'path': path} for path in sources], '  class  Classes\n  symbol  Symbols\n  file  Files')
                fixture = Fixture(root, binary, database, state, None)
                checks = state.execute("SELECT * FROM checks WHERE feature='map' ORDER BY subject")
                for check in checks:
                    with self.subTest(subject=check['subject']):
                        fixture.evaluate(check)
                        result = state.execute('SELECT verdict,error FROM checks WHERE id=?', (check['id'],)).fetchone()
                        self.assertEqual(result[0], 'pass', tuple(result))
                detail = fixture.cli('map', '--module', '', '--per-dir', '20', '--limit', '20')
                parents = {group['path']: item.get('parents', []) for group in detail['groups']
                           for item in group['symbols'] if item['name'] == 'Shared'}
                self.assertEqual(parents, {'a_one/': ['ParentA'], 'abone/': ['ParentB']})
                # This is a behaviour check, not a JSON-shape check: removing a
                # directory still produces valid JSON and must fail the fixture.
                check = state.execute("SELECT * FROM checks WHERE feature='map' AND subject=?",
                                      (canonical_json({'module': None}),)).fetchone()
                real_cli = fixture.cli

                def omit_directory(*arguments):
                    output = real_cli(*arguments)
                    output['groups'] = []
                    return output

                with patch.object(fixture, 'cli', side_effect=omit_directory):
                    fixture.evaluate(check)
                self.assertEqual(state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'fail')
                self.assertEqual(state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
                self.assertTrue(state.execute("SELECT reason FROM coverage WHERE feature='map'").fetchone()[0].startswith('internal CLI'))
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()
