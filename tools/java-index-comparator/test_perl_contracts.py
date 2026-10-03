"""Public synthetic Perl cases execute the CLI; fake MCP is adapter evidence only."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from audit import Fixture, SCHEMA, plan
from common import connect
from test_mobile_text_contracts import PagedTextOracle


CASES = {
    'perl-exports': ('.pm', 'our @EXPORT = qw(other);\nour @EXPORT_OK = qw(needle);\n', 2),
    'perl-subs': ('.pl', 'sub other { "needle" }\nsub needle {}\n', 2),
    'perl-pod': ('.pod', '=head1 Other\n=head2 needle\n', 2),
    'perl-tests': ('.t', 'ok(1, "other");\nis(1, 1, "needle");\n', 2),
    'perl-imports': ('.pm', 'use strict;\nuse Other "needle";\nuse needle;\n', 3),
}


class PerlContracts(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts' / 'tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=artifacts)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        self.state = connect(self.directory / 'checks.sqlite')
        self.state.executescript(SCHEMA)
        self.fixture = Fixture(self.root, self.binary, self.directory / 'index.sqlite', self.state, None)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def locations(self, output):
        return [(match[1], int(match[2])) for line in output.splitlines()
                if (match := re.fullmatch(r'  (.+):(\d+)', line))]

    def test_filters_precede_limits_for_all_five_commands(self):
        for feature, (suffix, body, line) in CASES.items():
            with self.subTest(feature=feature):
                path = self.root / ('Example' + suffix)
                path.write_text(body)
                output = self.fixture.text_cli(feature, 'NEEDLE', '--limit', '1')
                self.assertEqual(self.locations(output), [(path.name, line)])
                # Name filters cannot match only an argument or function body.
                if feature in {'perl-subs', 'perl-imports'}:
                    self.assertEqual(self.locations(self.fixture.text_cli(feature, 'needle', '--limit', '100')),
                                     [(path.name, line)])
                path.unlink()

    def test_imports_skip_exact_pragmas_not_module_prefixes(self):
        (self.root / 'Example.pm').write_text('''use strict;
use\twarnings;
use parent 'Example';
use strictness;
use warnings::register;
use constantine;
require strict;
use v5.36;
use v5compat;
''')
        output = self.fixture.text_cli('perl-imports', '--limit', '10')
        self.assertEqual(self.locations(output), [('Example.pm', n) for n in (4, 5, 6, 7, 9)])

    def test_export_identifiers_have_exact_boundaries(self):
        (self.root / 'Example.pm').write_text('''our @EXPORT_BAD = qw(other);
our @EXPORT_OKAY = qw(other);
our @EXPORT;
our @EXPORT_OK = qw(needle);
@EXPORT = qw(needle);
''')
        output = self.fixture.text_cli('perl-exports', '--limit', '10')
        self.assertEqual(self.locations(output), [('Example.pm', n) for n in (3, 4, 5)])

    def test_pages_are_source_order_prefixes_and_zero_is_empty(self):
        for feature, (suffix, body, _) in CASES.items():
            with self.subTest(feature=feature):
                # Reverse creation order must not influence pagination.
                for name in ('Z', 'A'):
                    (self.root / (name + suffix)).write_text(body)
                full = self.locations(self.fixture.text_cli(feature, '--limit', '100'))
                self.assertTrue(full)
                self.assertEqual(full, sorted(full))
                for limit in (0, 1, 3):
                    self.assertEqual(self.locations(self.fixture.text_cli(feature, '--limit', str(limit))), full[:limit])
                for name in ('Z', 'A'):
                    (self.root / (name + suffix)).unlink()

    def test_audit_contracts_use_scoped_paginated_oracle_and_detect_wrong_output(self):
        import perl_contracts
        for feature, (suffix, body, _) in CASES.items():
            path = self.root / ('Example' + suffix)
            path.write_text(body)
            self.fixture._inventory_ready = False
            self.fixture.client = PagedTextOracle(self.root)
            for query in (None, '', 'needle', 'NEEDLE', '[', '__audit_absent_perl__'):
                with self.subTest(feature=feature, query=query):
                    self.state.execute('INSERT OR REPLACE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                       (feature, feature, json.dumps({'query': query})))
                    self.state.commit()
                    check = self.state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
                    self.fixture.evaluate(check)
                    result = self.state.execute('SELECT verdict,error,diff_json FROM checks WHERE id=?', (feature,)).fetchone()
                    self.assertEqual(result[0], 'pass', tuple(result))
            check = self.state.execute('SELECT * FROM checks WHERE id=?', (feature,)).fetchone()
            with patch.object(self.fixture, 'text_cli', return_value=perl_contracts.HEADERS[feature] + ' (0):\n'):
                # This check expects a nonempty result, so a valid empty shape fails.
                changed = dict(check)
                changed['subject'] = json.dumps({'query': None})
                self.fixture.evaluate(changed)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (feature,)).fetchone()[0], 'fail')
            self.assertTrue(any('cursor' in request for request in self.fixture.client.requests))
            path.unlink()

    def test_full_inventory_prevents_false_language_absence(self):
        import perl_contracts
        help_text = '  class  Classes\n  symbol  Symbols\n  file  Files'
        plan(self.state, [], help_text, [], self.root)
        for feature in CASES:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'inapplicable')
            check = self.state.execute('SELECT * FROM checks WHERE feature=? LIMIT 1', (feature,)).fetchone()
            self.fixture.evaluate(check)
            self.assertEqual(self.state.execute('SELECT verdict FROM checks WHERE id=?', (check['id'],)).fetchone()[0], 'pass')
        self.assertEqual(self.state.execute('SELECT count(*) FROM oracle_pages').fetchone()[0], 0)
        (self.root / 'build').mkdir()
        for suffix in ('.pm', '.pl', '.pod', '.t'):
            (self.root / 'build' / ('Example' + suffix)).write_text('synthetic\n')
        plan(self.state, [], help_text, [], self.root)
        for feature in CASES:
            self.assertEqual(self.state.execute('SELECT status FROM coverage WHERE feature=?', (feature,)).fetchone()[0], 'pending')
        self.assertEqual(set(perl_contracts.EXTENSIONS), set(CASES))

    def test_relevant_links_and_uppercase_suffixes_cannot_prove_absence(self):
        import mobile_contracts
        import perl_contracts
        self.assertEqual(perl_contracts.applicability(self.state, 'perl-imports')[0], 'pending')
        for suffix in ('.PM', '.Pl', '.POD', '.T'):
            path = self.root / ('Example' + suffix)
            path.write_text('synthetic\n')
            mobile_contracts.inventory(self.state, self.root)
            for feature, extensions in perl_contracts.EXTENSIONS.items():
                if suffix.lower() in extensions:
                    self.assertEqual(perl_contracts.applicability(self.state, feature)[0], 'pending')
            path.unlink()
        path = self.root / 'Example.pm'
        path.symlink_to(self.directory / 'unresolved.pm')
        mobile_contracts.inventory(self.state, self.root)
        for feature in CASES:
            self.assertEqual(perl_contracts.applicability(self.state, feature)[0], 'pending')
        path.unlink()
        (self.root / 'linked').symlink_to(self.directory / 'elsewhere', target_is_directory=True)
        # Create the link target inside the disposable fixture, never probe an
        # external project to establish whether a directory link is relevant.
        (self.directory / 'elsewhere').mkdir()
        mobile_contracts.inventory(self.state, self.root)
        for feature in CASES:
            self.assertEqual(perl_contracts.applicability(self.state, feature)[0], 'pending')

    def test_perl_content_edits_invalidate_the_inventory_snapshot(self):
        import mobile_contracts
        path = self.root / 'Example.pm'
        path.write_text('sub alpha {}\n')
        before = path.stat()
        digest = mobile_contracts.inventory_snapshot(self.root)
        path.write_text('sub bravo {}\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(mobile_contracts.inventory_snapshot(self.root), digest)

    def test_document_regex_keeps_anchors_on_the_matched_physical_line(self):
        import perl_contracts
        for feature, (_, body, _) in CASES.items():
            with self.subTest(feature=feature):
                # Whole-document MCP regexes must not absorb the blank lines
                # preceding a match or join half-statements across lines.
                document = '\n\n' + body
                pattern = perl_contracts.query_pattern(feature, None, '.pm')
                matches = list(re.finditer(pattern, document, re.MULTILINE))
                self.assertTrue(matches)
                self.assertTrue(all('\n' not in match[0] for match in matches))
                self.assertGreaterEqual(document.count('\n', 0, matches[0].start()) + 1, 3)
        self.assertIsNone(re.search(perl_contracts.query_pattern('perl-subs', None, '.pm'),
                                    'sub\nname {}', re.MULTILINE))

    def test_bad_oracle_scope_and_caps_remain_unsupported(self):
        (self.root / 'Example.pm').write_text('sub example {}\n')
        class Oracle:
            def __init__(self, matches):
                self.matches = matches
            def call(self, tool, arguments):
                assert arguments['paths'] == ['Example.pm']
                return {'matches': self.matches}
        for matches in ([{'file': 'Other.pm', 'line': 1}],
                        [{'file': 'Example.pm', 'line': 1}] * 5000):
            self.state.execute("INSERT OR REPLACE INTO checks(id,feature,subject) VALUES ('cap','perl-subs',?)",
                               (json.dumps({'query': None}),))
            self.state.commit()
            self.fixture.client = Oracle(matches)
            check = self.state.execute("SELECT * FROM checks WHERE id='cap'").fetchone()
            self.fixture.evaluate(check)
            self.assertEqual(self.state.execute("SELECT verdict FROM checks WHERE id='cap'").fetchone()[0], 'unsupported')


if __name__ == '__main__':
    unittest.main()
