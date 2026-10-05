"""Primary Java module directory scope, from authored descriptors, not MCP truth.

The selected module graph is induced by module directory locations. Ancestor
modules are not selected from a source subdirectory, and routes cannot leave
and re-enter the selected graph. Attached module graphs remain pending.
"""
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id, file_sha256
import mobile_contracts
import module_contracts
import route_contracts
from root_contracts import Runner

FEATURES = {'global:scope:java-module-directories'}
REASON = ('independent source/state: disposable Maven/Java module directory scope, '
          'seed aliases, induced dependency/reverse/route graph, strict unused dependency '
          'classification, ordered limits and text/JSON rendering; not MCP equivalence '
          'or attached-root module graph coverage')
GAP = ('Java file views, navigation, caller/call-tree, map/conventions and primary module '
       'directory selectors have separate executed contracts; attached-root module graphs '
       'and analysis/graph/explore scope remain unresolved')
PATHS = ['scope_/app', 'scope_/live', 'scope_/dead', 'scope_/app/child',
         'scopeX/bridge', 'scope%/app', 'CAPS/app']
EDGES = [('scope_/app', 'scope_/live'), ('scope_/app', 'scope_/dead'),
         ('scope_/app', 'scopeX/bridge'), ('scope_/live', 'scope_/dead'),
         ('scopeX/bridge', 'scope_/dead')]


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-module-directory-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      (GAP,))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('module scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='module-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    database = directory / 'index.sqlite'
    runner.environment['AST_INDEX_DB_PATH'] = str(database)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    for path in PATHS:
        folder = runner.root / path
        folder.mkdir(parents=True, exist_ok=True)
        artifact = path.replace('/', '-')
        deps = ''.join('<dependency><groupId>fixture</groupId><artifactId>' + b.replace('/', '-') +
                       '</artifactId><version>1</version></dependency>' for a, b in EDGES if a == path)
        (folder / 'pom.xml').write_text('<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId>'
                                      f'<artifactId>{artifact}</artifactId><version>1</version>'
                                      f'<dependencies>{deps}</dependencies></project>\n')
        source = ('package fixture; public class Live {}' if path == 'scope_/live' else
                  'import fixture.Live; class App { Live value; }' if path == 'scope_/app' else
                  'class Placeholder {}')
        (folder / ('Live.java' if path == 'scope_/live' else 'Main.java')).write_text(source + '\n')
    # Presence of foreign sources/build types is inventoried, never parser coverage.
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    (runner.root / 'scope_' / 'app' / 'src').mkdir()
    (runner.root / 'empty').mkdir()
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if inventory != {'.java': len(PATHS), '.kt': 1, '.xml': len(PATHS) + 1}:
            raise ToolError('module scope full inventory incomplete')
    finally:
        state.close()
    runner.command('rebuild', '--force', '--max-files', '0')
    fingerprint = file_sha256(database)
    expected, actual = {}, {}
    expected['inventory'], actual['inventory'] = {'.java': len(PATHS), '.kt': 1, '.xml': len(PATHS) + 1}, inventory

    def record(key, want, got):
        expected[key], actual[key] = want, got

    for prefix in ('scope_/', 'scope%/', 'CAPS/', 'scope_/app/', 'scope_/app/src/', 'empty/', ''):
        selected = {p for p in PATHS if (p + '/').startswith(prefix)}
        names = {p: p.replace('/', '.') for p in selected}
        edges = {(names[a], names[b], 'compile') for a, b in EDGES if a in selected and b in selected}
        cwd = runner.root / prefix
        for flags in ([], ['--local']):
            label = prefix + ':' + ','.join(flags)
            for query in ('', 'app', 'APP', 'absent', '%'):
                pattern = re.compile(re.escape(query).replace('%', '.*').replace('_', '.'), re.I | re.ASCII)
                matches = sorted((n, p) for p, n in names.items() if pattern.search(n))
                for limit in (0, 1, 100):
                    args = [*flags, 'module', query, '--limit', str(limit)]
                    output = runner.json(*args, cwd=cwd)
                    key = f'{label}:module:{query}:{limit}'
                    record(key, {'items': [{'name': n, 'path': p} for n, p in matches[:limit]],
                                 'pagination': {'total': len(matches), 'returned': min(limit, len(matches)),
                                                'truncated': len(matches) > limit, 'limit': limit},
                                 'empty_reason': None if matches else 'no_matches'},
                           {k: output.get(k) for k in ('items', 'pagination', 'empty_reason')})
                    _, text = runner.command(*args, cwd=cwd)
                    record(key + ':text', matches[:limit], module_contracts.module_rows(text))
            for path in ('scope_/app', 'scope_/dead', 'scopeX/bridge', 'absent'):
                for subject in (path.replace('/', '.'), path):
                    name = path.replace('/', '.')
                    for command in ('deps', 'dependents'):
                        rows = sorted([(b if command == 'deps' else a,
                                        next(p for p, n in names.items() if n == (b if command == 'deps' else a)), k)
                                       for a, b, k in edges if (a if command == 'deps' else b) == name],
                                      key=lambda r: (r[2], r[0]))
                        reason = 'missing_module' if path not in selected else (
                            None if rows else 'no_dependencies' if command == 'deps' else 'no_dependents')
                        args = [*flags, command, subject]
                        output = runner.json(*args, cwd=cwd)
                        key = f'{label}:{command}:{subject}'
                        record(key, {'items': [{'name': n, 'path': p, 'kind': k} for n, p, k in rows],
                                     'count': len(rows), 'empty_reason': reason},
                               {k: output.get(k) for k in ('items', 'count', 'empty_reason')})
                        _, text = runner.command(*args, cwd=cwd)
                        record(key + ':text', rows, module_contracts.edge_rows(text, command))
                    args = [*flags, 'unused-deps', subject, '--strict', '--verbose']
                    output = runner.json(*args, cwd=cwd)
                    rows = [{'name': b, 'path': next(p for p, n in names.items() if n == b), 'kind': k,
                             'category': 'direct' if b == 'scope_.live' and name == 'scope_.app' else 'unused'}
                            for a, b, k in sorted(edges, key=lambda e: (e[2], e[1])) if a == name]
                    reason = 'missing_module' if path not in selected else None if rows else 'no_dependencies'
                    observed = [{k: r.get(k) for k in ('name', 'path', 'kind', 'category')} for r in output.get('items', [])]
                    key = f'{label}:unused-deps:{subject}'
                    record(key, {'items': rows, 'count': len(rows), 'empty_reason': reason},
                           {'items': observed, 'count': output.get('count'), 'empty_reason': output.get('empty_reason')})
                    _, text = runner.command(*args, cwd=cwd)
                    record(key + ':text', sorted((r['name'], r['category']) for r in rows),
                           sorted([(n, 'direct') for n in re.findall(r'^  ✓ (\S+) - \d+ symbols', text, re.M)] +
                                  [(n, 'unused') for n in re.findall(r'^  ✗ (\S+) \(compile\)', text, re.M)]))
            for start, end in (('scope_/app', 'scope_/dead'), ('scope_/app', 'scopeX/bridge'),
                               ('scope_/dead', 'scope_/app')):
                for aliases in (False, True):
                    a, b = start.replace('/', '.'), end.replace('/', '.')
                    paths = module_contracts.paths(edges, a, b, 10, 'all')
                    want = {'from': start if aliases else a, 'to': end if aliases else b,
                            'paths': [{'length': len(p), 'hops': [{'from': x, 'to': y, 'kind': k} for x, y, k in p]}
                                      for p in paths], 'count': len(paths), 'truncated': False,
                            'truncation_reason': None, 'empty_reason': 'missing_module_from' if start not in selected else
                            'missing_module_to' if end not in selected else None if paths else 'unreachable'}
                    args = [*flags, 'module-route', '--from', want['from'], '--to', want['to'], '--all']
                    key = f'{label}:route:{start}:{end}:{aliases}'
                    record(key, want, route_contracts.envelope(runner.json(*args, cwd=cwd)))
                    for format in ('mermaid', 'dot', 'text'):
                        _, text = runner.command(*args, '--format', format, cwd=cwd)
                        if format == 'text':
                            record(key + ':text', {'hops': [(h['from'], h['to'], h['kind'])
                                for p in want['paths'] for h in p['hops']],
                                'lengths': [p['length'] for p in want['paths']], 'reason': True,
                                'count': len(paths), 'ansi': False}, route_contracts.text_observation(text, want))
                        else:
                            record(key + ':' + format, route_contracts.diagram(want, format), text)
    record('read-only-index', fingerprint, file_sha256(database))
    # Legacy nonempty module graphs may predate the indexing timestamp. Their
    # readiness must not depend on whether the selected directory has edges.
    # This controlled state check is internal CLI/DB evidence, not source truth.
    state = connect(database)
    with state:
        state.execute("DELETE FROM metadata WHERE key='last_modules_indexed_at'")
    state.close()
    for command in ('deps', 'dependents', 'unused-deps'):
        output = runner.json(command, 'scope_.app', cwd=runner.root / 'empty')
        record('internal-readiness:' + command,
               {'source': 'internal CLI/DB readiness; not MCP equivalence', 'reason': 'missing_module'},
               {'source': 'internal CLI/DB readiness; not MCP equivalence', 'reason': output.get('empty_reason')})
    output = runner.json('module-route', '--from', 'scope_.app', '--to', 'scope_.dead', cwd=runner.root / 'empty')
    record('internal-readiness:route',
           {'source': 'internal CLI/DB readiness; not MCP equivalence', 'reason': 'missing_module_from'},
           {'source': 'internal CLI/DB readiness; not MCP equivalence', 'reason': output.get('empty_reason')})
    return {next(iter(FEATURES)): expected}, {next(iter(FEATURES)): actual}
