"""Java caller selectors against authored sources, not native DB or MCP truth.

Both line callers and owner trees consume the same root/path selection. Graph
freshness must not change that selection. Semantic dispatch remains separate.
"""
from collections import Counter
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'global:scope:java-callers', 'global:scope:java-call-tree'}
REASON = ('independent source/state: disposable Java caller lines and owner trees, '
          'literal file/cwd selectors, colliding attached roots, scope before limits '
          'and fresh/unbuilt graph paths; not MCP equivalence or semantic dispatch')
GAP = ('Java file views, navigation and caller/call-tree literal file/cwd/root '
       'selectors have separate executed contracts; module/map/analysis/graph/'
       'conventions/explore scope remains unresolved')
SOURCE = '''package fixture.{package};
class Probe {{
    void ping() {{}}
    void use() {{ ping(); }}
    void upper() {{ use(); }}
}}
'''


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-caller-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      (GAP,))


def tree_rows(output, runner):
    rows = []
    for line in output.splitlines():
        if '←' not in line:
            continue
        match = re.fullmatch(r'( +)← (\w+) \((.+):(\d+)\)(?: \(expanded above\))?', line)
        if not match or len(match[1]) % 2:
            raise ToolError('malformed caller tree; see private fixture logs')
        rows.append((len(match[1]) // 2 - 1, runner.path(match[3]), int(match[4]), match[2]))
    return rows


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('caller scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='caller-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    relative = ['src/scope_/Probe_.java', 'src/scopeX/ProbeX.java',
                'src/scope%/Probe%.java', 'src/scope_/Probe\\.java']
    paths = []
    for owner in ('project', 'attached'):
        for number, path in enumerate(relative):
            destination = directory / owner / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(SOURCE.format(package=f'{owner}.p{number}'))
            paths.append(f'{owner}/{path}')
        # Inventory includes foreign and descriptor types; no parser repair or
        # foreign absence claim is inferred from these authored Java sources.
        (directory / owner / 'Inventory.kt').write_text('// inventory marker\n')
        (directory / owner / 'descriptor.xml').write_text('<fixture/>\n')
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force')
    runner.json('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    inventory = connect(directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        for owner in ('project', 'attached'):
            mobile_contracts.inventory(inventory, directory / owner)
            rows = dict(inventory.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' "
                                          "AND extension IN ('.java','.kt','.xml') GROUP BY extension"))
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, {'.java': 4, '.kt': 1, '.xml': 1}, rows)
    finally:
        inventory.close()

    scopes = [('all', [], {'project', 'attached'}),
              ('local', ['--local'], {'project'}),
              ('attached', ['--subtree', 'attached-label'], {'attached'}),
              ('absent', ['--subtree', 'absent'], set())]
    selectors = [('empty', '', None), ('underscore', 'Probe_', None),
                 ('percent', '%', None), ('backslash', '\\', None),
                 ('case', 'probe_', None), ('root-name', 'attached-label', None),
                 ('artifact-name', directory.name, None),
                 ('cwd', 'Probe', 'src/scope_'), ('cwd-disjoint', 'ProbeX', 'src/scope_')]

    def page(rows, candidates, limit):
        rows, candidates = Counter(rows), Counter(candidates)
        return {'valid': not bool(rows - candidates), 'returned': rows.total(),
                'complete': limit < candidates.total() or rows == candidates}

    for mode in ('unbuilt', 'fresh'):
        if mode == 'fresh':
            runner.json('graph', 'build')
        for scope_name, flags, owners in scopes:
            for label, file_filter, cwd_prefix in selectors:
                cwd = runner.root / cwd_prefix if cwd_prefix else runner.root
                candidates = [path for path in paths if path.split('/', 1)[0] in owners
                              and file_filter in path.split('/', 1)[1]
                              and (cwd_prefix is None or path.split('/', 1)[1].startswith(cwd_prefix + '/'))]
                for limit in (0, 1, 100):
                    key = f'{mode}:{scope_name}:{label}:{limit}'
                    filters = ['--in-file', file_filter, '--limit', limit]
                    doc = runner.json(*flags, 'callers', 'ping', *filters, cwd=cwd)
                    rows = [(runner.path(row['path']), row['line']) for row in doc['items']]
                    want = {'valid': True, 'returned': min(limit, len(candidates)), 'complete': True,
                            'pagination': {'total': len(candidates), 'returned': min(limit, len(candidates)),
                                           'limit': limit, 'truncated': limit < len(candidates)}}
                    got = page(rows, [(path, 4) for path in candidates], limit)
                    got['pagination'] = doc['pagination']
                    record('global:scope:java-callers', key, want, got)
                    _, text = runner.command(*flags, 'call-tree', 'ping', '--depth', '2', *filters, cwd=cwd)
                    rows = tree_rows(text, runner)
                    for depth, line, name in ((1, 4, 'use'), (2, 5, 'upper')):
                        record('global:scope:java-call-tree', key + ':' + str(depth),
                               {'valid': True, 'returned': min(limit, len(candidates)), 'complete': True},
                               page([row[1:] for row in rows if row[0] == depth],
                                    [(path, line, name) for path in candidates], limit))
                    record('global:scope:java-call-tree', key + ':depths', True,
                           all(row[0] in (1, 2) for row in rows))
                # Text callers must apply the same selectors as JSON, including
                # attached-label decorations which must never match --in-file.
                _, text = runner.command(*flags, 'callers', 'ping', '--in-file', file_filter,
                                         '--limit', 100, cwd=cwd)
                rows, path = [], None
                for line in text.splitlines():
                    match = re.fullmatch(r'  (.+\.java):', line)
                    if match:
                        path = runner.path(match[1])
                    match = re.fullmatch(r'    :(\d+) .*', line)
                    if match:
                        if path is None:
                            raise ToolError('caller text location lacks a path')
                        rows.append((path, int(match[1])))
                record('global:scope:java-callers', f'{mode}:{scope_name}:{label}:text',
                       {'valid': True, 'returned': len(candidates), 'complete': True},
                       page(rows, [(path, 4) for path in candidates], 100))
    # Forced nested attachments have the most specific owner. A primary walk
    # can physically reach them; --local must still exclude them, and two walk
    # roots must not duplicate a caller line or tree owner.
    nested = runner.root / 'inner' / 'Nested.java'
    nested.parent.mkdir()
    nested.write_text(SOURCE.format(package='nested'))
    runner.json('subtree', 'add', 'inner', 'inner', '--force')
    runner.command('rebuild', '--force')
    for mode in ('nested-unbuilt', 'nested-fresh'):
        if mode == 'nested-fresh':
            runner.json('graph', 'build')
        for label, flags, candidates in (
                ('all', [], paths + ['project/inner/Nested.java']),
                ('local', ['--local'], [p for p in paths if p.startswith('project/')]),
                ('inner', ['--subtree', 'inner'], ['project/inner/Nested.java'])):
            key = mode + ':' + label
            doc = runner.json(*flags, 'callers', 'ping', '--limit', 100)
            rows = [(runner.path(row['path']), row['line']) for row in doc['items']]
            got = page(rows, [(path, 4) for path in candidates], 100)
            got['total'] = doc['pagination']['total']
            record('global:scope:java-callers', key,
                   {'valid': True, 'returned': len(candidates), 'complete': True, 'total': len(candidates)}, got)
            _, text = runner.command(*flags, 'call-tree', 'ping', '--depth', 1, '--limit', 100)
            record('global:scope:java-call-tree', key,
                   {'valid': True, 'returned': len(candidates), 'complete': True},
                   page([row[1:] for row in tree_rows(text, runner)],
                        [(path, 4, 'use') for path in candidates], 100))
    return expected, actual
