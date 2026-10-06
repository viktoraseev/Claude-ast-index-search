"""Direct Java method-owner callers against position-bound live MCP evidence.

The name-only CLI unions same-name declarations, so the oracle must query
every source declaration with that name, not choose its first overload.
Source anchors schedule queries and normalize owner identities; native rows
never supply expected edges. Deep traversal has separate pending contracts.
"""
from pathlib import Path
import json
import re
import subprocess

from common import McpRemoteError, ToolError, stable_id

FEATURE = 'call-tree:mcp-direct-callers'
FEATURES = {FEATURE}
REASON = ('live MCP direct Java caller owners; union of same-name JDK source method/constructor/accessor '
          'anchors; exact selected-member references normalize overridden-method families and generated accessors; '
          'supplement field-initializer and recursive owners omitted by hierarchy views; '
          'not virtual-family, deep traversal or attached-root equivalence')
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


def plan_methods(state, root, source_files, structure, *, schedule_checks=True):
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
        if not schedule_checks:
            return
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
    fixture._accessor_reference_anchors = {}
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
            fixture._accessor_reference_anchors = {
                (anchor['name'], anchor['path'], anchor['line'], anchor['column']): (path, line, column)}
            return response, page
    raise UnsupportedHierarchy('implicit accessor has no successful exact MCP usage anchor')


def callable_reference_owners(fixture, check, anchor):
    """Exact MCP selected-callable references normalize hierarchy scope and omissions.
    Bind the selected declaration, exhaust every page, and classify invocations
    and their owners independently with JDK syntax.
    """
    constructor = anchor['kind'] == 'constructor'
    method = anchor['kind'] == 'method'
    usage_anchor = getattr(fixture, '_accessor_reference_anchors', {}).get(
        (anchor['name'], anchor['path'], anchor['line'], anchor['column']))
    reference_path, reference_line, reference_column = usage_anchor or (anchor['path'], anchor['line'], anchor['column'])
    syntax_kinds = {'constructor_call', 'constructor_reference'} if constructor else {'call', 'method_reference'}
    resolved_kinds = ({'constructor'} if constructor else {'method'} if method else
                      {'method', 'record component'} if usage_anchor else {'record component'})
    request = dict(project_path=str(fixture.root), file=reference_path, line=reference_line,
                   column=reference_column, scope='project_files', includeGenerated=not (constructor or method), pageSize=500)
    def reference_pages():
        arguments, cursors, count = request, set(), 0
        page = fixture.state.execute('SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?',
                                     (check['id'],)).fetchone()[0]
        while True:
            response = fixture.client.call('ide_find_references', arguments)
            fixture.oracle_store.page(check['id'], page, 'ide_find_references', arguments, response)
            page += 1
            if not isinstance(response, dict) or any(response.get(flag) for flag in ('stale', 'truncated', 'incomplete')):
                raise UnsupportedHierarchy('incomplete MCP component references')
            if arguments is request:
                resolved = response.get('resolvedSymbol', {})
                if not isinstance(resolved, dict) or resolved.get('kind') not in resolved_kinds \
                        or resolved.get('name') != anchor['name'] \
                        or relative(resolved.get('file'), fixture.root) != anchor['path'] \
                        or resolved.get('line') != anchor['line']:
                    raise UnsupportedHierarchy('accessor references resolved a different component')
            usages = response.get('usages', response.get('references'))
            if not isinstance(usages, list) or any(not isinstance(usage, dict) for usage in usages):
                raise UnsupportedHierarchy('unknown MCP component reference shape')
            count += len(usages)
            if count > MAX_CALLERS:
                raise UnsupportedHierarchy('component references exceed bounded contract')
            yield from usages
            cursor = response.get('nextCursor')
            if response.get('hasMore') and not cursor:
                raise UnsupportedHierarchy('component references require a missing cursor')
            if not cursor:
                if response.get('totalIsExact') is not True:
                    raise UnsupportedHierarchy('component reference total is not exact')
                return
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= MAX_CALLERS:
                raise UnsupportedHierarchy('component reference pagination is invalid or unbounded')
            cursors.add(cursor)
            arguments = dict(project_path=str(fixture.root), pageSize=500, cursor=cursor)
    owners = set()
    for usage in reference_pages():
        reference_type = usage.get('type')
        path = relative(usage.get('file'), fixture.root)
        if not path.endswith('.java'):
            continue
        entries = fixture.structure(path)
        if reference_type == 'REFERENCE':
            reference_aliases = {entry.get('usage_kind') for entry in entries if entry['kind'] == 'usage'
                                 and entry.get('usage_kind') == 'method_reference' and entry['name'] == anchor['name']
                                 and entry.get('reference_line') == usage.get('line')
                                 and entry.get('reference_column') == usage.get('column')}
            positional_syntax = {entry.get('usage_kind') for entry in entries if entry['kind'] == 'usage'
                                 and entry['line'] == usage.get('line') and entry['column'] == usage.get('column')}
            # Component searches may include a constructor argument or pattern
            # binding whose local name differs from the component's name.
            if positional_syntax == {'value'} and not reference_aliases:
                continue
            syntax = {entry.get('usage_kind') for entry in entries if entry['kind'] == 'usage'
                      and (constructor or entry['name'] == anchor['name']) and entry['line'] == usage.get('line')
                      and entry['column'] == usage.get('column')}
            if reference_aliases:
                syntax = reference_aliases
            if not syntax or not syntax <= syntax_kinds:
                raise UnsupportedHierarchy('component reference lacks exact independent syntax classification')
        elif reference_type not in {'METHOD_CALL', 'METHOD_REFERENCE'}:
            # Component field reads do not call the generated getter.
            if usage.get('type') in {'READ', 'WRITE', 'READ_WRITE'}:
                continue
            raise UnsupportedHierarchy('unknown component reference semantics')
        line = usage.get('line')
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise UnsupportedHierarchy('invalid component usage line')
        candidates = [entry for entry in entries
                      if entry['kind'] in CALLABLE_KINDS and entry['line'] <= line <= entry['end_line']]
        if not candidates:
            candidates = [entry for entry in entries if entry['kind'] in {'property', 'constant'}
                          and entry['line'] <= line <= entry['end_line']]
        if not candidates:
            raise UnsupportedHierarchy('accessor reference has no callable source owner')
        span = min(entry['end_line'] - entry['line'] for entry in candidates)
        identities = {(path, entry['line'], entry['name']) for entry in candidates
                      if entry['end_line'] - entry['line'] == span}
        if len(identities) != 1:
            raise UnsupportedHierarchy('accessor reference has ambiguous callable source owner')
        owners.update(identities)
        if len(owners) > MAX_CALLERS:
            raise UnsupportedHierarchy('selected-callable owner union exceeds bounded contract')
    return owners


def hierarchy_omits_source_owners(fixture, anchor):
    """Schedule exact references for field initializers and self-recursion.

    Hierarchy views can omit these owners. Independent syntax, rather than
    native edges or a failed comparison, determines the supplemental query.
    """
    positions = fixture.state.execute('''SELECT DISTINCT path,line FROM call_hierarchy_usage_anchors
        WHERE name=? ORDER BY path,line''', (anchor['name'],))
    for count, (path, line) in enumerate(positions):
        if count >= MAX_CALLERS:
            raise UnsupportedHierarchy('hierarchy owner inventory exceeded bounded contract')
        entries = fixture.structure(path)
        if any(entry['kind'] in {'property', 'constant'}
               and entry['line'] <= line <= entry['end_line'] for entry in entries):
            return True
        if path == anchor['path'] and any(entry['kind'] == 'method'
                and entry['line'] == anchor['line'] and entry['name'] == anchor['name']
                and entry['line'] <= line <= entry['end_line'] for entry in entries):
            return True
    return False


def anchor_owners(fixture, check, anchor, *, exact_member=False):
    """Bind one callable declaration; never union another same-name member."""
    expected = set()
    queries = fixture.state.execute(
        'SELECT coalesce(max(page)+1,0) FROM pages WHERE check_id=?',
        (check['id'],)).fetchone()[0]
    request = dict(project_path=str(fixture.root), file=anchor['path'], line=anchor['line'],
                   column=anchor['column'], direction='callers', depth=1,
                   scope='project_files', includeGenerated=anchor['kind'] == 'accessor')
    if anchor['kind'] == 'accessor':
        try:
            response, queries = accessor_response(fixture, check, anchor, queries)
        except UnsupportedHierarchy:
            expected.update(callable_reference_owners(fixture, check, anchor))
            if len(expected) > MAX_CALLERS:
                raise UnsupportedHierarchy('MCP caller union exceeds bounded contract')
            return expected, False
    else:
        response = fixture.client.call('ide_call_hierarchy', request)
        fixture.oracle_store.page(check['id'], queries, 'ide_call_hierarchy', request, response)
        queries += 1
    if not isinstance(response, dict) or not isinstance(response.get('calls'), list) \
            or not isinstance(response.get('element'), dict):
        raise UnsupportedHierarchy('unrecognized MCP call hierarchy response')
    if any(response.get(flag) for flag in ('stale', 'truncated', 'hasMore', 'nextCursor', 'incomplete')):
        raise UnsupportedHierarchy('MCP call hierarchy response is incomplete')
    selected = response['element']
    if relative(selected.get('file'), fixture.root) != anchor['path'] or selected.get('line') != anchor['line']:
        raise UnsupportedHierarchy('MCP selected a different call hierarchy declaration')
    same_line = fixture.state.execute('SELECT column FROM call_hierarchy_anchors '
                                     'WHERE path=? AND line=? AND name=? LIMIT 2',
                                     (anchor['path'], anchor['line'], anchor['name'])).fetchall()
    if len(same_line) > 1 and selected.get('column') != anchor['column']:
        raise UnsupportedHierarchy('same-line callable selection requires exact MCP column')
    if len(response['calls']) > MAX_CALLERS:
        raise UnsupportedHierarchy('MCP direct callers exceed bounded contract')
    narrow_override = anchor['kind'] == 'method' and any(
        entry['kind'] == 'method' and entry['name'] == anchor['name']
        and entry['line'] == anchor['line'] and entry['column'] == anchor['column']
        and entry.get('overrides') is True for entry in fixture.structure(anchor['path']))
    for node in response['calls']:
        if not isinstance(node, dict) or node.get('children'):
            raise UnsupportedHierarchy('MCP direct caller node has unknown/deeper shape')
        identity = owner(fixture, node)
        if identity is not None and not (narrow_override or exact_member):
            expected.add(identity)
        if len(expected) > MAX_CALLERS:
            raise UnsupportedHierarchy('MCP caller union exceeds bounded contract')
    if anchor['kind'] in {'constructor', 'accessor'} or narrow_override or exact_member \
            or hierarchy_omits_source_owners(fixture, anchor):
        expected.update(callable_reference_owners(fixture, check, anchor))
        if len(expected) > MAX_CALLERS:
            raise UnsupportedHierarchy('MCP caller union exceeds bounded contract')
        queries = fixture.state.execute('SELECT count(*) FROM pages WHERE check_id=?',
                                        (check['id'],)).fetchone()[0]
    return expected, narrow_override


def exercise(fixture, check):
    if not getattr(fixture, '_call_hierarchy_graph_ready', False):
        fixture.cli('graph', 'build')
        fixture._call_hierarchy_graph_ready = True
    expected, queries, declarations = set(), 0, 0
    narrowed_overrides = 0
    for anchor in fixture.state.execute(
        'SELECT * FROM call_hierarchy_anchors WHERE name=? ORDER BY path,line,column,kind',
        (check['subject'],)):
        owners, narrow_override = anchor_owners(fixture, check, anchor)
        expected.update(owners)
        if len(expected) > MAX_CALLERS:
            raise UnsupportedHierarchy('MCP caller union exceeds bounded contract')
        declarations += 1
        narrowed_overrides += int(narrow_override)
    queries = fixture.state.execute(
        'SELECT count(*) FROM pages WHERE check_id=?', (check['id'],)).fetchone()[0]
    if not declarations:
        raise UnsupportedHierarchy('call hierarchy check has no independently scheduled declarations')
    actual = native_callers(fixture, check)
    reference_count = fixture.state.execute("SELECT count(*) FROM pages WHERE check_id=? AND tool='ide_find_references'",
                                            (check['id'],)).fetchone()[0]
    source = REASON if not reference_count else (REASON + '; MCP selected-callable '
        'reference locations with independent JDK invocation classification and callable ownership; '
        'supplements hierarchy, not call-hierarchy-only equivalence')
    if narrowed_overrides:
        source += '; overridden-method family hierarchy normalized to exact MCP selected-member references; not virtual-dispatch family equivalence'
    return {'source': source, 'queries': queries, 'declarations': declarations, 'items': sorted(expected)}, \
           {'items': sorted(actual)}, expected, actual
