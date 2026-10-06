"""Two-level Java call trees from completed position-bound MCP caller truth.

Name unions are valid for the root CLI query, not a particular child method.
Ambiguous/non-callable child anchors remain explicitly unsupported until their
position-bound oracle dependencies are available; never borrow a name union.
"""
from collections import Counter
import json
from pathlib import Path
import subprocess
import time

from common import ToolError, stable_id
import call_hierarchy_contracts as callers
from graph_mcp_contracts import ScopeUnsupported, identity

FEATURE = 'call-tree:mcp-traversal-depth2'
FEATURES = {FEATURE}
REASON = ('live MCP two-level Java caller owners; unique child name truth or '
          'position-bound callable hierarchy/references, recursion markers and '
          'complete bounded JSON; independent JDK noncallable initializer terminals; '
          'same-line ambiguous owners remain unsupported, not compiler-wide '
          'dispatch or attached-root equivalence')
MAX_ITEMS = 100000
MAX_BYTES = 32 * 1024 * 1024


def plan(state):
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      (FEATURE, 'implemented', REASON))
        for row in state.execute('SELECT DISTINCT name FROM call_hierarchy_anchors ORDER BY name'):
            subject = row[0]
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))


def source_row(state, subject):
    row = state.execute('SELECT id,status,verdict,expected_json FROM checks WHERE feature=? AND subject=?',
                        (callers.FEATURE, subject)).fetchone()
    if row is None and state.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_mcp_sources'").fetchone():
        row = state.execute('SELECT id,status,verdict,expected_json FROM graph_mcp_sources WHERE subject=?',
                            (subject,)).fetchone()
    return row


def owners(fixture, subject):
    row = source_row(fixture.state, subject)
    if row is None or row['status'] != 'complete' or row['verdict'] not in {'pass', 'fail'}:
        raise ScopeUnsupported('completed live MCP child/root caller evidence is required')
    doc = json.loads(row['expected_json'] or 'null')
    if (not isinstance(doc, dict) or not isinstance(doc.get('items'), list)
            or not str(doc.get('source', '')).startswith('live MCP')):
        raise ToolError('two-level caller expectations must come from live MCP evidence')
    if len(doc['items']) > MAX_ITEMS:
        raise ScopeUnsupported('MCP caller owner budget exceeded')
    locations = {identity(value, fixture.root.resolve()) for value in doc['items']}
    return {location for location in locations if location[0].endswith('.java')}


def expectations(fixture, check):
    subject = check['subject']
    root_owners = owners(fixture, subject)
    expected = []
    dependencies = {subject}
    for caller in sorted(root_owners):
        path, line, name = caller
        # A single independent source declaration makes its name union exact.
        # Otherwise its own position-bound truth is required, not another
        # declaration's callers. Bound the ambiguity probe to two rows.
        anchors = fixture.state.execute('SELECT path,line,name FROM call_hierarchy_anchors '
                                        'WHERE name=? LIMIT 2', (name,)).fetchall()
        if any(not (fixture.root / anchor[0]).is_file() for anchor in anchors):
            raise ScopeUnsupported('child callable inventory contains a missing source anchor')
        exact = fixture.state.execute('SELECT * FROM call_hierarchy_anchors '
                                      'WHERE path=? AND line=? AND name=? LIMIT 2',
                                      caller).fetchall()
        terminals = [entry for entry in fixture.structure(path)
                     if entry['kind'] in {'property', 'constant'}
                     and entry['line'] == line and entry['name'] == name]
        if exact and terminals:
            raise ScopeUnsupported('same-line callable and initializer owner identities collide')
        if not exact and len(terminals) == 1:
            expected.append((1, *caller, 'shown'))
            continue
        if len(exact) != 1:
            raise ScopeUnsupported('position-bound MCP truth is required for ambiguous/noncallable child')
        if name == subject:
            # The initial name query has already expanded this root target.
            expected.append((1, *caller, 'expanded_above'))
            continue
        expected.append((1, *caller, 'shown'))
        if len(anchors) == 1 and tuple(anchors[0]) == caller:
            dependencies.add(name)
            child_owners = owners(fixture, name)
        else:
            try:
                child_owners, _ = callers.anchor_owners(fixture, check, exact[0], exact_member=True)
            except callers.UnsupportedHierarchy as error:
                raise ScopeUnsupported(str(error)) from error
        for child in sorted(child_owners):
            expected.append((2, *child, 'recursive' if child == caller else 'shown'))
            if len(expected) > MAX_ITEMS:
                raise ScopeUnsupported('two-level expected tree exceeds bounded item budget')
    if len(expected) > MAX_ITEMS:
        raise ScopeUnsupported('two-level expected tree exceeds bounded item budget')
    return sorted(expected), sorted(dependencies)


def native_tree(fixture, check):
    directory = fixture.database.parent / 'call-tree-mcp-depth2'
    directory.mkdir(exist_ok=True)
    output = directory / (check['id'] + '.stdout.json')
    started = time.perf_counter()
    try:
        with output.open('wb') as stdout, output.with_suffix('.stderr.log').open('wb') as stderr:
            process = subprocess.run([str(fixture.binary), '--format', 'json', 'call-tree', check['subject'],
                '--depth', '2', '--limit', str(MAX_ITEMS + 1), '--in-file', '.java'],
                cwd=fixture.root, env=fixture.environment, stdout=stdout, stderr=stderr, timeout=120)
        if process.returncode:
            raise ToolError('native two-level call tree failed; inspect private command logs')
        with output.open('rb') as stream:
            payload = stream.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ScopeUnsupported('native two-level tree exceeds bounded-memory output budget')
        try:
            return json.loads(payload)
        except ValueError as error:
            raise ToolError('native two-level call tree is not JSON') from error
    finally:
        fixture.metrics.record('cli.call-tree.depth2', time.perf_counter() - started)


def exercise(fixture, check):
    expected, dependencies = expectations(fixture, check)
    if not getattr(fixture, '_call_hierarchy_graph_ready', False):
        fixture.cli('graph', 'build')
        fixture._call_hierarchy_graph_ready = True
    doc = native_tree(fixture, check)
    if (not isinstance(doc, dict) or doc.get('function') != check['subject']
            or doc.get('max_depth') != 2 or doc.get('limit_per_level') != MAX_ITEMS + 1
            or not isinstance(doc.get('items'), list) or type(doc.get('count')) is not int):
        raise ToolError('malformed native two-level call tree metadata')
    if len(doc['items']) > MAX_ITEMS:
        raise ScopeUnsupported('native two-level call tree exceeds item budget')
    if doc['count'] != len(doc['items']):
        raise ScopeUnsupported('native two-level call tree page is incomplete')
    actual = []
    java_branch = False
    for row in doc['items']:
        if (not isinstance(row, dict) or type(row.get('depth')) is not int
                or row['depth'] not in (1, 2)
                or row.get('status') not in ('shown', 'recursive', 'expanded_above')):
            raise ToolError('malformed native two-level call tree row')
        location = identity([row.get(key) for key in ('path', 'line', 'name')], fixture.root.resolve())
        if row['depth'] == 1:
            java_branch = location[0].endswith('.java')
        if not location[0].endswith('.java'):
            continue
        if not java_branch:
            continue
        actual.append((row['depth'], *location, row['status']))
    actual.sort()
    # Counters preserve repeated owners reached through different branches.
    want, got = Counter(expected), Counter(actual)
    return {'source': REASON, 'dependencies': dependencies, 'items': expected}, \
           {'items': actual}, want, got


def copy_dependencies(source, target, subject):
    """Replay only the selected root's first-hop truth, not extra test checks."""
    root = source_row(source, subject)
    if root is None:
        return
    subjects = {subject}
    if root['status'] == 'complete' and root['verdict'] in {'pass', 'fail'}:
        doc = json.loads(root['expected_json'] or 'null')
        if isinstance(doc, dict) and isinstance(doc.get('items'), list):
            if len(doc['items']) > MAX_ITEMS:
                raise ScopeUnsupported('replay caller dependency budget exceeded')
            for owner in doc['items']:
                if not isinstance(owner, list) or len(owner) != 3 or not isinstance(owner[2], str):
                    raise ToolError('malformed replay caller dependency identity')
                subjects.add(owner[2])
    with target:
        for name in sorted(subjects):
            row = source_row(source, name)
            if row is not None:
                target.execute('INSERT OR REPLACE INTO graph_mcp_sources VALUES (?,?,?,?,?)',
                               (name, *tuple(row)))
