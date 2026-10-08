"""Authored Java Android dependency ownership; independent source, never MCP truth."""
from itertools import product
from pathlib import Path
import re
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from android_contracts import observation
from unused_dep_contracts import result

LEGACY_FEATURES = {'unused-deps:android-ownership', 'resource-usages:xml-namespace-ownership'}
JAVA_FEATURES = {'unused-deps:java-android-ownership', 'resource-usages:java-namespace-ownership'}
FEATURES = LEGACY_FEATURES
REASON = ('independent source/state: disposable Java Android qualified/nested XML classes, '
          'literal resource namespaces, configuration variants, module collisions and option rendering; '
          'not MCP equivalence or compiler-wide/merged resource resolution')


JAVA_REASON = ('independent source/state: executed Java projection of legacy Android ownership; '
               'qualified Java resource locations, Java class ownership from simple layout inputs, '
               'declaring-module unused/dependency ownership '
               'and option rendering; XML-only criteria explicitly out-of-scope; not MCP equivalence')


def plan_dependencies(state, root, java_only=False):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES | JAVA_FEATURES):
            subject = 'disposable-java-android-dependency-ownership'
            # Scope migration preserves established identities even in a
            # fresh epoch. Excluded rows stay unobserved, never fake passes.
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
            if java_only and feature in LEGACY_FEATURES:
                state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                              (feature, 'out-of-scope', 'legacy mixed case retained unchanged; XML criteria out-of-scope; Java criteria executed under ' + ', '.join(sorted(JAVA_FEATURES))))
                continue
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', JAVA_REASON if feature in JAVA_FEATURES else REASON))


def dependency_observation(output, verbose):
    """Check rendered dependency identities and sample counts, not just summary totals."""
    value = result(output, verbose)
    for section in ('XML', 'Resource'):
        body = output.split(f'=== {section} Usage ===', 1)[-1].split('===', 1)[0] if verbose else ''
        value[section] = sorted((name, int(count)) for name, count in
                               re.findall(r'^  ✓ (.+) - (\d+) usages$', body, re.M))
        value[section + '-samples'] = sorted(re.findall(r'^    └─ (.+)$', body, re.M))
    return value


def exercise_legacy(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Android dependency artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-android-deps-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in LEGACY_FEATURES} for _ in range(2))

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


def exercise_java(binary, base):
    """Execute retained Java criteria without evaluating XML reference syntax."""
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Android dependency artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-android-owner-projection-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        expected, actual = ({feature: {} for feature in JAVA_FEATURES} for _ in range(2))
        deps = ['views', 'wrongviews', 'reslib', 'decoy', 'dead', 'views/child']
        for module in ('app', 'app/child', 'appExtra', *deps):
            path = runner.root / module
            path.mkdir(parents=True, exist_ok=True)
            (path / 'build.gradle').write_text("plugins { id 'com.android.library' }\n"
                + f"android {{ namespace 'fixture.{module.replace('/', '.')}' }}\n"
                + ('dependencies { ' + ''.join('implementation(project(":' + dep.replace('/', ':') + '")); ' for dep in deps) + '}' if module == 'app' else ''))
            if module in ('app', 'reslib', 'decoy', 'dead'):
                values = path / 'src/main/res/values'
                values.mkdir(parents=True)
                (values / 'strings.xml').write_text('<resources><string name="shared">Value</string></resources>')
        for module, source in {
            'views': 'package fixture.views; class Widget {} class Outer { static class Inner {} }',
            'wrongviews': 'package fixture.wrong; class Widget {}',
            'views/child': 'package fixture.child; class Child {}',
        }.items():
            (runner.root / module / 'Use.java').write_text(source + '\n')
        layout = runner.root / 'app/src/main/res/layout/screen.xml'
        layout.parent.mkdir(parents=True)
        layout.write_text('<root>\n<fixture.views.Widget/>\n<view class="fixture.views.Outer$Inner"/>\n<fixture.child.Child/>\n</root>')
        for module in ('app/child', 'appExtra'):
            child_layout = runner.root / module / 'src/main/res/layout/other.xml'
            child_layout.parent.mkdir(parents=True)
            child_layout.write_text('<fixture.wrong.Widget/>')
        # Exact retained Java site from the legacy shared-resource assertion.
        (runner.root / 'app/Use.java').write_text('package fixture.app; class Use { int value = fixture.reslib.R.string.shared; }\n')
        variant = runner.root / 'reslib/src/main/res/values-fr/strings.xml'
        variant.parent.mkdir(parents=True)
        variant.write_text('<resources><string name="shared">Variant</string></resources>')
        runner.command('rebuild', '--force', '--max-files', 0)
        _, output = runner.command('resource-usages', '@string/shared', '--module', 'app')
        feature = 'resource-usages:java-namespace-ownership'
        expected[feature]['shared-java'] = {**observation(''), 'locations': [('app/Use.java', 1)],
                                           'groups': [('Kotlin/Java', 1)], 'total': 1}
        actual[feature]['shared-java'] = observation(output)
        for module in ('app', 'reslib', 'decoy', 'dead'):
            _, output = runner.command('resource-usages', '--unused', '--module', module)
            unused = [] if module == 'reslib' else ['string/shared']
            if module == 'app':
                unused = ['layout/screen', *unused]
            expected[feature]['unused:' + module] = {**observation(''), 'unused': unused, 'unused_total': len(unused)}
            actual[feature]['unused:' + module] = observation(output)
        for xml, resources, verbose in product((False, True), repeat=3):
            flags = (() if xml else ('--no-xml',)) + (() if resources else ('--no-resources',)) + (('--verbose',) if verbose else ())
            _, output = runner.command('unused-deps', 'app', *flags)
            used = (['views', 'views.child'] if xml else []) + (['reslib'] if resources else [])
            value = {'summary': [6 - len(used), 0, len(used), 6],
                     'unused': sorted(set(dep.replace('/', '.') for dep in deps) - set(used)),
                     'exported': [], 'direct': [], 'via': [], 'sections': {'Direct': 0, 'Transitive': 0, **({'XML': 2} if xml else {}), **({'Resources': 1} if resources else {})},
                     'strict': False, 'transitive_section': verbose,
                     'XML': [('views', 2), ('views.child', 1)] if xml and verbose else [],
                     'XML-samples': ['Child:4', 'Inner:3', 'Widget:2'] if xml and verbose else [],
                     'Resource': [('reslib', 1)] if resources and verbose else [],
                     'Resource-samples': ['@string/shared (code)'] if resources and verbose else []}
            label = str((xml, resources, verbose))
            expected['unused-deps:java-android-ownership'][label] = value
            actual['unused-deps:java-android-ownership'][label] = dependency_observation(output, verbose)
        for label, flags, used, strict in (
                ('strict:default-switches', ('--strict', '--verbose'), [], True),
                ('no-transitive:with-java-class-tags', ('--no-transitive', '--verbose'),
                 ['views', 'views.child', 'reslib'], False)):
            _, output = runner.command('unused-deps', 'app', *flags)
            wanted = {'summary': [6 - len(used), 0, len(used), 6],
                      'unused': sorted(set(dep.replace('/', '.') for dep in deps) - set(used)),
                      'exported': [], 'direct': [], 'via': [],
                      'sections': {'Direct': 0, **({'XML': 2, 'Resources': 1} if used else {})},
                      'strict': strict, 'transitive_section': False,
                      'XML': [('views', 2), ('views.child', 1)] if used else [],
                      'XML-samples': ['Child:4', 'Inner:3', 'Widget:2'] if used else [],
                      'Resource': [('reslib', 1)] if used else [],
                      'Resource-samples': ['@string/shared (code)'] if used else []}
            expected['unused-deps:java-android-ownership'][label] = wanted
            actual['unused-deps:java-android-ownership'][label] = dependency_observation(output, True)
        # Retain the existing keys/expectations for these direct-Java controls.
        # The separately named variants above must not be overwritten: they
        # preserve the no-transitive Java-class-tag ownership assertion too.
        for label, flags, source_key, strict in (
                ('strict', ('--strict', '--verbose'), str((False, False, True)), True),
                ('no-transitive', ('--no-transitive', '--no-xml', '--verbose'),
                 str((False, True, True)), False)):
            wanted = dict(expected['unused-deps:java-android-ownership'][source_key])
            wanted['sections'] = {key: count for key, count in wanted['sections'].items()
                                  if key != 'Transitive'}
            wanted['strict'], wanted['transitive_section'] = strict, False
            _, output = runner.command('unused-deps', 'app', *flags)
            expected['unused-deps:java-android-ownership'][label] = wanted
            actual['unused-deps:java-android-ownership'][label] = dependency_observation(output, True)
        # Retained Java negative criterion: ambiguous namespaces do not own
        # generated references even when a local same-named definition exists.
        (runner.root / 'decoy/build.gradle').write_text("plugins { id 'com.android.library' }\nandroid { namespace 'fixture.reslib' }")
        runner.command('rebuild', '--force', '--max-files', 0)
        _, output = runner.command('resource-usages', '@string/shared', '--module', 'app')
        expected[feature]['ambiguous-java'] = {**observation(''), 'total': 0}
        actual[feature]['ambiguous-java'] = observation(output)
        return expected, actual


def exercise(binary, base):
    expected, actual = exercise_legacy(binary, base)
    java_expected, java_actual = exercise_java(binary, base)
    return {**expected, **java_expected}, {**actual, **java_actual}
