"""Direct Java method-owner callers against position-bound live MCP evidence.

The name-only CLI unions same-name declarations, so the oracle must query
every source declaration with that name, not choose its first overload.
Source anchors schedule queries and normalize owner identities; native rows
never supply expected edges. Deep traversal has separate pending contracts.
"""
from pathlib import Path
import re
import subprocess

from common import McpRemoteError, ToolError, stable_id

FEATURE = 'call-tree:mcp-direct-callers'
FEATURES = {FEATURE}
REASON = ('live MCP direct Java caller owners; union of all same-name JDK source '
          'method/constructor/accessor anchors; not deep traversal or attached-root equivalence')
CALLABLE_KINDS = {'method', 'constructor', 'accessor'}
MAX_CALLERS = 100000


class UnsupportedHierarchy(ToolError):
    pass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS call_hierarchy_anchors(
 name TEXT NOT NULL,path TEXT NOT NULL,line INTEGER NOT NULL,
 column INTEGER NOT NULL,kind TEXT NOT NULL,
 PRIMARY KEY(name,path,line,column,kind)
);
CREATE INDEX IF NOT EXISTS call_hierarchy_anchor_name ON call_hierarchy_anchors(name);
CREATE INDEX IF NOT EXISTS call_hierarchy_anchor_path ON call_hierarchy_anchors(path);
CREATE TABLE IF NOT EXISTS call_hierarchy_usage_anchors(
 name TEXT NOT NULL,path TEXT NOT NULL,line INTEGER NOT NULL,column INTEGER NOT NULL,
 PRIMARY KEY(name,path,line,column)
);
CREATE INDEX IF NOT EXISTS call_hierarchy_usage_path ON call_hierarchy_usage_anchors(path);
'''


def plan_methods(state, root, source_files, structure):
    """File-sized parsing and disk-backed identity inventory, idempotent on resume."""
    for source in source_files:
        path = source['path']
        entries = structure(path)
        with state:
            state.execute('DELETE FROM call_hierarchy_anchors WHERE path=?', (path,))
            state.execute('DELETE FROM call_hierarchy_usage_anchors WHERE path=?', (path,))
            state.executemany('INSERT OR IGNORE INTO call_hierarchy_anchors VALUES (?,?,?,?,?)',
                ((entry['name'], path, entry['line'], entry['column'], entry['kind'])
                 for entry in entries if entry['kind'] in CALLABLE_KINDS))
            state.executemany('INSERT OR IGNORE INTO call_hierarchy_usage_anchors VALUES (?,?,?,?)',
                ((entry['name'], path, entry['line'], entry['column'])
                 for entry in entries if entry['kind'] == 'usage'))
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        for row in state.execute('SELECT DISTINCT name FROM call_hierarchy_anchors ORDER BY name'):
            name = row[0]
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                (stable_id({'feature': FEATURE, 'subject': name}), FEATURE, name))


def relative(file, root):
    if not isinstance(file, str):
        raise UnsupportedHierarchy('call hierarchy location lacks a path')
    path = Path(file)
    path = path if path.is_absolute() else root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise UnsupportedHierarchy('call hierarchy project scope escaped target root')
    return path.relative_to(root).as_posix()


def owner(fixture, node):
    path = relative(node.get('file'), fixture.root)
    if not path.endswith('.java'):
        return None
    if not isinstance(node.get('line'), int) or isinstance(node['line'], bool) or node['line'] < 1:
        raise UnsupportedHierarchy('invalid MCP caller declaration line')
    entries = fixture.structure(path)
    candidates = [entry for entry in entries if entry['kind'] in CALLABLE_KINDS
                  and entry['line'] == node['line']]
    identities = {(path, entry['line'], entry['name']) for entry in candidates}
    if len(identities) != 1:
        # The public name/line CLI deliberately unions same-name overloads;
        # distinct methods sharing a line still need an exact column anchor.
        identities = {(path, entry['line'], entry['name']) for entry in candidates
                      if entry['column'] == node.get('column')}
    if len(identities) != 1:
        raise UnsupportedHierarchy('MCP caller owner has missing or ambiguous source declaration anchor')
    return next(iter(identities))


def native_callers(fixture, check):
    """Bound stdout on disk and in memory; never print project data to session."""
    directory = fixture.database.parent / 'call-hierarchy'
    directory.mkdir(exist_ok=True)
    output = directory / (check['id'] + '.stdout.log')
    with output.open('wb') as stdout, output.with_suffix('.stderr.log').open('wb') as stderr:
        completed = subprocess.run([str(fixture.binary), 'call-tree', check['subject'],
            '--depth', '1', '--limit', str(MAX_CALLERS + 1), '--in-file', '.java'],
            cwd=fixture.root, env=fixture.environment, stdout=stdout, stderr=stderr, timeout=120)
    if completed.returncode:
        raise ToolError('native call hierarchy failed; inspect private command logs')
    actual = set()
    with output.open(encoding='utf-8') as stream:
        header = stream.readline(1024 * 1024)
        seed = stream.readline(1024 * 1024)
        if header != f"Call tree for '{check['subject']}':\n" or seed != f"  {check['subject']}\n":
            raise UnsupportedHierarchy('unrecognized native call hierarchy header')
        for count in range(MAX_CALLERS + 1):
            line = stream.readline(1024 * 1024)
            if not line:
                break
            if count == MAX_CALLERS or not line.endswith('\n'):
                raise UnsupportedHierarchy('native call hierarchy exceeded bounded direct-caller contract')
            match = re.fullmatch(r'    ← (.+) \((.+):(\d+)\)\n', line)
            if match is None:
                raise UnsupportedHierarchy('unrecognized native direct-caller row')
            path = relative(match[2], fixture.root)
            if path.endswith('.java'):
                actual.add((path, int(match[3]), match[1]))
    return actual


def accessor_response(fixture, check, anchor, page):
    """An implicit getter has no method declaration token in source.

    Resolve a real source usage through MCP, and accept only its exact
    component anchor. A missing anchor remains unsupported, never empty truth.
    """
    positions = fixture.state.execute('''SELECT path,line,column FROM call_hierarchy_usage_anchors
        WHERE name=? ORDER BY path,line,column''', (anchor['name'],))
    for count, (path, line, column) in enumerate(positions):
        if count >= MAX_CALLERS:
            raise UnsupportedHierarchy('implicit accessor alias search exceeded bounded contract')
        request = dict(project_path=str(fixture.root), file=path, line=line, column=column,
                       direction='callers', depth=1, scope='project_files', includeGenerated=True)
        try:
            response = fixture.client.call('ide_call_hierarchy', request)
        except McpRemoteError:
            # InvocationOracle durably captures the remote failure. An error
            # is not a negative result; only a successful exact anchor binds.
            continue
        fixture.oracle_store.page(check['id'], page, 'ide_call_hierarchy', request, response)
        page += 1
        selected = response.get('element', {}) if isinstance(response, dict) else {}
        selected_file = selected.get('file')
        if not isinstance(selected_file, str):
            raise UnsupportedHierarchy('MCP accessor alias response lacks a declaration path')
        selected_path = Path(selected_file)
        selected_path = (selected_path if selected_path.is_absolute() else fixture.root / selected_path).resolve()
        if not selected_path.is_relative_to(fixture.root):
            continue
        if selected_path.relative_to(fixture.root).as_posix() == anchor['path'] \
                and selected.get('line') == anchor['line'] \
                and selected.get('column') == anchor['column']:
            return response, page
    raise UnsupportedHierarchy('implicit accessor has no successful exact MCP usage anchor')


def exercise(fixture, check):
    if not getattr(fixture, '_call_hierarchy_graph_ready', False):
        fixture.cli('graph', 'build')
        fixture._call_hierarchy_graph_ready = True
    expected, queries, declarations = set(), 0, 0
    for anchor in fixture.state.execute(
        'SELECT * FROM call_hierarchy_anchors WHERE name=? ORDER BY path,line,column,kind',
        (check['subject'],)):
        request = dict(project_path=str(fixture.root), file=anchor['path'], line=anchor['line'],
                       column=anchor['column'], direction='callers', depth=1,
                       scope='project_files', includeGenerated=anchor['kind'] == 'accessor')
        if anchor['kind'] == 'accessor':
            response, queries = accessor_response(fixture, check, anchor, queries)
        else:
            response = fixture.client.call('ide_call_hierarchy', request)
            fixture.oracle_store.page(check['id'], queries, 'ide_call_hierarchy', request, response)
            queries += 1
        declarations += 1
        if not isinstance(response, dict) or not isinstance(response.get('calls'), list) \
                or not isinstance(response.get('element'), dict):
            raise UnsupportedHierarchy('unrecognized MCP call hierarchy response')
        if any(response.get(flag) for flag in ('stale', 'truncated', 'hasMore', 'nextCursor', 'incomplete')):
            raise UnsupportedHierarchy('MCP call hierarchy response is incomplete')
        selected = response['element']
        if relative(selected.get('file'), fixture.root) != anchor['path'] or selected.get('line') != anchor['line']:
            raise UnsupportedHierarchy('MCP selected a different call hierarchy declaration')
        if len(response['calls']) > MAX_CALLERS:
            raise UnsupportedHierarchy('MCP direct callers exceed bounded contract')
        for node in response['calls']:
            if not isinstance(node, dict) or node.get('children'):
                raise UnsupportedHierarchy('MCP direct caller node has unknown/deeper shape')
            identity = owner(fixture, node)
            if identity is not None:
                expected.add(identity)
            if len(expected) > MAX_CALLERS:
                raise UnsupportedHierarchy('MCP caller union exceeds bounded contract')
    if not declarations:
        raise UnsupportedHierarchy('call hierarchy check has no independently scheduled declarations')
    actual = native_callers(fixture, check)
    return {'source': REASON, 'queries': queries, 'declarations': declarations, 'items': sorted(expected)}, \
           {'items': sorted(actual)}, expected, actual
