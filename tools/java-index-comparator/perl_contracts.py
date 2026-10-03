"""Per-file MCP lexical anchors for Perl; absence uses full source inventory.

These contracts compare line-oriented grep commands, not Perl semantic
navigation. Unknown ignored/link scopes remain pending rather than passing.
"""
from pathlib import Path
import re

from common import ToolError, canonical_json, stable_id
import mobile_contracts


EXTENSIONS = {
    'perl-exports': ('.pm',),
    'perl-subs': ('.pm', '.pl', '.t'),
    'perl-pod': ('.pm', '.pl', '.pod'),
    'perl-tests': ('.t', '.pm', '.pl'),
    'perl-imports': ('.pm', '.pl', '.t'),
}
HEADERS = {
    'perl-exports': 'Perl exports', 'perl-subs': 'Perl subroutines',
    'perl-pod': 'POD documentation', 'perl-tests': 'Perl tests',
    'perl-imports': 'Perl imports',
}
PATTERNS = {
    'perl-exports': r'\bour\s+@EXPORT(?:_OK)?\b|@EXPORT(?:_OK)?\s*=',
    'perl-subs': r'^\s*sub\s+\w+',
    'perl-pod': r'^=(head[1-4]|item|over|back|pod|cut|begin|end|for)\b',
    'perl-tests': r'\b(ok|is|isnt|like|unlike|cmp_ok|is_deeply|diag|pass|fail|subtest|plan|done_testing|SKIP|TODO)\s*[(\{]',
    'perl-imports': r'^\s*(use|require)\s+[A-Za-z]',
}


def applicable_paths(state, feature):
    return mobile_contracts.applicable_paths(state, feature, EXTENSIONS)


def applicability(state, feature):
    return mobile_contracts.applicability(state, feature, EXTENSIONS)


def query_pattern(feature, query, extension):
    # Confirm broad anchors with MCP; independently apply the CLI's literal
    # line or declared-name filter. Query metacharacters cannot become regex.
    # MCP searches a whole document, whereas grep searches one physical line.
    # Whitespace must never consume a newline and move the reported anchor.
    return PATTERNS[feature].replace(r'\s', r'[^\S\r\n]')


def accepts(feature, query, line):
    text = line
    if feature in {'perl-imports', 'perl-subs'}:
        declaration = re.match(r'^\s*(sub|use|require)\s+([\w:]+)', line)
        if not declaration:
            return False
        keyword, text = declaration.groups()
        if feature == 'perl-imports' and keyword == 'use' and (
                text in {'strict', 'warnings', 'constant', 'base', 'parent', 'utf8'} or
                re.fullmatch(r'v[0-9]+', text)):
            return False
    return not query or query.lower() in text.lower()


def output_locations(feature, output, root, limit):
    lines = output.splitlines()
    header = re.fullmatch(re.escape(HEADERS[feature]) + r' \((\d+)\):', lines[0]) if lines else None
    if header is None or (len(lines) - 1) % 2:
        raise ToolError('unrecognized Perl search output')
    entries = []
    for index in range(1, len(lines), 2):
        match = re.fullmatch(r'  (.+):(\d+)', lines[index])
        if not match or int(match[2]) < 1 or not lines[index + 1].startswith('    '):
            raise ToolError('unrecognized Perl location/content pair')
        path = Path(match[1])
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError as error:
                raise ToolError('Perl search path outside target') from error
        if '..' in path.parts or path.suffix not in EXTENSIONS[feature]:
            raise ToolError('Perl search path outside language scope')
        entries.append((path.as_posix(), int(match[2])))
    if len(entries) != int(header[1]) or len(entries) > limit or len(set(entries)) != len(entries):
        raise ToolError('Perl count does not match unique rendered locations')
    return entries


def plan_perl(state, root):
    if root is None:
        return
    # plan_mobile has just completed the shared atomic full-type inventory.
    with state:
        for feature in EXTENSIONS:
            status, reason = applicability(state, feature)
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, status, reason))
            queries = {None, '', 'a', 'A', '[', '__audit_absent_perl__'}
            if status == 'implemented':
                # Stream source-derived representative filters, so a project
                # with no letter "a" still exercises nonempty filter pages.
                for row in applicable_paths(state, feature):
                    with (root / row['path']).open(encoding='utf-8') as source:
                        for line in source:
                            if re.search(PATTERNS[feature], line):
                                match = (re.match(r'^\s*(?:sub|use|require)\s+([\w:]+)', line)
                                         if feature in {'perl-subs', 'perl-imports'} else
                                         re.search(r'(\b[A-Za-z_]\w*\b)', line))
                                if match:
                                    for query in (match[1], match[1].upper()):
                                        subject = canonical_json({'query': query})
                                        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                                      (stable_id({'feature': feature, 'subject': subject}), feature, subject))
            for query in sorted(queries, key=lambda value: (value is not None, value or '')):
                subject = canonical_json({'query': query})
                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                              (stable_id({'feature': feature, 'subject': subject}), feature, subject))
