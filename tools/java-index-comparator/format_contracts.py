"""Java line-search rendering on authored sources, independently of MCP/DB.

The parent format contract remains pending: this family does not establish
format support for lifecycle, module, graph or other navigation commands.
"""
from collections import Counter
import json
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner


FEATURES = {'global:format:java-text-search', 'global:format:java-text-search-roots'}
REASON = ('independent source/state: disposable Java text-search identities, snippets, '
          'JSON/text rendering, filters, limits and attached-root scope; not MCP equivalence')
SOURCE = '''package fixture;
class Probe {
    // TODO needle "quoted" \\ unicode λ ''' + 'λ' * 90 + '''
    // FIXME other
    @Deprecated(since="needle")
    @SuppressWarnings("needle")
    @Tag("needle")
    @Inject Widget service;
    @Provides
    Widget provide() { return null; }
    @Binds Widget bind(Widget input) { return input; }
    @DeepLink("app://needle")
    void route() {}
    @Deprecated(since="other") void old() {}
    @SuppressWarnings("other") void other() {}
    // TODO second
}
'''
# Authored declaration/text identities, not a native query or native DB.
SITES = {'todo': [3, 4, 16], 'deprecated': [5, 14], 'suppress': [6, 15],
         'annotations': [7], 'inject': [8], 'provides': [9, 11], 'deeplinks': [12]}
ARGS = {'todo': [], 'deprecated': [], 'suppress': [], 'annotations': ['Tag'],
        'inject': ['Widget'], 'provides': ['Widget'], 'deeplinks': []}
FILTERS = {'todo': ['TODO'], 'deprecated': ['NEEDLE'],
           'suppress': ['NEEDLE'], 'annotations': ['@Tag'], 'inject': ['Missing'],
           'provides': ['Missing'], 'deeplinks': ['NEEDLE']}
ABSENT = {command: ['AbsentContractMatch'] for command in SITES}


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-text-search'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java line-search JSON/text and root contracts implemented separately; '
                       'other Java-applicable command formats and invalid diagram-format handling remain unresolved',))


def rows(command, paths, mode='all'):
    numbers = SITES[command]
    if mode == 'filter':
        if command == 'todo':
            numbers = [3, 16]
        else:
            numbers = numbers[:1] if command not in {'provides', 'inject'} else []
    elif mode == 'absent':
        numbers = []
    lines = SOURCE.splitlines()
    result = []
    for path in paths:
        for line in numbers:
            content_line = 10 if command == 'provides' and line == 9 else line
            content = lines[content_line - 1]
            if command != 'todo':
                content = content.strip()
            item = {'path': path, 'line': line, 'content': content[:100 if command in {'provides', 'deeplinks'} else 80]}
            if command == 'todo':
                item['category'] = 'FIXME' if line == 4 else 'TODO'
            elif command == 'provides':
                item['name'] = 'provide' if line == 9 else 'bind'
            result.append(item)
    return result


def observe(output, format, command, runner, candidates, limit):
    """Validate real identities and metadata, preserving duplicate rows.

    Limited parallel grep pages need not have deterministic membership.
    Validate them against authored candidates; require exact full-page equality.
    """
    valid_json, metadata, count, items = True, True, None, []
    if format == 'json':
        try:
            document = json.loads(output)
            valid_json = isinstance(document, dict) and set(document) == {'items', 'count'}
            count = document.get('count') if isinstance(document, dict) else None
            items = document.get('items', []) if isinstance(document, dict) else []
        except ValueError:
            valid_json = False
        metadata = (valid_json and type(count) is int and isinstance(items, list) and
                    all(isinstance(row, dict) and set(row) ==
                        {'path', 'line', 'content'} | ({'category'} if command == 'todo' else
                                                     {'name'} if command == 'provides' else set())
                        for row in items))
    else:
        for line in output.splitlines():
            if match := re.fullmatch(r'  (.+\.java):(\d+)', line):
                items.append({'path': match[1], 'line': int(match[2]), 'content': None})
            elif line.startswith('    ') and items:
                items[-1]['content'] = line[4:]
        header = output.splitlines()[0] if output.splitlines() else ''
        match = re.fullmatch(r'Found (\d+) comments:', header) if command == 'todo' else re.search(r'\((\d+)\):$', header)
        count = int(match[1]) if match else None
        candidates = [{k: v for k, v in row.items() if k not in {'name', 'category'}} for row in candidates]
    paths_valid = True
    normalized = []
    if not isinstance(items, list):
        items, metadata = [], False
    for row in items:
        if not isinstance(row, dict):
            metadata = False
            continue
        row = dict(row)
        try:
            # Decorations are text metadata, never part of a JSON path.
            raw = row.get('path', '')
            if not isinstance(raw, str) or (format == 'json' and raw.startswith('[')):
                raise ToolError('invalid structured path')
            row['path'] = runner.path(raw)
        except (ToolError, TypeError, ValueError):
            paths_valid = False
        if type(row.get('line')) is not int or row['line'] < 1 or not isinstance(row.get('content'), str):
            metadata = False
        normalized.append(row)
    candidate_counts = Counter(json.dumps(row, sort_keys=True) for row in candidates)
    observed_counts = Counter(json.dumps(row, sort_keys=True) for row in normalized)
    return {'json': valid_json, 'metadata': metadata, 'count': count,
            'identities': not (observed_counts - candidate_counts),
            'complete': limit < len(candidates) or observed_counts == candidate_counts,
            'paths': paths_valid, 'ansi': '\x1b' in output,
            'returned': len(items)}


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    for folder in (runner.root, directory / 'attached'):
        folder.mkdir()
        (folder / 'Probe.java').write_text(SOURCE)
    (runner.root / '.git').mkdir()
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        if state.execute("SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0] != 1:
            raise ToolError('applicable Java format fixture inventory incomplete')
    finally:
        state.close()
    runner.command('rebuild', '--force')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
    scopes = [('primary', ['--local'], ['project/Probe.java']),
              ('combined', [], ['project/Probe.java', 'attached/Probe.java']),
              ('attached', ['--subtree', 'attached'], ['attached/Probe.java']),
              ('missing', ['--subtree', 'missing'], [])]
    for command in sorted(SITES):
        for scope, flags, paths in scopes:
            feature = 'global:format:java-text-search' if scope == 'primary' else 'global:format:java-text-search-roots'
            for mode, queries in [('all', ARGS), ('filter', FILTERS), ('absent', ABSENT)]:
                candidates = rows(command, paths, mode)
                for limit in (0, 1, 100):
                    arguments = [*flags, command, *queries[command], '--limit', str(limit)]
                    for format in ('json', 'text'):
                        # Exercise clap's global flag both before and after the command.
                        arguments_with_format = ['--format', format, *arguments] if limit != 1 else [*arguments, '--format', format]
                        _, output = runner.command(*arguments_with_format)
                        key = f'{command}:{scope}:{mode}:{limit}:{format}'
                        expected[feature][key] = {'json': True, 'metadata': True,
                            'count': min(limit, len(candidates)), 'identities': True, 'complete': True,
                            'paths': True, 'ansi': False, 'returned': min(limit, len(candidates))}
                        actual[feature][key] = observe(output, format, command, runner, candidates, limit)
    return expected, actual
