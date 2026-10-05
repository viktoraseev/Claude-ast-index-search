"""Java analysis/exploration selectors on authored sources, not MCP equivalence."""
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

ANALYSIS = 'global:scope:java-analysis'
EXPLORE = 'global:scope:java-explore'
FEATURES = {ANALYSIS, EXPLORE}
REASON = ('independent source/state: disposable javac-validated Java analysis/exploration '
          'directory/root intersections, literal module aliases, export selection, ordered limits, '
          'source/test ownership, fresh/unbuilt graph neighbours and text/JSON identities; '
          'unused analysis retains its indexed name-reference heuristic; not MCP equivalence '
          'or compiler-wide dispatch')


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-analysis-exploration-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      ('Java module directory/root graphs, file views, navigation, caller/call-tree, '
                       'map/conventions, graph and analysis/exploration selectors have separate executed '
                       'contracts; ambiguous attached-root module alias ownership and compiler-wide '
                       'attached-root module graphs/resolution remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('analysis scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='analysis-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, label, want, got):
        expected[feature][label], actual[feature][label] = want, got

    rows = []
    for owner, folders in [('project', ['scope_', 'scopeX', 'scope%', 'CAPS', 'scope_/nested']),
                           ('attached', ['scope_'])]:
        for number, folder in enumerate(folders):
            package = f'fixture.{owner}.p{number}'
            destination = directory / owner / folder
            destination.mkdir(parents=True)
            value = number + (100 if owner == 'attached' else 0)
            source = (f'package {package};\nclass ScopeProbe {{\n'
                      f' int signal() {{ return {value}; }}\n void UpperUnused() {{}}\n void used() {{}}\n'
                      ' void consumer() { used(); signal(); }\n}\n')
            (destination / 'ScopeProbe.java').write_text(source)
            (destination / 'ScopeProbeTest.java').write_text(
                f'package {package};\nclass ScopeProbeTest {{}}\n')
            rows.append({'owner': owner, 'folder': folder, 'package': package, 'value': value})
        (directory / owner / 'empty').mkdir()
        marker = directory / owner / 'build'
        marker.mkdir()
        (marker / 'Inventory.kt').write_text('// inventory only\n')
        (directory / owner / 'descriptor.xml').write_text('<fixture/>\n')
    (runner.root / '.git').mkdir()
    for folder, name in [('', 'analysis-root'), ('scope_', 'selected-module'),
                         ('scope_/nested', 'nested-artifact')]:
        (runner.root / folder / 'pom.xml').write_text(
            '<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId>'
            f'<artifactId>{name}</artifactId><version>1</version></project>\n')
    # Compiler validation is independent of the native parser/database.
    classes = directory / 'javac'
    sources = [directory / row['owner'] / row['folder'] / name
               for row in rows for name in ('ScopeProbe.java', 'ScopeProbeTest.java')]
    with (directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(classes), *map(str, sources)],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('analysis fixture javac validation failed; see private log')
    for owner, count, xml in [('project', 10, 4), ('attached', 2, 1)]:
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' "
                                           "AND extension IN ('.java','.kt','.xml') GROUP BY extension"))
            want = {'.java': count, '.kt': 1, '.xml': xml}
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, want, inventory)
            if inventory != want:
                raise ToolError('analysis fixture full inventory incomplete')
        finally:
            state.close()
    runner.command('rebuild', '--force')
    runner.json('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force')

    def identity(row):
        return runner.path(row['path']), row['name'], row['kind'], row['line']

    scopes = [('all', [], {'project', 'attached'}, ''),
              ('local', ['--local'], {'project'}, ''),
              ('attached', ['--subtree', 'attached-label'], {'attached'}, ''),
              ('missing-root', ['--subtree', 'absent'], set(), ''),
              ('underscore', [], {'project', 'attached'}, 'scope_/'),
              ('local-underscore', ['--local'], {'project'}, 'scope_/'),
              ('attached-underscore', ['--subtree', 'attached-label'], {'attached'}, 'scope_/'),
              ('percent', [], {'project', 'attached'}, 'scope%/'),
              ('case', [], {'project', 'attached'}, 'CAPS/'),
              ('empty', [], {'project', 'attached'}, 'empty/')]
    # Native nested module aliases are directory-derived, whereas the root
    # Maven module uses its artifact name. A dotted alias differs from a path.
    modules = [(None, ''), ('', ''), ('scope_/', 'scope_/'), ('scope_.nested', 'scope_/nested/'),
               ('analysis-root', ''), ('scope%', 'scope%'), ('CAPS/', 'CAPS/'),
               ('caps/', 'caps/'), ('missing/', 'missing/'), (':scope_:nested', 'scope_/nested/')]
    for label, flags, owners, prefix in scopes:
        selected = [r for r in rows if r['owner'] in owners and r['folder'].startswith(prefix.rstrip('/'))]
        cwd = runner.root / prefix
        for module, module_prefix in modules:
            scoped = [r for r in selected if (r['folder'] + '/').startswith(module_prefix)]
            for exports in (False, True):
                candidates = []
                for row in scoped:
                    path = row['owner'] + '/' + row['folder'] + '/'
                    candidates.extend([(path + 'ScopeProbe.java', 'ScopeProbe', 'class', 2),
                                       (path + 'ScopeProbe.java', 'UpperUnused', 'function', 4),
                                       (path + 'ScopeProbeTest.java', 'ScopeProbeTest', 'class', 2)])
                    if not exports:
                        candidates.append((path + 'ScopeProbe.java', 'consumer', 'function', 6))
                candidates.sort(key=lambda r: (r[0].split('/', 1)[1], r[3], r[0].split('/', 1)[0]))
                for limit in (0, 1, 100):
                    args = [*flags, 'unused-symbols', '--limit', limit]
                    if module is not None:
                        args += ['--module', module]
                    if exports:
                        args += ['--export-only']
                    doc = runner.json(*args, cwd=cwd)
                    _, text = runner.command(*args, cwd=cwd)
                    text_rows = [(runner.path(p), name, kind, int(line)) for name, kind, p, line in
                                 re.findall(r'^  (.+) \[([^]]+)\]: (.+\.java):(\d+)$', text, re.M)]
                    header = re.search(r'\((\d+)/(\d+) checked\):', text)
                    key = f'{label}:{module}:{exports}:{limit}'
                    record(ANALYSIS, key, {'json': candidates[:limit], 'text': candidates[:limit], 'ansi': False,
                                          'checked': [min(limit, len(candidates)), len(scoped) * (3 if exports else 6)]},
                           {'json': [identity(r) for r in doc], 'text': text_rows, 'ansi': '\x1b' in text,
                            'checked': list(map(int, header.groups())) if header else None})

    # Exploration exposes cwd/root selectors only. Test both lexical caller
    # fallback and a fresh graph, without inventing file/module CLI switches.
    for built in (False, True):
        if built:
            runner.command('graph', 'build')
        for label, flags, owners, prefix in scopes:
            selected = [r for r in rows if r['owner'] in owners and (r['folder'] + '/').startswith(prefix)]
            for rwr in (False, True):
                for limit in (0, 1, 100):
                    args = [*flags, 'explore', 'signal', '--max-files', limit, *(['--rwr'] if rwr else [])]
                    doc = runner.json(*args, cwd=runner.root / prefix)
                    symbols = [(r['owner'] + '/' + r['folder'] + '/ScopeProbe.java',
                                r['package'] + '.ScopeProbe.signal', 'function', 3) for r in selected]
                    # The indexed signature contains the same-line call, so
                    # consumer is also a lexical seed without graph expansion.
                    consumers = [(r['owner'] + '/' + r['folder'] + '/ScopeProbe.java',
                                  r['package'] + '.ScopeProbe.consumer', 'function', 6) for r in selected]
                    neighbours = consumers if rwr else []
                    paths = {r[0] for r in symbols}
                    bodies = {r['owner'] + '/' + r['folder'] + '/ScopeProbe.java':
                              f"int signal() {{ return {r['value']}; }}" for r in selected}
                    file_paths = [runner.path(r['path']) for r in doc['files']]
                    tests = [(runner.path(r['source']), [runner.path(p) for p in r['tests']]) for r in doc['tests']]
                    got_symbols = [identity(r) for r in doc['symbols']]
                    got_neighbours = [identity(r) for r in doc['neighbours']]
                    _, text = runner.command(*args, cwd=runner.root / prefix)
                    text_symbols = [(runner.path(p), name, kind, int(line)) for name, kind, p, line in
                                    re.findall(r'^  (.+?) \[([^]]+)\]  (.+\.java):(\d+)  score=', text, re.M)]
                    record(EXPLORE, f'{built}:{label}:{rwr}:{limit}',
                           {'symbols': sorted(symbols + consumers), 'neighbours': sorted(neighbours),
                            'roles': ['caller'] * len(neighbours), 'text': sorted(symbols + consumers),
                            'files': min(limit, len(paths)), 'ownership': True, 'ansi': False},
                           {'symbols': sorted(got_symbols), 'neighbours': sorted(got_neighbours),
                            'roles': sorted(r['link'] for r in doc['neighbours']), 'text': sorted(text_symbols),
                            'files': len(file_paths), 'ownership': len(set(file_paths)) == len(file_paths)
                            and set(file_paths) <= paths
                            and (limit < len(paths) or set(file_paths) == paths)
                            and all(bodies.get(runner.path(r['path']), '\0') in r.get('source', '')
                                    for r in doc['files'])
                            and sorted(tests) == sorted((p, [p.replace('ScopeProbe.java', 'ScopeProbeTest.java')])
                                                         for p in file_paths), 'ansi': '\x1b' in text})
    # Declaration scoping must not ignore users in another directory. This
    # guard preserves the documented name-reference heuristic explicitly.
    destination = runner.root / 'scope_' / 'ScopeProbe.java'
    destination.write_text(destination.read_text().replace('\n}\n', '\n void externalUsed() {}\n}\n'))
    outside = runner.root / 'outside'
    outside.mkdir()
    (outside / 'User.java').write_text('package fixture.project.p0;\n'
                                     'class User { void invoke(ScopeProbe probe) { probe.externalUsed(); } }\n')
    with (directory / 'javac.log').open('ab') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(classes),
                                 *map(str, sources), str(outside / 'User.java')],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('analysis cross-directory fixture javac validation failed; see private log')
    runner.command('rebuild', '--force')
    record(ANALYSIS, 'users-outside-declaration-scope',
           [('project/scope_/ScopeProbe.java', 'UpperUnused', 'function', 4),
            ('project/scope_/ScopeProbe.java', 'consumer', 'function', 6),
            ('project/scope_/ScopeProbeTest.java', 'ScopeProbeTest', 'class', 2)],
           [identity(r) for r in runner.json('--local', 'unused-symbols', '--module', 'scope_/ScopeProbe',
                                           '--limit', 100, cwd=runner.root / 'scope_')])
    return expected, actual
