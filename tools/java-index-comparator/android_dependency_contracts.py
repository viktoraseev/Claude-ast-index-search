"""Authored Java Android dependency ownership; independent source, never MCP truth."""
from itertools import product
from pathlib import Path
import re
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from android_contracts import observation
from unused_dep_contracts import result

FEATURES = {'unused-deps:android-ownership', 'resource-usages:xml-namespace-ownership'}
REASON = ('independent source/state: disposable Java Android qualified/nested XML classes, '
          'literal resource namespaces, configuration variants, module collisions and option rendering; '
          'not MCP equivalence or compiler-wide/merged resource resolution')


def plan_dependencies(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-android-dependency-ownership'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def dependency_observation(output, verbose):
    """Check rendered dependency identities and sample counts, not just summary totals."""
    value = result(output, verbose)
    for section in ('XML', 'Resource'):
        body = output.split(f'=== {section} Usage ===', 1)[-1].split('===', 1)[0] if verbose else ''
        value[section] = sorted((name, int(count)) for name, count in
                               re.findall(r'^  ✓ (.+) - (\d+) usages$', body, re.M))
        value[section + '-samples'] = sorted(re.findall(r'^    └─ (.+)$', body, re.M))
    return value


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Android dependency artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-android-deps-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

        def write(path, source):
            destination = runner.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source)

        deps = ['views', 'wrongviews', 'reslib', 'decoy', 'dead', 'views/child']
        for module in ['app', 'app/child', 'appExtra', *deps]:
            namespace = 'fixture.' + module.replace('/', '.')
            edges = deps if module == 'app' else []
            write(module + '/build.gradle', "plugins { id 'com.android.library' }\n"
                  + f"android {{ namespace '{namespace}' }}\n" + 'dependencies {\n'
                  + ''.join(f' implementation(project(":{dep.replace("/", ":")}"))\n' for dep in edges) + '}\n')
        for module, source in {
            'app': 'package fixture.app; class Use { int value = fixture.reslib.R.string.shared; }',
            'views': 'package fixture.views; class Widget {} class Outer { static class Inner {} }',
            'wrongviews': 'package fixture.wrong; class Widget {}',
            'views/child': 'package fixture.child; class Child {}',
            'reslib': 'package fixture.reslib; class Sentinel {}',
            'decoy': 'package fixture.decoy; class Sentinel {}',
            'dead': 'package fixture.dead; class Dead {}',
        }.items():
            write(module + '/Use.java', source + '\n')
        for module in ('app', 'reslib', 'decoy'):
            write(module + '/src/main/res/values/strings.xml',
                  '<resources><string name="shared">Value</string></resources>\n')
        write('reslib/src/main/res/values-fr/strings.xml',
              '<resources><string name="shared">Variant</string></resources>\n')
        write('reslib/src/main/res/values/unique.xml',
              '<resources><string name="unique">Value</string></resources>\n')
        write('dead/src/main/res/values/strings.xml',
              '<resources><string name="child_only">Value</string></resources>\n')
        layout = 'app/src/main/res/layout/screen.xml'
        write(layout, '''<root>
<fixture.views.Widget/>
<view class="fixture.views.Outer$Inner"/>
<fixture.child.Child/>
<text title="@fixture.reslib:string/shared"/>
<text title="&#64;fixture.reslib:string/shared"/>
<text title="@string/unique"/>
<text title="@string/shared"/>
<text title="@missing.namespace:string/shared"/>
<text title="@android:string/shared"/>
<!-- <fixture.wrong.Widget title="@fixture.decoy:string/shared"/> -->
<![CDATA[<fixture.wrong.Widget title="@fixture.decoy:string/shared"/>]]>
</root>
''')
        for module in ('app/child', 'appExtra'):
            write(module + '/src/main/res/layout/other.xml',
                  '<fixture.wrong.Widget title="@string/child_only"/>\n')
        runner.command('rebuild', '--force', '--max-files', 0)

        def dependency_sample(label, xml, resources, verbose, flags, resource_count=4):
            _, output = runner.command('unused-deps', 'app', *flags)
            used = (['views', 'views.child'] if xml else []) + (['reslib'] if resources else [])
            sections = {'Direct': 0}
            if '--no-transitive' not in flags and '--strict' not in flags:
                sections['Transitive'] = 0
            if xml:
                sections['XML'] = 2
            if resources:
                sections['Resources'] = 1
            wanted = {'summary': [6 - len(used), 0, len(used), 6],
                      'unused': sorted(set(dep.replace('/', '.') for dep in deps) - set(used)),
                      'exported': [], 'direct': [], 'via': [], 'sections': sections,
                      'strict': '--strict' in flags or all(flag in flags for flag in
                          ('--no-transitive', '--no-xml', '--no-resources')),
                      'transitive_section': verbose and '--no-transitive' not in flags and '--strict' not in flags,
                      'XML': [('views', 2), ('views.child', 1)] if xml and verbose else [],
                      'XML-samples': ['Child:4', 'Inner:3', 'Widget:2'] if xml and verbose else [],
                      'Resource': [('reslib', resource_count)] if resources and verbose else [],
                      'Resource-samples': ['@string/shared (code)', '@string/unique (xml)'] if resources and verbose else []}
            expected['unused-deps:android-ownership'][label] = wanted
            actual['unused-deps:android-ownership'][label] = dependency_observation(output, verbose)

        # Defaults, independent switches, strict mode and rendering share one
        # authored oracle. Generated R classes are absent from indexed source.
        for xml, resources, verbose in product((False, True), repeat=3):
            flags = tuple(flag for enabled, flag in ((xml, '--no-xml'), (resources, '--no-resources')) if not enabled)
            if verbose:
                flags += ('--verbose',)
            dependency_sample(str((xml, resources, verbose)), xml, resources, verbose, flags)
        dependency_sample('strict', False, False, True, ('--strict', '--verbose'))
        dependency_sample('no-transitive', True, True, True, ('--no-transitive', '--verbose'))

        for query, lines in [('shared', [5, 6, 8]), ('unique', [7])]:
            _, output = runner.command('resource-usages', '@string/' + query, '--module', 'app')
            code = [('app/Use.java', 1)] if query == 'shared' else []
            expected['resource-usages:xml-namespace-ownership'][query] = {
                **observation(''), 'locations': sorted(code + [(layout, n) for n in lines]),
                'groups': sorted(([('Kotlin/Java', 1)] if code else []) + [('XML', len(lines))]),
                'total': len(code) + len(lines)}
            actual['resource-usages:xml-namespace-ownership'][query] = observation(output)

        # Isolate configuration deduplication from qualified XML resolution;
        # otherwise two defects can accidentally cancel the usage total.
        write(layout, '<root title="@string/unique"/>\n')
        runner.command('rebuild', '--force', '--max-files', 0)
        dependency_sample('configuration-variants', False, True, True,
                          ('--no-xml', '--verbose'), resource_count=2)

        # Duplicate literal namespaces are ambiguous; never fall back to a
        # colliding local or unrelated resource. Unknown/framework names also
        # remain unresolved, without fabricating a native usage.
        write('decoy/build.gradle', "plugins { id 'com.android.library' }\nandroid { namespace 'fixture.reslib' }\n")
        write(layout, '<root title="@fixture.reslib:string/shared" local="@string/shared"/>\n')
        runner.command('rebuild', '--force', '--max-files', 0)
        _, output = runner.command('resource-usages', '@string/shared', '--module', 'app')
        expected['resource-usages:xml-namespace-ownership']['ambiguous-namespace'] = {
            **observation(''), 'locations': [(layout, 1)], 'groups': [('XML', 1)], 'total': 1}
        actual['resource-usages:xml-namespace-ownership']['ambiguous-namespace'] = observation(output)
        return expected, actual
