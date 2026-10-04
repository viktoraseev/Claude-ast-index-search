"""Java file-view root selection; authored source/state, never MCP equivalence."""
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner
from file_view_contracts import document, outline


FEATURES = {'global:scope:java-file-views'}
REASON = ('independent source/state: disposable Java file discovery, outline/import '
          'selection and public API across colliding attached roots, module paths, '
          'scope flags, limits and text/JSON rendering; not MCP equivalence')
SOURCE = '''package fixture.{name};
import java.util.{import_name};
public class View {{
    public void {name}() {{}}
}}
'''


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-file-view-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      ('Java file/outline/imports/API root selection has a separate executed contract; '
                       'combined navigation path filters and module/map/analysis/graph/conventions/'
                       'explore scope remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('file scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='file-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    sources = {}
    for root, name, imported in [('project', 'primary', 'List'), ('attached', 'attached', 'Set'),
                                  ('other', 'other', 'Map')]:
        folder = directory / root / 'src' / 'views'
        folder.mkdir(parents=True)
        path = f'{root}/src/views/View.java'
        sources[path] = SOURCE.format(name=name, import_name=imported)
        (directory / path).write_text(sources[path])
    (runner.root / '.git').mkdir()
    (runner.root / 'pom.xml').write_text('<project><artifactId>root-module</artifactId></project>')
    # Other directories must not leak into a selector sharing a string prefix.
    (runner.root / 'src' / 'views-extra').mkdir()
    (runner.root / 'src/views-extra/Leak.java').write_text('public class Leak {}\n')
    sources['attached/Only.java'] = 'public class Only {}\n'
    (directory / 'attached/Only.java').write_text(sources['attached/Only.java'])
    expected, actual = {}, {}

    def run(format, flags, *args):
        _, output = runner.command('--format', format, *flags, *args)
        return output

    # Preserve source-only behavior, without manufacturing an index requirement.
    for command in ('outline', 'imports', 'api'):
        argument = 'src/views' if command == 'api' else 'src/views/View.java'
        output = document(run('json', [], command, argument))
        expected[f'no-index:{command}'] = False
        actual[f'no-index:{command}'] = 'skipped' in output
    runner.command('rebuild', '--force')
    for name in ('attached', 'other'):
        runner.json('subtree', 'add', name, '../' + name)
    runner.command('rebuild', '--force')
    # Full file-type inventory is private; this authored applicable family may
    # never be skipped because an empty command or a missing oracle looks valid.
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        for name in ('project', 'attached', 'other'):
            mobile_contracts.inventory(state, directory / name)
            expected['inventory:' + name] = True
            actual['inventory:' + name] = state.execute(
                "SELECT 1 FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone() is not None
    finally:
        state.close()
    scopes = [('all', [], ['project', 'attached', 'other']),
              ('local', ['--local'], ['project']),
              ('attached', ['--subtree', 'attached'], ['attached']),
              ('other', ['--subtree', 'other'], ['other']),
              ('absent', ['--subtree', 'absent'], [])]
    for format in ('json', 'text'):
        for label, flags, roots in scopes:
            candidates = sorted(f'{r}/src/views/View.java' for r in roots)
            for limit in (0, 1, 100):
                output = run(format, flags, 'file', 'View.java', '--limit', limit)
                files = document(output) if format == 'json' else [line[2:] for line in output.splitlines()
                    if line.startswith('  ') and line.strip() != 'No files found.']
                paths = [runner.path(p) for p in files]
                key = f'{format}:{label}:file:{limit}'
                expected[key] = {'count': min(limit, len(candidates)), 'valid': True, 'complete': True}
                actual[key] = {'count': len(paths), 'valid': len(set(paths)) == len(paths) and set(paths) <= set(candidates),
                               'complete': limit < len(candidates) or sorted(paths) == candidates}
            # Relative single-file views select the primary in the default
            # scope; --subtree disambiguates the same relative path explicitly.
            selected = roots[0] if roots else None
            for argument, owner in [('src/views/View.java', selected),
                                    (str(directory / 'attached/src/views/View.java'),
                                     'attached' if 'attached' in roots else None),
                                    ('src/views/Missing.java', None)]:
                key = f'{format}:{label}:{argument}'
                output = run(format, flags, 'outline', argument)
                rows = [('View', 'class', 3, 5), (owner if owner != 'project' else 'primary', 'function', 4, 4)] if owner else []
                expected[key + ':outline'] = ({'file': argument, 'schema_version': 1,
                    'skipped': None if owner else 'not_found', 'rows': rows} if format == 'json' else rows)
                actual[key + ':outline'] = outline(output, format)
                imported = {'project': 'List', 'attached': 'Set', 'other': 'Map'}.get(owner)
                names = ['java.util.' + imported] if owner else []
                output = run(format, flags, 'imports', argument)
                expected[key + ':imports'] = ({'file': argument, 'imports': names, 'count': len(names),
                    **({} if owner else {'skipped': 'not_found'})} if format == 'json' else
                    (f'Imports in {argument}:\n  {names[0]};\n\n  Total: 1 imports\n' if owner else
                     f'File not found: {argument}\n'))
                actual[key + ':imports'] = document(output) if format == 'json' else output
            # Relative module selectors aggregate the selected roots before
            # applying limits. Absolute selectors intersect, never override flags.
            for selector, wanted in [('src/views', candidates), ('src.views', candidates),
                                     ('src/./views', candidates),
                                     ('src/views/View.java', candidates),
                                     (str(directory / 'attached/src/views'),
                                      ['attached/src/views/View.java'] if 'attached' in roots else []),
                                     ('missing', [])]:
                for limit in (0, 1, 100):
                    key = f'{format}:{label}:api:{selector}:{limit}'
                    items = [(p, n, sources[p].splitlines()[n - 1].strip())
                             for p in sorted(wanted) for n in (3, 4)]
                    output = run(format, flags, 'api', selector, '--limit', limit)
                    if format == 'json':
                        doc = document(output)
                        observed = [(runner.path(r['path']), r['line'], r['content']) for r in doc['items']]
                        skipped = doc.get('skipped')
                        actual[key + ':count'] = doc['count']
                        expected[key + ':count'] = min(limit, len(items))
                    else:
                        observed = [(runner.path(p), int(n), content) for p, n, content in
                            re.findall(r'^  (.+):(\d+)\n    (.*)$', output, re.MULTILINE)]
                        skipped = 'not_found' if output.startswith('Module not found:') else None
                    expected[key] = {'rows': items[:limit], 'skipped': None if wanted else 'not_found'}
                    actual[key] = {'rows': observed, 'skipped': skipped}
        # Unqualified fallback is allowed for a unique attached file, never for
        # an ambiguous attached collision after the primary file disappears.
        for command in ('outline', 'imports'):
            output = document(run('json', [], command, 'Only.java'))
            expected[f'{format}:unique:{command}'] = False
            actual[f'{format}:unique:{command}'] = 'skipped' in output
    # A primary DB module alias retains its owner, rather than inheriting an
    # attached directory just because its relative path collides.
    for label, flags, roots in scopes:
        doc = document(run('json', flags, 'api', 'root-module', '--limit', 100))
        key = 'module-alias:' + label
        expected[key] = sorted([('project/src/views-extra/Leak.java', 1),
                                ('project/src/views/View.java', 3),
                                ('project/src/views/View.java', 4)]) if 'project' in roots else []
        actual[key] = sorted((runner.path(row['path']), row['line']) for row in doc['items'])
    # Forced overlaps have a most-specific owner. Primary traversal must not
    # include that attachment, and combined traversal must not duplicate it.
    inner = runner.root / 'inner'
    inner.mkdir()
    (inner / 'Inner.java').write_text('import java.util.List;\npublic class Inner {}\n')
    runner.json('subtree', 'add', 'inner', 'inner', '--force')
    runner.command('rebuild', '--force')
    for label, flags, wanted in [('all', [], True), ('local', ['--local'], False),
                                  ('inner', ['--subtree', 'inner'], True),
                                  ('attached', ['--subtree', 'attached'], False)]:
        file = str(inner / 'Inner.java')
        for command in ('outline', 'imports'):
            doc = document(run('json', flags, command, file))
            expected[f'overlap:{label}:{command}'] = not wanted
            actual[f'overlap:{label}:{command}'] = 'skipped' in doc
        doc = document(run('json', flags, 'api', str(inner), '--limit', 100))
        expected[f'overlap:{label}:api'] = [('project/inner/Inner.java', 2)] if wanted else []
        actual[f'overlap:{label}:api'] = [(runner.path(row['path']), row['line']) for row in doc['items']]
    # A symlink cannot override the selected root's canonical boundary.
    link = runner.root / 'Cross.java'
    link.symlink_to(directory / 'attached/src/views/View.java')
    for command in ('outline', 'imports', 'api'):
        doc = document(run('json', ['--local'], command, str(link)))
        expected['symlink-scope:' + command] = 'not_found'
        actual['symlink-scope:' + command] = doc.get('skipped')
    (runner.root / 'src/views/View.java').unlink()
    for command in ('outline', 'imports'):
        code, _ = runner.command('--format', 'json', command, 'src/views/View.java', acceptable=(0, 1))
        expected['ambiguous:' + command] = 1
        actual['ambiguous:' + command] = code
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
