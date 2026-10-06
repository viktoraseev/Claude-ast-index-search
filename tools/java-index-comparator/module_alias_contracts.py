"""Selected-owner module aliases on authored Java/Maven roots, not MCP truth."""
import json
from pathlib import Path
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
import module_contracts
import route_contracts
from root_contracts import Runner

SCOPE = 'global:scope:java-module-aliases'
ERRORS = 'global:format:java-module-alias-errors'
FEATURES = {SCOPE, ERRORS}
REASON = ('independent source/state and internal CLI: disposable Java/Maven module aliases, '
          'selected-owner ambiguity, exact-name precedence, qualified/path/Gradle forms, '
          'dependency/reverse/unused/route identities and JSON/text error isolation; '
          'not MCP equivalence or compiler-wide attached-root resolution')


def plan_aliases(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-module-aliases'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      ('Java module directory/root graphs and selected-owner aliases, file views, '
                       'navigation, caller/call-tree, map/conventions, graph and analysis/exploration '
                       'selectors have separate executed contracts; compiler-wide attached-root '
                       'module graphs/resolution remain unresolved',))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java module alias ambiguity errors')=0",
                      ('; Java module alias ambiguity errors have a separate executed JSON/text contract',))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('module alias fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    # Namespace punctuation in a physical path is not a subtree qualifier.
    directory = Path(tempfile.mkdtemp(prefix='module-aliases::', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment.update(AST_INDEX_ROOT=str(runner.root),
                              AST_INDEX_DB_PATH=str(directory / 'index.sqlite'))
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    owners = ['project', 'attached', 'other']
    for owner in owners:
        for leaf in ['app', 'live']:
            folder = directory / owner / 'scope_' / leaf
            folder.mkdir(parents=True)
            dependency = ('<dependencies><dependency><groupId>fixture</groupId>'
                          '<artifactId>live</artifactId></dependency></dependencies>' if leaf == 'app' else '')
            (folder / 'pom.xml').write_text('<project><modelVersion>4.0.0</modelVersion>'
                f'<groupId>fixture</groupId><artifactId>{leaf}</artifactId>{dependency}</project>\n')
            source = ('package fixture; public class Live {}\n' if leaf == 'live' else
                      'import fixture.Live; class App { Live value; }\n' if owner == 'project' else
                      'class App {}\n')
            (folder / ('Live.java' if leaf == 'live' else 'App.java')).write_text(source)
        (directory / owner / 'empty').mkdir()
        # Presence evidence covers every type. These descriptors are not
        # parsed as a second language or used as declaration expectations.
        (directory / owner / 'inventory.txt').write_text('inventory sentinel\n')
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            if counts != {'.java': 2, '.xml': 2, '.txt': 1}:
                raise ToolError('module alias full inventory incomplete')
            record(SCOPE, 'inventory:' + owner, {'.java': 2, '.xml': 2, '.txt': 1}, counts)
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    for owner in owners[1:]:
        runner.json('subtree', 'add', owner, '../' + owner)
    runner.command('rebuild', '--force', '--max-files', '0')
    fingerprint = file_sha256(directory / 'index.sqlite')

    def name(owner, leaf):
        return ('' if owner == 'project' else owner + '::') + 'scope_.' + leaf

    def arguments(command, app, live):
        if command == 'module-route':
            return [command, '--from', app, '--to', live]
        return [command, live if command == 'dependents' else app,
                *(['--strict', '--verbose'] if command == 'unused-deps' else [])]

    def sample(label, flags, app, live, owner, *, cwd=None, ambiguous=False):
        for command in ['deps', 'dependents', 'unused-deps', 'module-route']:
            for fmt in ['json', 'text']:
                feature = ERRORS if ambiguous else SCOPE
                key = label + ':' + command + ':' + fmt
                code, output = runner.command('--format', fmt, *flags, *arguments(command, app, live),
                                              cwd=cwd, acceptable=(0, 1))
                if ambiguous:
                    with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
                        diagnostic = stream.read(runner.output_budget + 1)
                    if len(diagnostic) > runner.output_budget:
                        raise ToolError('module alias diagnostic exceeded its budget')
                    record(feature, key, {'code': 1, 'stdout': '', 'ambiguity': True},
                           {'code': code, 'stdout': output, 'ambiguity':
                            b'Ambiguous module' in diagnostic})
                    continue
                if code:
                    record(feature, key, {'code': 0}, {'code': code})
                    continue
                target = 'app' if command == 'dependents' else 'live'
                wanted = [name(owner, target)] if owner else []
                reason = None if owner else 'missing_module'
                if fmt == 'json':
                    try:
                        doc = json.loads(output)
                    except ValueError as error:
                        raise ToolError('module alias expected JSON; see private logs') from error
                    if command == 'module-route':
                        record(feature, key, {'hops': [(name(owner, 'app'), name(owner, 'live'), 'compile')]
                            if owner else [], 'count': int(bool(owner)),
                            'reason': None if owner else 'missing_module_from'},
                            {'hops': [(hop['from'], hop['to'], hop['kind']) for path in doc.get('paths', [])
                                      for hop in path.get('hops', [])], 'count': doc.get('count'),
                             'reason': doc.get('empty_reason')})
                    else:
                        want = {'names': wanted, 'paths': [f'{owner}/scope_/{target}'] if owner else [],
                                'count': len(wanted), 'reason': reason}
                        got = {'names': [r['name'] for r in doc.get('items', [])],
                               'paths': [runner.path(r['path']) for r in doc.get('items', [])],
                               'count': doc.get('count'), 'reason': doc.get('empty_reason')}
                        if command == 'unused-deps':
                            want['categories'] = [('direct' if owner == 'project' else 'unused')] if owner else []
                            got['categories'] = [r.get('category') for r in doc.get('items', [])]
                        record(feature, key, want, got)
                elif command in ['deps', 'dependents']:
                    record(feature, key, [(n, f'{owner}/scope_/{target}', 'compile') for n in wanted],
                           [(n, runner.path(p), k) for n, p, k in module_contracts.edge_rows(output, command)])
                elif command == 'unused-deps':
                    # Require dependency identities as well as counts. A
                    # missing/empty response cannot pass by JSON shape alone.
                    want = f'Total: {int(owner != "project")} unused, 0 exported, {int(owner == "project")} used of 1 dependencies'
                    record(feature, key, {'identity': True, 'summary': True},
                           {'identity': name(owner, 'live') in output,
                            'summary': want in output} if owner else
                           {'identity': "not found in index" in output, 'summary': "not found in index" in output})
                else:
                    record(feature, key, [(name(owner, 'app'), name(owner, 'live'), 'compile')] if owner else [],
                           route_contracts.text_observation(output, {
                               'from': app, 'to': live, 'count': int(bool(owner)),
                               'empty_reason': None if owner else 'missing_module_from',
                               'paths': [{'length': 1, 'hops': [{'from': name(owner, 'app'),
                                   'to': name(owner, 'live'), 'kind': 'compile'}]}] if owner else [],
                               'truncated': False})['hops'])

    forms = [('slash', 'scope_/{}'), ('gradle', ':scope_:{}'),
             ('colon', 'scope_:{}'), ('dotted', 'scope_.{}')]
    for label, flags, owner in [('local', ['--local'], 'project'),
                               ('attached', ['--subtree', 'attached'], 'attached'),
                               ('other', ['--subtree', 'other'], 'other')]:
        for form, pattern in forms:
            # A literal primary name retains that identity when excluded;
            # only aliases may be rebound to a uniquely selected subtree.
            selected = None if form == 'dotted' and owner != 'project' else owner
            sample(label + ':' + form, flags, pattern.format('app'), pattern.format('live'), selected)
        sample(label + ':empty-directory', flags, 'scope_.app', 'scope_.live', None,
               cwd=runner.root / 'empty')
    # Exact names retain precedence; other unqualified aliases must not
    # silently choose the primary owner when several roots are selected.
    for form, pattern in forms:
        sample('all:' + form, [], pattern.format('app'), pattern.format('live'),
               'project', ambiguous=form != 'dotted')
    for owner in owners:
        sample('absolute:' + owner, [], str(directory / owner / 'scope_/app'),
               str(directory / owner / 'scope_/live'), owner)
    for owner in owners[1:]:
        for form, pattern in forms:
            app, live = (owner + '::' + pattern.format(leaf) for leaf in ['app', 'live'])
            sample('qualified:' + owner + ':' + form, [], app, live, owner)
            sample('excluded:' + owner + ':' + form, ['--local'], app, live, None)
    sample('unknown-owner', [], 'absent::scope_/app', 'absent::scope_/live', None)
    # Selection is checked independently for both route endpoints. Reusing
    # only a from-id must not hide an ambiguous destination.
    for app, live in [('scope_/app', 'attached::scope_.live'),
                      ('attached::scope_.app', 'scope_/live')]:
        for fmt in ['json', 'text', 'mermaid', 'dot']:
            code, output = runner.command('--format', fmt, 'module-route', '--from', app,
                                          '--to', live, acceptable=(0, 1))
            record(ERRORS, 'route-endpoints:' + app + ':' + fmt,
                   {'code': 1, 'stdout': ''}, {'code': code, 'stdout': output})
    record(SCOPE, 'queries-read-only', fingerprint, file_sha256(directory / 'index.sqlite'))
    # Registration removal changes alias cardinality, not exact-name rules.
    runner.json('subtree', 'remove', 'other')
    sample('two-owners', [], ':scope_:app', ':scope_:live', 'project', ambiguous=True)
    # A literal path can identify one owner even when its normalized spelling
    # matches another owner's logical name. Preserve path precedence too.
    original = runner.root / 'scope_/app'
    moved = runner.root / 'scope_.app'
    original.rename(moved)
    try:
        state = connect(directory / 'project-renamed-inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, runner.root)
        finally:
            state.close()
        runner.command('rebuild', '--force', '--max-files', '0')
        sample('literal-path-before-normalized-name', [], 'scope_/app', 'attached::scope_.live', 'attached')
        sample('normalized-name-still-ambiguous', [], ':scope_:app', ':scope_:live',
               'project', ambiguous=True)
    finally:
        moved.rename(original)
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.json('subtree', 'remove', 'attached')
    sample('one-owner', [], ':scope_:app', ':scope_:live', 'project')
    return expected, actual
