"""Direct Java graph owner edges projected from completed live MCP evidence.

Graph hides edges internal to its selected subjects, unlike call hierarchy.
Initializer owners are properties/constants, not necessarily functions.
Neither native index rows nor graph edges supply oracle expectations.
"""
import json
from collections import Counter
from pathlib import Path
import subprocess
import time

from common import ToolError, stable_id
from call_hierarchy_contracts import FEATURE as CALLERS, MAX_CALLERS

FEATURE = 'graph:mcp-direct-callers'
FEATURES = {FEATURE}
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
REASON = ('live MCP direct Java caller-owner expectations reused from the same audit; '
          'exact independently planned callable seeds, Java scope and graph internal-subject '
          'projection; includes initializer owners; not deep traversal/type-edge/attached-root equivalence')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS graph_mcp_sources(
 subject TEXT PRIMARY KEY,id TEXT NOT NULL,status TEXT NOT NULL,
 verdict TEXT,expected_json TEXT
);
'''


class ScopeUnsupported(ToolError):
    pass


def native_dependents(fixture, check):
    """Keep project payload on disk; never capture unbounded stdout in memory."""
    directory = fixture.database.parent / 'graph-mcp'
    directory.mkdir(exist_ok=True)
    case_id = stable_id({'feature': FEATURE, 'subject': check['subject']})
    output = directory / (case_id + '.stdout.json')
    started = time.perf_counter()
    try:
        with output.open('wb') as stdout, (directory / (case_id + '.stderr.log')).open('wb') as stderr:
            result = subprocess.run([str(fixture.binary), '--format', 'json', 'graph', 'dependents',
                check['subject'], '--kind', 'function', '--in-file', '.java', '--include-ambiguous',
                '--limit', str(MAX_CALLERS + 1)], cwd=fixture.root, env=fixture.environment,
                stdout=stdout, stderr=stderr, timeout=120)
        if result.returncode:
            raise ToolError('native graph comparison failed; see private command logs')
        with output.open('rb') as stream:
            payload = stream.read(MAX_OUTPUT_BYTES + 1)
        if len(payload) > MAX_OUTPUT_BYTES:
            raise ScopeUnsupported('graph response exceeds bounded-memory output cap')
        try:
            return json.loads(payload)
        except ValueError as error:
            raise ToolError('native graph response is not JSON; see private command logs') from error
    finally:
        fixture.metrics.record('cli.graph', time.perf_counter() - started)


def plan(state):
    """One resumable graph check per independently scheduled callable name."""
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        for row in state.execute('SELECT DISTINCT name FROM call_hierarchy_anchors ORDER BY name'):
            name = row[0]
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': name}), FEATURE, name))


def identity(value, root):
    if (not isinstance(value, (tuple, list)) or len(value) != 3
            or not isinstance(value[0], str) or type(value[1]) is not int
            or value[1] < 1 or not isinstance(value[2], str)):
        raise ToolError('malformed graph/MCP owner identity')
    path = Path(value[0])
    path = path if path.is_absolute() else root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ScopeUnsupported('graph/MCP owner escaped the target root')
    return path.relative_to(root).as_posix(), value[1], value[2]


def exercise(fixture, check):
    source = fixture.state.execute('SELECT * FROM checks WHERE feature=? AND subject=?',
                                   (CALLERS, check['subject'])).fetchone()
    if source is None:
        source = fixture.state.execute('SELECT * FROM graph_mcp_sources WHERE subject=?',
                                       (check['subject'],)).fetchone()
    if source is None or source['status'] != 'complete' or source['verdict'] not in {'pass', 'fail'}:
        raise ScopeUnsupported('completed live MCP caller evidence is required before graph comparison')
    truth = json.loads(source['expected_json'] or 'null')
    if (not isinstance(truth, dict) or not isinstance(truth.get('items'), list)
            or not str(truth.get('source', '')).startswith('live MCP')):
        raise ToolError('graph expected owners must come from live MCP evidence')
    if len(truth['items']) > MAX_CALLERS:
        raise ScopeUnsupported('MCP caller owner cap exceeded')
    root = fixture.root.resolve()
    owners = {identity(item, root) for item in truth['items']}
    seeds = Counter()
    seed_total = 0
    for row in fixture.state.execute('SELECT path,line,name FROM call_hierarchy_anchors WHERE name=?',
                                     (check['subject'],)):
        seeds[identity(tuple(row), root)] += 1
        seed_total += 1
        if seed_total > MAX_CALLERS:
            raise ScopeUnsupported('independent callable seed cap exceeded')
    if not seeds:
        raise ScopeUnsupported('graph comparison has no independent callable anchors')
    if not getattr(fixture, '_call_hierarchy_graph_ready', False):
        fixture.cli('graph', 'build')
        fixture._call_hierarchy_graph_ready = True
    doc = native_dependents(fixture, check)
    if not isinstance(doc, dict) or doc.get('error'):
        raise ScopeUnsupported('graph did not select the independently planned callable scope')
    if not isinstance(doc.get('matched'), list) or not isinstance(doc.get('items'), list):
        raise ToolError('malformed graph dependent response')
    if len(doc['matched']) > MAX_CALLERS:
        raise ScopeUnsupported('native callable seed cap exceeded')
    matched = Counter()
    for item in doc['matched']:
        if not isinstance(item, dict) or item.get('kind') != 'function':
            raise ScopeUnsupported('graph selected a non-callable entity')
        matched[identity([item.get(key) for key in ('path', 'line', 'name')], root)] += 1
    if matched != seeds:
        raise ScopeUnsupported('graph and MCP callable seed identities differ')
    pagination = doc.get('pagination', {})
    if (not isinstance(pagination, dict) or type(pagination.get('total')) is not int
            or pagination['total'] < 0):
        raise ToolError('graph pagination total is missing or malformed')
    if (len(doc['items']) > MAX_CALLERS or pagination['total'] > MAX_CALLERS
            or pagination.get('has_more') or pagination.get('hasMore')):
        raise ScopeUnsupported('graph direct-owner result cap exceeded')
    if pagination['total'] != len(doc['items']):
        raise ScopeUnsupported('graph direct-owner page is incomplete')
    actual = set()
    for item in doc['items']:
        if not isinstance(item, dict) or not isinstance(item.get('other'), dict):
            raise ToolError('malformed graph dependent owner')
        owner = identity([item['other'].get(key) for key in ('path', 'line', 'name')], root)
        if owner[0].endswith('.java'):
            actual.add(owner)
    # Native graph's public contract excludes ALL edges internal to subjects,
    # not only recursion. Keep raw oracle owners and seeds as durable evidence.
    expected = owners - set(seeds)
    return {'source': REASON, 'source_case_id': source['id'], 'oracle_owners': sorted(owners),
            'selected_seeds': sorted(seeds), 'items': sorted(expected)}, \
           {'items': sorted(actual)}, expected, actual
