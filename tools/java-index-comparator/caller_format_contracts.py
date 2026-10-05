"""Java caller rendering from authored lines/trees, not DB or MCP equivalence."""
from collections import Counter
import json
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'global:format:java-callers', 'global:format:java-call-tree'}
REASON = ('independent source/state: disposable Java caller snippets/pages and owner '
          'tree identities, JSON/text, depth/limits, cycles/repeated expansion and '
          'attached-root rendering with fresh/unbuilt graphs; not MCP equivalence '
          'or compiler-wide semantic dispatch')
FILENAME = 'src/Probe "λ".java'
SOURCE = '''package fixture.{owner};
class Probe {{
    void leaf() {{}}
    void alpha() {{ leaf(); leaf(); beta(); }} // "λ" ''' + 'λ' * 40 + '''
    void beta() {{ leaf(); alpha(); }}
    void top() {{ alpha(); }}
    // leaf() is prose, not a call
    String text = "leaf()";
}}
'''


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-caller-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views, module diagrams, navigation and '
                       'callers/call-tree formats have separate executed contracts; search, '
                       'exploration, analysis, management and lifecycle formats remain unresolved',))


def document(output):
    try:
        return json.loads(output)
    except ValueError:
        return None


def caller_page(output, format, runner, candidates, limit):
    rows, total, metadata = [], None, True
    if format == 'json':
        doc = document(output)
        metadata = (isinstance(doc, dict) and set(doc) == {'schema_version', 'items', 'pagination'}
                    and type(doc.get('schema_version')) is int and doc['schema_version'] == 2
                    and isinstance(doc.get('items'), list))
        if metadata:
            for row in doc['items']:
                if (not isinstance(row, dict) or set(row) != {'path', 'line', 'content'}
                        or type(row.get('line')) is not int or not isinstance(row.get('path'), str)
                        or row['path'].startswith('[')):
                    metadata = False
                    continue
                rows.append((runner.path(row['path']), row['line'], row['content']))
            page = doc.get('pagination')
            want = {'total': len(candidates), 'returned': min(limit, len(candidates)),
                    'limit': limit, 'truncated': limit < len(candidates)}
            metadata = metadata and page == want
            if isinstance(page, dict):
                metadata = metadata and all(type(page.get(k)) is int for k in ('total', 'returned', 'limit')) \
                    and type(page.get('truncated')) is bool
                total = page.get('total')
    else:
        header = re.search(r"^Callers of 'leaf' \(showing (\d+) of (\d+)\):$", output, re.M)
        metadata = header is not None and int(header[1]) == min(limit, len(candidates))
        total = int(header[2]) if header else None
        path = None
        for line in output.splitlines():
            match = re.fullmatch(r'  (.+\.java):', line)
            if match:
                path = runner.path(match[1])
            match = re.fullmatch(r'    :(\d+) (.*)', line)
            if match:
                rows.append((path, int(match[1]), match[2]))
    observed, available = Counter(rows), Counter(candidates)
    return {'metadata': bool(metadata), 'total': total, 'returned': len(rows),
            'identities_and_snippets': not bool(observed - available),
            'complete': limit < len(candidates) or observed == available,
            'ansi': '\x1b' in output}


def tree_rows(output, format, runner, query, depth, limit):
    if format == 'json':
        doc = document(output)
        if not (isinstance(doc, dict) and set(doc) ==
                {'schema_version', 'function', 'max_depth', 'limit_per_level', 'items', 'count'}
                and type(doc['schema_version']) is int and doc['schema_version'] == 2
                and doc['function'] == query and type(doc['max_depth']) is int and doc['max_depth'] == depth
                and type(doc['limit_per_level']) is int and doc['limit_per_level'] == limit
                and isinstance(doc['items'], list) and type(doc['count']) is int
                and doc['count'] == len(doc['items'])):
            return {'invalid-document': True}
        rows = []
        for row in doc['items']:
            if not (isinstance(row, dict) and set(row) == {'depth', 'name', 'path', 'line', 'status'}
                    and type(row['depth']) is int and 1 <= row['depth'] <= depth
                    and type(row['line']) is int and row['line'] > 0
                    and isinstance(row['path'], str) and not row['path'].startswith('[')
                    and row['status'] in ('shown', 'expanded_above', 'recursive')):
                return {'invalid-node': True}
            rows.append((row['depth'], row['name'], runner.path(row['path']), row['line'], row['status']))
        return rows
    if not output.startswith(f"Call tree for '{query}':\n  {query}\n") or '\x1b' in output:
        return {'invalid-header': True}
    rows = []
    for line in output.splitlines()[2:]:
        match = re.fullmatch(r'( +)← (\w+) \((.+):(\d+)\)( \(expanded above\))?', line)
        cycle = re.fullmatch(r'( +)← (\w+) \(recursive\)', line)
        if match and len(match[1]) % 2 == 0:
            rows.append((len(match[1]) // 2 - 1, match[2], runner.path(match[3]), int(match[4]),
                         'expanded_above' if match[5] else 'shown'))
        elif cycle and len(cycle[1]) % 2 == 0:
            rows.append((len(cycle[1]) // 2 - 1, cycle[2], None, None, 'recursive'))
        else:
            return {'invalid-node': True}
    return rows


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('caller format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='caller-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for owner in ('project', 'attached'):
        path = directory / owner / FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(SOURCE.format(owner=owner))
        (path.parent.parent / 'Inventory.kt').write_text('// inventory only\n')
        (path.parent.parent / 'descriptor.xml').write_text('<fixture/>\n')
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, {'.java': 1, '.kt': 1, '.xml': 1}, inventory)
            if inventory != {'.java': 1, '.kt': 1, '.xml': 1}:
                raise ToolError('caller format fixture full inventory incomplete')
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force')
    runner.command('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force')
    scopes = [('all', [], ['project', 'attached']), ('local', ['--local'], ['project']),
              ('attached', ['--subtree', 'attached-label'], ['attached']),
              ('absent', ['--subtree', 'absent'], [])]
    for mode in ('unbuilt', 'fresh'):
        if mode == 'fresh':
            runner.command('graph', 'build')
        for label, flags, owners in scopes:
            candidates = [(owner + '/' + FILENAME, line, SOURCE.format(owner=owner).splitlines()[line - 1][:70])
                          for owner in owners for line in (4, 5)]
            for format in ('json', 'text'):
                for limit in (0, 1, 100):
                    _, output = runner.command('--format', format, *flags, 'callers', 'leaf', '--limit', limit)
                    key = f'{mode}:{label}:{format}:{limit}'
                    record('global:format:java-callers', key,
                           {'metadata': True, 'total': len(candidates), 'returned': min(limit, len(candidates)),
                            'identities_and_snippets': True, 'complete': True, 'ansi': False},
                           caller_page(output, format, runner, candidates, limit))
                for query, depth, limit in (('leaf', 0, 100), ('leaf', 1, 0), ('leaf', 1, 100),
                                             ('absentMethod', 3, 100), ('leaf', 3, 100), ('leaf', 3, 1)):
                    if label == 'all' and depth == 3 and query == 'leaf':
                        continue  # Multi-root compiler dispatch is a separate pending contract.
                    want = []
                    for owner in owners:
                        path = owner + '/' + FILENAME
                        if query == 'leaf' and depth > 0 and limit > 0:
                            if depth == 1:
                                want += [(1, 'alpha', path, 4, 'shown'), (1, 'beta', path, 5, 'shown')]
                            else:
                                cycle_path, cycle_line = (path, 4) if format == 'json' else (None, None)
                                want += [(1, 'alpha', path, 4, 'shown'), (2, 'beta', path, 5, 'shown'),
                                         (3, 'alpha', cycle_path, cycle_line, 'recursive')]
                                if limit > 1:
                                    want += [(2, 'top', path, 6, 'shown'), (1, 'beta', path, 5, 'expanded_above')]
                    _, output = runner.command('--format', format, *flags, 'call-tree', query,
                                               '--depth', depth, '--limit', limit)
                    got = tree_rows(output, format, runner, query, depth, limit)
                    # Depth-one all-root order can depend on display decoration.
                    if depth == 1 and isinstance(got, list):
                        want, got = sorted(want), sorted(got)
                    record('global:format:java-call-tree', f'{mode}:{label}:{format}:{query}:{depth}:{limit}', want, got)
    return expected, actual
