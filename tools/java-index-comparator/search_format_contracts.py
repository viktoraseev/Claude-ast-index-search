"""Java literal/ranked search rendering from authored source, not MCP truth.

Intent fallback budgets and exploration rendering remain pending separately.
"""
from collections import Counter
import json
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from navigation_format_contracts import expected_page, page
from root_contracts import Runner

FEATURES = {'global:format:java-search', 'global:format:java-ranked-search'}
REASON = ('independent source/state: disposable Java literal/OR search sections, '
          'reference counts, ranked/unranked JSON/text pages, Unicode snippets, '
          'missing index and attached-root rendering; not MCP equivalence or '
          'intent-fallback/exploration coverage')
FILENAME = 'src/Probe "λ".java'
SOURCE = '''package fixture.{owner};
class Probe {{
 int signal() {{
  return {value};
 }}
 int consume() {{
  return signal();
 }}
 // signal "λ" ''' + 'λ' * 110 + '''
 // signal λλλ
}}
'''
SECTIONS = {'files': 'Files by path', 'symbols': 'Symbols',
            'references': 'References', 'content_matches': 'Content matches'}


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-search-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views, module diagrams, navigation, callers/call-tree '
                       'and literal/ranked search formats have separate executed contracts; intent '
                       'fallback, exploration, analysis, management and lifecycle formats remain unresolved',))


def document(output):
    try:
        return json.loads(output)
    except ValueError:
        return None


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('search format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='search-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
    sources = {}

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for owner, value in [('project', 7), ('attached', 107)]:
        path = directory / owner / FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(SOURCE.format(owner=owner, value=value))
        sources[owner] = path.read_text().splitlines()
        (path.parent / 'Inventory.kt').write_text('// inventory only\n')
        (path.parent / 'descriptor.xml').write_text('<fixture/>\n')
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, {'.java': 1, '.kt': 1, '.xml': 1}, inventory)
            if inventory != {'.java': 1, '.kt': 1, '.xml': 1}:
                raise ToolError('search format fixture full inventory incomplete')
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    for feature in sorted(FEATURES):
        rank = ['--rank', 'central'] if feature.endswith('ranked-search') else []
        for format in ('json', 'text'):
            code, output = runner.command('--format', format, 'search', 'signal', *rank, acceptable=(0, 1))
            record(feature, 'missing-index:' + format,
                   (1, '') if format == 'json' else (0, "Index not found. Run 'ast-index rebuild' first.\n"),
                   (code, output))
    runner.command('rebuild', '--force')
    runner.command('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force')
    runner.command('graph', 'build')

    scopes = [('all', [], ['project', 'attached']), ('local', ['--local'], ['project']),
              ('attached', ['--subtree', 'attached-label'], ['attached']),
              ('missing', ['--subtree', 'absent'], [])]
    queries = [('signal', 'signal'), (' signal, ,', 'signal'),
               ('signal,signal', 'signal'), ('Probe, signal', 'both'),
               ('signal λλλ', 'phrase'), ('signal()', 'call'),
               ('AbsentContractMatch', 'absent'), ('MissingOne,MissingTwo', 'absent'),
               (', ,', 'absent'), ('', 'absent'), ('  ', 'absent')]
    for feature in sorted(FEATURES):
        rank = ['--rank', 'central'] if feature.endswith('ranked-search') else []
        for scope, flags, owners in scopes:
            for query, mode in queries:
                if mode == 'phrase' and not owners:
                    # A genuinely absent intent uses explore's distinct
                    # report/budgets, outside the literal search contract.
                    continue
                candidates = {section: [] for section in SECTIONS}
                bodies = {}
                for owner in owners:
                    path = owner + '/' + FILENAME
                    if mode == 'both':
                        candidates['files'].append(path)
                    for name, kind, start, end in ([('Probe', 'class', 2, 11)] if mode == 'both' else []) + (
                            [('signal', 'function', 3, 5)] if mode in ('signal', 'both', 'call') else []):
                        identity = (path, start, f'fixture.{owner}.Probe' + ('.signal' if name == 'signal' else ''), kind)
                        candidates['symbols'].append(identity)
                        bodies[identity] = (''.join(f' {line:4d}\t{sources[owner][line - 1]}\n'
                                                   for line in range(start, end + 1)), end)
                    lines = ([2, 3, 7, 9, 10] if mode == 'both' else [3, 7, 9, 10] if mode == 'signal'
                             else [10] if mode == 'phrase' else [3, 7] if mode == 'call' else [])
                    candidates['content_matches'] += [(path, line, sources[owner][line - 1].strip()[:100]) for line in lines]
                if owners and mode in ('signal', 'both'):
                    candidates['references'] = [('signal', len(owners))]
                for limit in (0, 1, 100):
                    for content in (False, True):
                        args = [*flags, 'search', query, *rank, '--limit', limit,
                                *(['--with-content'] if content else [])]
                        for format in ('json', 'text'):
                            _, output = runner.command('--format', format, *args)
                            key = f'{scope}:{query}:{limit}:{content}:{format}'
                            doc = document(output) if format == 'json' else None
                            valid = (isinstance(doc, dict) and type(doc.get('schema_version')) is int
                                     and doc['schema_version'] == 2 and 'fallback' not in doc)
                            record(feature, key + ':valid', True, valid if format == 'json'
                                   else output.splitlines()[0] == f"Search results for '{query}'" +
                                   (' (ranked: central):' if rank else ':') and '\x1b' not in output)
                            if format == 'json':
                                summary = doc.get('rank') if valid else None
                                record(feature, key + ':ranking', True,
                                       (isinstance(summary, dict) and summary.get('preset') == 'central'
                                        and summary.get('applied') is True and summary.get('ranked_sections') == ['files', 'symbols']
                                        and summary.get('graph', {}).get('built') is True) if rank else summary is None)
                            snippet_rows = []
                            for section, title in SECTIONS.items():
                                if format == 'json':
                                    rows = doc.get(section, []) if valid else []
                                    pagination = doc.get('pagination', {}).get(section) if valid else None
                                    identities = []
                                    for row in rows:
                                        if rank and section in ('files', 'symbols'):
                                            dossier = row.get('rank')
                                            snippet_rows.append(isinstance(dossier, dict)
                                                                and type(dossier.get('score')) in (float, int)
                                                                and isinstance(dossier.get('components'), list))
                                        if section == 'files':
                                            identities.append(runner.path(row['path'] if rank else row))
                                        elif section == 'symbols':
                                            identity = (runner.path(row['path']), row.get('line'), row.get('qualified_name'), row.get('kind'))
                                            identities.append(identity)
                                            if content:
                                                want_body, end = bodies.get(identity, ('invalid', -1))
                                                snippet_rows.append(row.get('content') == want_body and row.get('end_line') == end
                                                                    and row.get('truncated') is False)
                                            else:
                                                snippet_rows.append('content' not in row and 'truncated' not in row)
                                        elif section == 'references':
                                            identities.append((row['name'], row['usage_count']))
                                        else:
                                            identities.append((runner.path(row['path']), row['line'], row['content']))
                                else:
                                    match = re.search(r'^' + re.escape(title) + r' \(showing (\d+) of (\d+)(?:, not ranked)?\):\n(.*?)(?=\n(?:Files by path|Symbols|References|Content matches) |\Z)', output, re.M | re.S)
                                    block = match[3] if match else ''
                                    pagination = ({'total': int(match[2]), 'returned': int(match[1]), 'limit': limit,
                                                   'truncated': int(match[1]) < int(match[2])} if match else None)
                                    if section == 'files':
                                        identities = [runner.path(p) for p in re.findall(r'^  (.+\.java)$', block, re.M)]
                                    elif section == 'symbols':
                                        identities = [(runner.path(p), int(line), name, kind) for name, kind, p, line in
                                                      re.findall(r'^  (.+) \[(\w+)\]: (.+\.java):(\d+)$', block, re.M)]
                                        want_lines = ''.join(bodies.get(row, ('invalid', -1))[0] for row in identities) if content else ''
                                        shown_lines = ''.join(line + '\n' for line in block.splitlines() if re.match(r'^ \s*\d+\t', line))
                                        snippet_rows.append(Counter(shown_lines.splitlines()) == Counter(want_lines.splitlines()))
                                    elif section == 'references':
                                        identities = [(name, int(count)) for name, count in re.findall(r'^  (.+) — used in (\d+) places$', block, re.M)]
                                    else:
                                        identities = [(runner.path(p), int(line), snippet) for p, line, snippet in
                                                      re.findall(r'^  (.+\.java):(\d+)\n    ([^\n]*)$', block, re.M)]
                                want = expected_page(candidates[section], limit)
                                if format == 'text' and not candidates[section]:
                                    want['pagination'] = None
                                record(feature, key + ':' + section, want,
                                       page(identities, candidates[section], pagination, limit))
                            record(feature, key + ':snippets', True, all(snippet_rows))
                            if format == 'text':
                                record(feature, key + ':empty-message', not any(candidates.values()),
                                       'No results found.' in output)
    return expected, actual
