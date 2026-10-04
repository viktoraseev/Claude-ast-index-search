"""Module rendering on authored Java projects; independent of MCP and native DB truth."""
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
import module_contracts
import route_contracts
from root_contracts import Runner


FEATURES = {'global:format:java-modules', 'global:format:diagram-selection'}
REASON = ('independent source/state: disposable Java module identities, dependency kinds, '
          'unused dependency categories/counts/examples, empty/index states and text/JSON '
          'rendering; diagram selection is an internal CLI contract; not MCP equivalence')
EDGES = [('app', 'dead', 'implementation'), ('app', 'exported', 'api'),
         ('app', 'facade', 'implementation'), ('app', 'live', 'implementation'),
         ('app', 'resources', 'implementation'), ('app', 'xml', 'implementation'),
         ('facade', 'leaf', 'api'), ('observer', 'exported', 'implementation')]
NAMES = ['app', 'dead', 'exported', 'facade', 'leaf', 'live', 'observer', 'quoted"λ', 'resources', 'xml']


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-module-formats'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views and module formats and diagram selection have '
                       'separate executed contracts; other Java command formats remain unresolved',))


def document(output):
    try:
        return json.loads(output)
    except ValueError:
        return {'invalid_json': True}


def empty(command, subject, reason, limit=100):
    result = {'schema_version': 2, 'items': [], 'empty_reason': reason}
    if command == 'module':
        result.update(pattern=subject, pagination={'total': 0, 'returned': 0,
                      'truncated': False, 'limit': limit})
    else:
        result.update(module=subject, count=0)
    if command == 'unused-deps':
        result['summary'] = {key: 0 for key in ('unused', 'exported', 'used', 'total',
                                              'direct', 'transitive', 'xml', 'resources')}
    return result


def usage(flags, verbose):
    enabled = {name: '--strict' not in flags and '--no-' + name not in flags
               for name in ('transitive', 'xml', 'resources')}
    categories = {'dead': 'unused', 'exported': 'exported', 'live': 'direct',
                  'facade': 'transitive' if enabled['transitive'] else 'unused',
                  'xml': 'xml' if enabled['xml'] else 'unused',
                  'resources': 'resources' if enabled['resources'] else 'unused'}
    items = []
    for owner, name, kind in sorted(EDGES, key=lambda e: (e[2], e[1])):
        if owner != 'app':
            continue
        category = categories[name]
        counts = {key: int(category == key) for key in ('direct', 'transitive', 'xml', 'resources')}
        if category == 'direct':
            counts['direct'] = 4
        row = {'name': name, 'path': name, 'kind': kind, 'category': category, 'usage': counts}
        if verbose:
            row['examples'] = {'direct': ['Alpha', 'Beta', 'Gamma'] if category == 'direct' else [],
                'transitive': [{'module': 'leaf', 'symbols': ['Leaf']}] if category == 'transitive' else [],
                'xml': [{'class': 'Widget', 'line': 1}] if category == 'xml' else [],
                'resources': [{'name': '@string/sample', 'usage_type': 'code'}] if category == 'resources' else [],
                'consumers': ['observer'] if category == 'exported' else []}
        items.append(row)
    summary = {key: sum(r['category'] == key for r in items)
               for key in ('unused', 'exported', 'direct', 'transitive', 'xml', 'resources')}
    summary.update(used=sum(summary[k] for k in ('direct', 'transitive', 'xml', 'resources')), total=len(items))
    return {'schema_version': 2, 'module': 'app', 'items': items, 'count': len(items),
            'summary': summary, 'empty_reason': None}


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('module format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='module-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    runner.environment['AST_INDEX_DB_PATH'] = str(directory / 'index.sqlite')
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))
    family = 'global:format:java-modules'

    def sample(key, args, want, format='json'):
        _, output = runner.command(*args, '--format', format)
        expected[family][key] = want
        actual[family][key] = document(output) if format == 'json' else output

    # Early returns must render the same structured contract as successful queries.
    for command in ('module', 'deps', 'dependents', 'unused-deps'):
        args = [command, 'app'] + (['--limit', '100'] if command == 'module' else [])
        sample(command + ':no-index', args, empty(command, 'app', 'no_index'))

    def write(path, source):
        file = runner.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)

    sources = {'live': 'package live; public class Live { public static class Alpha {} '
                       'public static class Beta {} public static class Gamma {} }',
               'leaf': 'package leaf; public class Leaf {}',
               'xml': 'package xml; public class Widget {}',
               'app': 'import live.Live; import leaf.Leaf; import fixture.resources.R;\n'
                      'public class App { Live live; Live.Alpha a; Live.Beta b; Live.Gamma c; Leaf leaf; '
                      'int resource = R.string.sample; }'}
    for name in NAMES:
        deps = [(b, k) for a, b, k in EDGES if a == name]
        build = 'dependencies {\n' + ''.join(f'    {kind}(project(":{dep}"))\n' for dep, kind in deps) + '}\n'
        if name == 'resources':
            build += "android { namespace 'fixture.resources' }\n"
        write(name + '/build.gradle', build)
        filename = {'live': 'Live', 'leaf': 'Leaf', 'xml': 'Widget', 'app': 'App'}.get(name, 'Placeholder')
        write(name + '/' + filename + '.java', sources.get(name, 'class Placeholder {}'))
    write('app/src/main/res/layout/main.xml', '<xml.Widget/>\n')
    write('resources/src/main/res/values/strings.xml', '<resources><string name="sample">value</string></resources>\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        # Full inventory, including Gradle and XML, is private; Java-only inventory
        # is never used as proof of framework absence or applicability.
        for extension, count in (('.java', len(NAMES)), ('.gradle', len(NAMES)), ('.xml', 2)):
            if state.execute('SELECT count(*) FROM file_inventory WHERE extension=? AND kind=\'file\'',
                             (extension,)).fetchone()[0] != count:
                raise ToolError('applicable module format fixture inventory incomplete')
    finally:
        state.close()
    runner.command('rebuild', '--force', '--max-files', '0')

    for query in ('', 'app', 'absent', 'quoted"λ', '%', 'APP'):
        pattern = re.compile(re.escape(query).replace('%', '.*').replace('_', '.'), re.I | re.ASCII)
        matches = [n for n in sorted(NAMES) if pattern.search(n)]
        for limit in (0, 1, 100):
            rows = [{'name': n, 'path': n} for n in matches[:limit]]
            want = {'schema_version': 2, 'pattern': query, 'items': rows,
                    'pagination': {'total': len(matches), 'returned': len(rows),
                                   'truncated': len(matches) > len(rows), 'limit': limit},
                    'empty_reason': 'no_matches' if not matches else None}
            sample(f'module:{query}:{limit}', ['module', query, '--limit', str(limit)], want)
            _, output = runner.command('module', query, '--limit', str(limit))
            expected[family][f'module:{query}:{limit}:text'] = [(n, n) for n in matches[:limit]]
            actual[family][f'module:{query}:{limit}:text'] = module_contracts.module_rows(output)

    for command in ('deps', 'dependents'):
        for subject in ('app', 'live', 'leaf', 'absent', 'quoted"λ'):
            edges = sorted([(b, b, k) if command == 'deps' else (a, a, k)
                            for a, b, k in EDGES if (a if command == 'deps' else b) == subject],
                           key=lambda r: (r[2], r[0]))
            reason = 'missing_module' if subject not in NAMES else 'no_dependencies' if command == 'deps' else 'no_dependents'
            want = {**empty(command, subject, None if edges else reason),
                    'items': [{'name': n, 'path': p, 'kind': k} for n, p, k in edges], 'count': len(edges)}
            sample(command + ':' + subject, [command, subject], want)
            _, output = runner.command(command, subject)
            expected[family][command + ':' + subject + ':text'] = sorted(edges)
            actual[family][command + ':' + subject + ':text'] = sorted(module_contracts.edge_rows(output, command))

    for flags in ([], ['--strict'], ['--no-transitive'], ['--no-xml'], ['--no-resources']):
        for verbose in (False, True):
            args = ['unused-deps', 'app', *flags] + (['--verbose'] if verbose else [])
            want = usage(flags, verbose)
            key = 'unused-deps:' + ','.join(args[2:])
            sample(key, args, want)
            _, output = runner.command(*args)
            summary = re.search(r'^Total: (\d+) unused, (\d+) exported, (\d+) used of (\d+) dependencies$', output, re.M)
            observed = {'summary': list(map(int, summary.groups())) if summary else None,
                        'unused': sorted(re.findall(r'^  ✗ (.+) \(.+\)$', output, re.M)),
                        'exported': sorted(re.findall(r'^  ⚡ (.+) \(api\)$', output, re.M)),
                        'ansi': '\x1b' in output}
            expected[family][key + ':text'] = {'summary': [want['summary'][k] for k in ('unused', 'exported', 'used', 'total')],
                        'unused': sorted(r['name'] for r in want['items'] if r['category'] == 'unused'),
                        'exported': sorted(r['name'] for r in want['items'] if r['category'] == 'exported'), 'ansi': False}
            actual[family][key + ':text'] = observed
    for subject, reason in (('absent', 'missing_module'), ('leaf', 'no_dependencies')):
        sample('unused-deps:' + subject, ['unused-deps', subject], empty('unused-deps', subject, reason))

    for format in ('mermaid', 'dot'):
        _, output = runner.command('module-route', '--from', 'app', '--to', 'live', '--format', format)
        key = format + ':supported-route'
        expected['global:format:diagram-selection'][key] = route_contracts.diagram(
            {'paths': [{'hops': [{'from': 'app', 'to': 'live', 'kind': 'implementation'}]}],
             'truncated': False}, format)
        actual['global:format:diagram-selection'][key] = output

    # Controlled native state is only an internal readiness contract, not a
    # source oracle. Mutate exclusively the disposable fixture's own index.
    conn = sqlite3.connect(directory / 'index.sqlite')
    try:
        with conn:
            conn.execute('DELETE FROM module_deps')
            conn.execute("DELETE FROM metadata WHERE key='last_modules_indexed_at'")
    finally:
        conn.close()
    for command in ('deps', 'dependents', 'unused-deps'):
        sample(command + ':unindexed', [command, 'app'], empty(command, 'app', 'not_indexed'))

    # Validate selection before index discovery or mutation. Version gives a
    # cache-independent probe in addition to the module family.
    for format in ('mermaid', 'dot'):
        for args in (['module', 'app'], ['deps', 'app'], ['dependents', 'app'],
                     ['unused-deps', 'app'], ['version'], ['rebuild', '--force']):
            fingerprint = file_sha256(directory / 'index.sqlite')
            code, output = runner.command(*args, '--format', format, acceptable=(0, 1, 2))
            key = ':'.join([format, *args])
            expected['global:format:diagram-selection'][key] = {'rejected': True, 'stdout_empty': True, 'index_unchanged': True}
            actual['global:format:diagram-selection'][key] = {'rejected': code != 0, 'stdout_empty': output == '',
                'index_unchanged': fingerprint == file_sha256(directory / 'index.sqlite')}
    for format in ('yaml', 'JSON'):
        code, output = runner.command('module', 'app', '--format', format, acceptable=(0, 1, 2))
        expected['global:format:diagram-selection'][format] = {'rejected': True, 'stdout_empty': True}
        actual['global:format:diagram-selection'][format] = {'rejected': code != 0, 'stdout_empty': output == ''}
    return expected, actual
