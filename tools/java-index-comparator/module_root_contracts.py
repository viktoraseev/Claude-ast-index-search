"""Authored Maven/Java root-induced module graphs; no MCP equivalence claim."""
from pathlib import Path
import tempfile

from common import ToolError, connect, stable_id, file_sha256
import mobile_contracts
import module_contracts
import route_contracts
from root_contracts import Runner

FEATURES = {'global:scope:java-module-roots'}
REASON = ('independent source/state: disposable Maven/Java colliding root module identities, '
          'root/directory intersections, coordinate binding, dependency/reverse/route graph, '
          'strict unused classifications, pages and rendering; not MCP equivalence or '
          'compiler-wide attached-root resolution')


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-module-root-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      ('Java module directory/root graphs, file views, navigation, caller/call-tree, '
                       'map/conventions and graph selectors have separate executed contracts; '
                       'analysis/explore scope and compiler-wide attached-root module graphs/resolution remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('module root fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='module-roots-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    database = directory / 'index.sqlite'
    runner.environment['AST_INDEX_DB_PATH'] = str(database)
    owners = ['project', 'attached', 'other']
    expected, actual = {}, {}
    def record(key, want, got):
        expected[key], actual[key] = want, got
    for owner in owners:
        root = directory / owner
        for module in ['scope_/app', 'scope_/live', 'scopeX/dead']:
            folder = root / module
            folder.mkdir(parents=True, exist_ok=True)
            artifact = module.rsplit('/', 1)[-1]
            deps = ''.join('<dependency><groupId>fixture</groupId><artifactId>' + dep +
                           '</artifactId><version>1</version></dependency>' for dep in
                           (['live', 'dead'] if artifact == 'app' else []))
            (folder / 'pom.xml').write_text('<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId>'
                                          f'<artifactId>{artifact}</artifactId><version>1</version>'
                                          f'<dependencies>{deps}</dependencies></project>\n')
            source = ('package fixture; public class Live {}' if artifact == 'live' else
                      'import fixture.Live; class App { Live value; }' if artifact == 'app' and owner == 'project' else
                      'class Placeholder {}')
            (folder / ('Live.java' if artifact == 'live' else 'Main.java')).write_text(source + '\n')
        (root / 'Inventory.kt').write_text('// inventory only\n')
        (root / 'empty').mkdir()
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, root)
            inventory = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            if inventory != {'.java': 3, '.kt': 1, '.xml': 3}:
                raise ToolError('module root full inventory incomplete')
            record('inventory:' + owner, {'.java': 3, '.kt': 1, '.xml': 3}, inventory)
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    # Internal schema compatibility complements the source ledger. It does
    # not establish MCP equivalence or repair legacy foreign module models.
    state = connect(database)
    with state:
        state.execute('ALTER TABLE modules DROP COLUMN root_path')
    state.close()
    output = runner.json('--local', 'module', '', '--limit', 100)
    record('legacy-primary-schema', ['scopeX.dead', 'scope_.app', 'scope_.live'],
           [r['name'] for r in output.get('items', [])])
    for owner in owners[1:]:
        runner.json('subtree', 'add', owner, '../' + owner)
    runner.command('rebuild', '--force', '--max-files', '0')
    fingerprint = file_sha256(database)
    def name(owner, path):
        return ('' if owner == 'project' else owner + '::') + path.replace('/', '.')
    paths = ['scope_/app', 'scope_/live', 'scopeX/dead']
    scopes = [('all', [], owners), ('local', ['--local'], ['project']),
              ('attached', ['--subtree', 'attached'], ['attached']),
              ('other', ['--subtree', 'other'], ['other'])]
    for label, flags, selected_owners in scopes:
        for prefix in ['', 'scope_/', 'empty/']:
            selected = [(o, p) for o in selected_owners for p in paths if (p + '/').startswith(prefix)]
            nodes = sorted((name(o, p), o, p) for o, p in selected)
            edges = {(name(o, 'scope_/app'), name(o, p), o, p) for o in selected_owners
                     for p in paths[1:] if (o, 'scope_/app') in selected and (o, p) in selected}
            cwd = runner.root / prefix
            key = label + ':' + prefix
            for limit in [0, 1, 100]:
                output = runner.json(*flags, 'module', '', '--limit', limit, cwd=cwd)
                record(key + f':module:{limit}', {'rows': [(n, f'{o}/{p}') for n, o, p in nodes[:limit]],
                       'total': len(nodes), 'returned': min(limit, len(nodes)), 'truncated': limit < len(nodes)},
                       {'rows': [(r['name'], runner.path(r['path'])) for r in output.get('items', [])],
                        **{k: output.get('pagination', {}).get(k) for k in ['total', 'returned', 'truncated']}})
                _, text = runner.command(*flags, 'module', '', '--limit', limit, cwd=cwd)
                record(key + f':module-text:{limit}', [(n, f'{o}/{p}') for n, o, p in nodes[:limit]],
                       [(n, runner.path(p)) for n, p in module_contracts.module_rows(text)])
            for owner in owners:
                app, live, dead = (name(owner, p) for p in paths)
                for command, seed in [('deps', app), ('dependents', live), ('unused-deps', app)]:
                    rows = sorted((b if command != 'dependents' else a, f'{o}/{p}' if command != 'dependents' else f'{o}/scope_/app')
                                  for a, b, o, p in edges if (b == seed if command == 'dependents' else a == seed))
                    output = runner.json(*flags, command, seed, *(['--strict', '--verbose'] if command == 'unused-deps' else []), cwd=cwd)
                    seed_path = paths[1] if command == 'dependents' else paths[0]
                    reason = ('missing_module' if (owner, seed_path) not in selected else None if rows else
                              'no_dependents' if command == 'dependents' else 'no_dependencies')
                    record(key + ':' + command + ':' + owner, {'rows': rows, 'count': len(rows), 'reason': reason},
                           {'rows': [(r['name'], runner.path(r['path'])) for r in output.get('items', [])],
                            'count': output.get('count'), 'reason': output.get('empty_reason')})
                    if command == 'unused-deps':
                        record(key + ':usage:' + owner,
                               ['direct' if owner == 'project' and n == live else 'unused' for n, _ in rows],
                               [r.get('category') for r in output.get('items', [])])
                    else:
                        _, text = runner.command(*flags, command, seed, cwd=cwd)
                        record(key + ':text:' + command + ':' + owner, [(n, p, 'compile') for n, p in rows],
                               [(n, runner.path(p), k) for n, p, k in module_contracts.edge_rows(text, command)])
                args = [*flags, 'module-route', '--from', app, '--to', live, '--all']
                want = {'from': app, 'to': live, 'paths': ([{'length': 1, 'hops': [
                    {'from': app, 'to': live, 'kind': 'compile'}]}] if (app, live, owner, paths[1]) in edges else []),
                    'count': int((app, live, owner, paths[1]) in edges), 'truncated': False,
                    'truncation_reason': None, 'empty_reason': 'missing_module_from' if (owner, paths[0]) not in selected else
                    'missing_module_to' if (owner, paths[1]) not in selected else None}
                record(key + ':route:' + owner, want, route_contracts.envelope(runner.json(*args, cwd=cwd)))
                for fmt in ['mermaid', 'dot']:
                    _, output = runner.command(*args, '--format', fmt, cwd=cwd)
                    record(key + ':route:' + owner + ':' + fmt, route_contracts.diagram(want, fmt), output)
    for owner, flags in [('project', ['--local']), ('attached', ['--subtree', 'attached'])]:
        for alias in ['scope_/app', str(directory / owner / 'scope_/app')]:
            rows = runner.json(*flags, 'deps', alias).get('items', [])
            record('alias:' + owner + ':' + alias, [name(owner, p) for p in ['scopeX/dead', 'scope_/live']],
                   [r['name'] for r in rows])
    code, _ = runner.command('deps', 'scope_/app', acceptable=(0, 1))
    record('ambiguous-path-rejected', 1, code)
    _, output = runner.command('--subtree', 'attached', 'unused-deps', 'attached::scope_.app', '--strict', '--verbose')
    record('unused-text-root-identity', True, 'attached::scope_.live' in output and 'attached::scopeX.dead' in output)
    live = directory / 'attached/scope_/live'
    moved = directory / 'attached/scope_/temporarily-moved'
    live.rename(moved)
    try:
        rows = runner.json('--subtree', 'attached', 'deps', 'attached::scope_.app').get('items', [])
        record('stale-root-display', ['attached/scopeX/dead', 'attached/scope_/live'],
               [runner.path(r['path']) for r in rows])
    finally:
        moved.rename(live)
    record('queries-read-only', fingerprint, file_sha256(database))
    # Modules-only refresh must retain root ownership and local Maven edges.
    runner.command('rebuild', '--type', 'modules')
    for label, flags, selected_owners in scopes:
        output = runner.json(*flags, 'module', '', '--limit', 100)
        record('refresh:' + label, sorted(name(o, p) for o in selected_owners for p in paths),
               [r['name'] for r in output.get('items', [])])
        for owner in selected_owners:
            output = runner.json(*flags, 'deps', name(owner, 'scope_/app'))
            record('refresh-edges:' + label + ':' + owner, [name(owner, p) for p in ['scopeX/dead', 'scope_/live']],
                   [r['name'] for r in output.get('items', [])])
    # A unique foreign coordinate can bind across roots. Scoped queries must
    # induce the graph rather than leak an excluded endpoint into routes.
    unique = directory / 'other/scope_/unique'
    unique.mkdir()
    descriptor = ('<project><modelVersion>4.0.0</modelVersion><groupId>shared</groupId>'
                  '<artifactId>unique</artifactId><version>1</version></project>\n')
    (unique / 'pom.xml').write_text(descriptor)
    (unique / 'Unique.java').write_text('package shared; public class Unique {}\n')
    app = runner.root / 'scope_/app'
    pom = app / 'pom.xml'
    pom.write_text(pom.read_text().replace('</dependencies>',
        '<dependency><groupId>shared</groupId><artifactId>unique</artifactId><version>1</version>'
        '</dependency></dependencies>'))
    (app / 'Main.java').write_text('import fixture.Live; import shared.Unique; class App { Live live; Unique other; }\n')
    runner.command('update')
    for label, flags, selected_owners in scopes:
        rows = runner.json(*flags, 'deps', 'scope_.app').get('items', [])
        want = (['other::scope_.unique', 'scopeX.dead', 'scope_.live'] if label == 'all' else
                ['scopeX.dead', 'scope_.live'] if label == 'local' else [])
        record('cross-root-deps:' + label, want, [r['name'] for r in rows])
        output = runner.json(*flags, 'unused-deps', 'scope_.app', '--strict')
        record('cross-root-usage:' + label,
               [('other::scope_.unique', 'direct'), ('scopeX.dead', 'unused'), ('scope_.live', 'direct')] if label == 'all' else
               [('scopeX.dead', 'unused'), ('scope_.live', 'direct')] if label == 'local' else [],
               [(r['name'], r['category']) for r in output.get('items', [])])
        route = runner.json(*flags, 'module-route', '--from', 'scope_.app', '--to', 'other::scope_.unique')
        record('cross-root-route:' + label, {'count': int(label == 'all'),
               'reason': None if label == 'all' else 'missing_module_to' if label == 'local' else 'missing_module_from'},
               {'count': route.get('count'), 'reason': route.get('empty_reason')})
    # Two foreign candidates cannot prove a Maven binding. Retain no edge
    # rather than let root discovery order choose a dependency arbitrarily.
    ambiguous = directory / 'attached/scope_/unique'
    ambiguous.mkdir()
    (ambiguous / 'pom.xml').write_text(descriptor)
    (ambiguous / 'Unique.java').write_text('package shared; public class Unique {}\n')
    runner.command('update')
    record('ambiguous-coordinate-guard', ['scopeX.dead', 'scope_.live'],
           [r['name'] for r in runner.json('deps', 'scope_.app').get('items', [])])
    for owner in owners:
        state = connect(directory / (owner + '-final-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            count = 3 + int(owner != 'project')
            want = {'.java': count, '.kt': 1, '.xml': count}
            got = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            if got != want:
                raise ToolError('module root final inventory incomplete')
            record('final-inventory:' + owner, want, got)
        finally:
            state.close()
    # Fail closed for an unknown selector: returning the primary graph is unsafe.
    code, _ = runner.command('--subtree', 'absent', 'deps', 'scope_.app', acceptable=(0, 1))
    record('unknown-subtree-rejected', 1, code)
    runner.json('subtree', 'remove', 'other')
    for flags, label in [([], 'all'), (['--local'], 'local')]:
        rows = runner.json(*flags, 'module', '', '--limit', 100).get('items', [])
        record('removed-owner:' + label, False, any(r['name'].startswith('other::') for r in rows))
    return {next(iter(FEATURES)): expected}, {next(iter(FEATURES)): actual}
