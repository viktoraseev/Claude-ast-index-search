"""Java resource/class-reference ownership selectors; XML syntax is excluded."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from android_contracts import observation
from root_contracts import Runner

FEATURE = 'global:scope:java-resources'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-resource-scope'
REASON = ('independent source/state and javac: Java R references and Java class layout '
          'reference ownership across colliding attached roots; cwd/module/root intersections '
          'before counts/caps, unused-definition scope and external-scope use guards; '
          'XML-only syntax and foreign parser contracts excluded; not MCP equivalence')


def plan_scope(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('Java resource scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-resource-scope-', dir=base) as temporary:
        directory = Path(temporary).resolve()
        runner = Runner(binary, directory)
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        sources, entries = [], []

        def write(owner, path, content):
            file = directory / owner / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
            return file

        paths = ['scope_/app', 'scopeX/app', 'scope%/app', 'ScopeCase/app']
        for owner in ('project', 'attached'):
            for ordinal, module in enumerate(paths):
                package = f'fixture.{owner}.m{ordinal}'
                write(owner, module + '/build.gradle',
                      "plugins { id 'com.android.library' }\nandroid { namespace '" + package + "' }\n" +
                      ('dependencies { implementation(project(":scopeX:app")) }\n' if ordinal == 0 else ''))
                sources.append(write(owner, module + '/src/main/java/Widget.java',
                                     f'package {package}; public class Widget {{}}\n'))
                sources.append(write(owner, module + '/src/main/java/Use.java',
                                     f'package {package}; class Use {{\n' +
                                     ''.join(f' int v{i}=R.string.shared;\n' for i in range(12)) +
                                     (f' int cross=fixture.{owner}.m1.R.string.shared;\n'
                                      f' int external=fixture.{owner}.m1.R.string.external_only;\n'
                                      if ordinal == 0 else '') + '}\n'))
                # Generated Android R is a compilation input outside indexed
                # roots, so a handwritten source R cannot change applicability.
                sources.append(write('compiler', f'{owner}/{ordinal}/R.java',
                                     f'package {package}; public class R {{ public static class string {{'
                                     'public static final int shared=1,external_only=2; } }\n'))
                write(owner, module + '/src/main/res/values/strings.xml',
                      '<resources><string name="shared">Value</string><string name="unused">Value</string>' +
                      ('<string name="external_only">Value</string>' if ordinal == 1 else '') + '</resources>\n')
                layout = module + '/src/main/res/layout/view.xml'
                write(owner, layout, f'<{package}.Widget/>\n' * (102 if ordinal == 0 else 1))
                entries.append((owner, module, layout, module + '/src/main/java/Use.java', 102 if ordinal == 0 else 1))
            write(owner, 'Inventory.kt', '// inventory only\n')
            state = connect(directory / (owner + '-inventory.sqlite'))
            try:
                state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
                mobile_contracts.inventory(state, directory / owner)
                inventory = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
                if inventory != {'.java': 8, '.gradle': 4, '.xml': 8, '.kt': 1}:
                    raise ToolError('Java resource scope complete inventory differs')
            finally:
                state.close()
        (runner.root / '.git').mkdir()
        (runner.root / 'empty').mkdir()
        with (directory / 'javac.log').open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'), *map(str, sources)],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('Java resource scope sources rejected; see private javac log')
        runner.command('rebuild', '--force', '--max-files', 0)
        runner.command('subtree', 'add', 'attached', '../attached')
        runner.command('rebuild', '--force', '--max-files', 0)
        expected, actual = {}, {}

        def record(key, want, got):
            expected[key], actual[key] = want, got

        record('applicable-inventory', {'.java': 8, '.gradle': 4, '.xml': 8, '.kt': 1}, inventory)

        def module_name(owner, path):
            return ('' if owner == 'project' else 'attached::') + path.replace('/', '.')

        def stored(owner, path):
            return path if owner == 'project' else str(directory / owner / path)

        scopes = [('all', [], ['project', 'attached'], ''),
                  ('local', ['--local'], ['project'], ''),
                  ('attached', ['--subtree', 'attached'], ['attached'], ''),
                  ('absent', ['--subtree', 'absent'], [], ''),
                  ('cwd', [], ['project', 'attached'], 'scope_/'),
                  ('cwd-local', ['--local'], ['project'], 'scope_/'),
                  ('cwd-attached', ['--subtree', 'attached'], ['attached'], 'scope_/'),
                  ('percent', [], ['project', 'attached'], 'scope%/'),
                  ('case', [], ['project', 'attached'], 'ScopeCase/'),
                  ('empty', [], ['project', 'attached'], 'empty/'),
                  ('java-only-dir', [], ['project', 'attached'], 'scope_/app/src/main/java/'),
                  ('definitions-only-dir', [], ['project', 'attached'], 'scope_/app/src/main/res/'),
                  ('library-definitions-only-dir', [], ['project', 'attached'], 'scopeX/app/src/main/res/')]
        for label, flags, owners, prefix in scopes:
            cwd = runner.root / prefix
            for query_module in (None, 'scope_.app', 'attached::scope_.app', 'scopeX.app', 'absent'):
                key = f'{label}:{query_module}'
                chosen = [row for row in entries if row[0] in owners and
                          (query_module is None or module_name(row[0], row[1]) == query_module)]
                class_sites = sorted((stored(o, layout), n, o, layout) for o, _, layout, _, count in chosen
                                     if layout.startswith(prefix) for n in range(1, count + 1))
                # xml-usages advertises a global cap, but a requested exact
                # module returns all of that module's class reference sites.
                class_sites = class_sites if query_module is not None else class_sites[:100]
                module_flags = [] if query_module is None else ['--module', query_module]
                _, output = runner.command(*flags, 'xml-usages', 'Widget', *module_flags, cwd=cwd)
                doc = observation(output)
                record(key + ':java-class-layout-sites',
                       sorted((o + '/' + layout, n) for _, n, o, layout in class_sites),
                       sorted((runner.path(p), n) for p, n in doc['locations']))
                record(key + ':java-class-count', len(class_sites), doc['xml_count'])
                code_sites = sorted((stored(o, code), n, o, code) for o, _, _, code, _ in chosen
                                    if code.startswith(prefix) for n in range(2, 15 if code.startswith('scope_/') else 14))
                _, output = runner.command(*flags, 'resource-usages', 'R.string.shared', *module_flags, cwd=cwd)
                doc = observation(output)
                record(key + ':java-resource-sites',
                       sorted((o + '/' + code, n) for _, n, o, code in code_sites[:10]),
                       sorted((runner.path(p), n) for p, n in doc['locations']))
                record(key + ':java-resource-counts',
                       {'groups': [('Kotlin/Java', len(code_sites))] if code_sites else [],
                        'total': len(code_sites), 'omitted': [len(code_sites) - 10] if len(code_sites) > 10 else []},
                       {k: doc[k] for k in ('groups', 'total', 'omitted')})
                _, output = runner.command(*flags, 'resource-usages', 'shared', '--type', 'color',
                                           *module_flags, cwd=cwd)
                record(key + ':type-intersection', [], observation(output)['locations'])
                if query_module is not None:
                    _, output = runner.command(*flags, 'resource-usages', '--unused', '--module', query_module, cwd=cwd)
                    definitions = any((module + '/src/main/res/').startswith(prefix)
                                      for _, module, _, _, _ in chosen)
                    # Usage outside cwd still keeps the selected shared definition
                    # used. Changing the result scope cannot manufacture unusedness.
                    record(key + ':unused-definition-scope',
                           ['layout/view', 'string/unused'] if definitions else [], observation(output)['unused'])
        layout = 'scope_/app/src/main/res/layout/view.xml'
        write('attached', layout, '<fixture.attached.m0.Widget/>\n' * 2)
        runner.command('update')
        _, output = runner.command('--subtree', 'attached', 'xml-usages', 'Widget', '--module', 'attached::scope_.app')
        doc = observation(output)
        record('update-attached-java-class-sites', [('attached/' + layout, 1), ('attached/' + layout, 2)],
               sorted((runner.path(p), n) for p, n in doc['locations']))
        record('update-attached-java-class-count', 2, doc['xml_count'])
        # Rows remain until an index refresh; detaching must immediately stop
        # treating their occurrence paths as part of the query's source roots.
        runner.command('subtree', 'remove', 'attached')
        _, output = runner.command('xml-usages', 'Widget', '--module', 'attached::scope_.app')
        record('detached-stale-java-class-sites', [], observation(output)['locations'])
        _, output = runner.command('resource-usages', 'shared', '--module', 'attached::scope_.app')
        record('detached-stale-java-resource-sites', [], observation(output)['locations'])
        return {FEATURE: expected}, {FEATURE: actual}
