"""Finite Java resource ownership criteria; authored source/CLI, not MCP truth."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
from android_contracts import observation, applicability, SCHEMA as ANDROID_SCHEMA
import mobile_contracts

FEATURE = 'resource-usages:java-owner-resolution'
FEATURES = {FEATURE}
REASON = ('independent source/javac/CLI: Java resource dependency R modes, literal variable '
          'and manifest namespace metadata, typed definitions and attached declaring roots; '
          'not MCP equivalence; XML-only parsing explicitly out-of-scope')


def plan_ownership(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-resource-owner-resolution-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        note = ('; separate executed Java owner checklist covers explicit transitive/nontransitive '
                'dependency R modes, proven literal variable/manifest metadata, integer/bool/array/plurals '
                'definitions and attached-root declaring identity; this independent source/javac/CLI '
                'coverage does not waive remaining parent obligations or establish MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='android:syntax-resolution' "
                      "AND status='pending' AND instr(reason,?)=0", (note, note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java resource ownership fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-resource-owners-', dir=base))
    runner = Runner(binary, directory)
    runner.root.mkdir()
    expected, actual = {}, {}

    def write(path, source):
        path = (directory / 'drawable-cache' / path.removeprefix('attached/')
                if path.startswith('attached/') else runner.root / path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def build(namespace, dependencies=''):
        return ("plugins { id 'com.android.library' }\nandroid { " + namespace + ' }\n'
                'dependencies { ' + dependencies + ' }\n')

    def usage(label, kind, name, sites, module='app'):
        _, output = runner.command('resource-usages', '@' + kind + '/' + name, '--module', module)
        output = output.replace(str(directory) + '/', '<fixture>/')
        expected[label] = {**observation(''), 'locations': sorted(sites),
                           'groups': [('Kotlin/Java', len(sites))] if sites else [], 'total': len(sites)}
        actual[label] = observation(output)

    def unused(label, module, names, kind=None):
        flags = ('--type', kind) if kind else ()
        _, output = runner.command('resource-usages', '--unused', '--module', module, *flags)
        expected[label] = {**observation(''), 'unused': sorted(names), 'unused_total': len(names)}
        actual[label] = observation(output)

    write('app/build.gradle', build("namespace 'fixture.app'", 'implementation(project(":library"))'))
    write('library/build.gradle', build("namespace 'fixture.library'"))
    write('decoy/build.gradle', build("namespace 'fixture.decoy'"))
    write('app/gradle.properties', 'android.nonTransitiveRClass=false\n')
    write('app/src/main/res/values/strings.xml', '<resources><string name="local">Value</string></resources>')
    for module in ('library', 'decoy'):
        write(module + '/src/main/res/values/strings.xml',
              '<resources><string name="library_only">Value</string></resources>')
    write('app/Use.java', '''package fixture.app;
class Use {
 int merged = R.string.library_only;
 int qualified = fixture.app.R.string.library_only;
 int local = R.string.local;
}
''')
    # R stubs stay outside the indexed sources, as they do in generated builds.
    stub = directory / 'R.java'
    stub.write_text('package fixture.app; public class R { public static class string { '
                    'public static final int library_only=1, local=2; } }')
    javac = shutil.which('javac')
    if javac is None:
        raise ToolError('Java resource ownership validation requires JDK 17+')
    with (directory / 'javac.stdout.log').open('wb') as stdout, \
            (directory / 'javac.stderr.log').open('wb') as stderr:
        result = subprocess.run([javac, '-proc:none', '-d', str(directory / 'classes'),
                                 str(stub), str(runner.root / 'app/Use.java')],
                                stdout=stdout, stderr=stderr, timeout=30)
    if result.returncode:
        raise ToolError('authored merged R source did not compile; see private logs')
    expected['javac-merged'], actual['javac-merged'] = 0, result.returncode
    runner.command('rebuild', '--force', '--max-files', 0)
    usage('merged', 'string', 'library_only', [('app/Use.java', 3), ('app/Use.java', 4)])
    unused('merged:library', 'library', [])
    unused('merged:decoy', 'decoy', ['string/library_only'])
    write('app/gradle.properties', 'android.nonTransitiveRClass=true\n')
    # The same source is deliberately rejected against a non-transitive R.
    stub.write_text('package fixture.app; public class R { public static class string { '
                    'public static final int local=2; } }')
    with (directory / 'javac-negative.stdout.log').open('wb') as stdout, \
            (directory / 'javac-negative.stderr.log').open('wb') as stderr:
        result = subprocess.run([javac, '-proc:none', '-d', str(directory / 'negative-classes'),
                                 str(stub), str(runner.root / 'app/Use.java')],
                                stdout=stdout, stderr=stderr, timeout=30)
    expected['javac-nontransitive-reject'], actual['javac-nontransitive-reject'] = True, result.returncode != 0
    runner.command('rebuild', '--force', '--max-files', 0)
    usage('nontransitive', 'string', 'library_only', [])
    unused('nontransitive:library', 'library', ['string/library_only'])

    # Namespace metadata is parsed without executing a build script. Computed
    # declarations are unresolved, rather than guessed from a manifest.
    for label, script, manifest, sites in (
            ('variable', "def owner = 'fixture.library'\n" + build('namespace owner'), None, [1]),
            ('variable-kts', 'val owner = "fixture.library"\n' + build('namespace = owner'), None, [1]),
            ('manifest', build(''), '<manifest package="fixture.library"/>', [1]),
            ('computed', build("namespace 'fixture.library' + suffix"), '<manifest package="fixture.library"/>', [])):
        for filename in ('build.gradle', 'build.gradle.kts'):
            (runner.root / 'library' / filename).unlink(missing_ok=True)
        write('library/build.gradle.kts' if label == 'variable-kts' else 'library/build.gradle', script)
        manifest_path = runner.root / 'library/src/main/AndroidManifest.xml'
        if manifest is None:
            manifest_path.unlink(missing_ok=True)
        else:
            write('library/src/main/AndroidManifest.xml', manifest)
        write('app/Use.java', 'class Use { int x = fixture.library.R.string.library_only; }\n')
        runner.command('rebuild', '--force', '--max-files', 0)
        usage('namespace:' + label, 'string', 'library_only', [('app/Use.java', n) for n in sites])
        unused('namespace-unused:' + label, 'library', [] if sites else ['string/library_only'])

    write('library/build.gradle', build("namespace 'fixture.library'"))
    write('library/src/main/res/values/typed.xml', '''<resources>
<integer name="count">1</integer>
<bool name="enabled">true</bool>
<string-array name="labels"><item>Value</item></string-array>
<integer-array name="numbers"><item>1</item></integer-array>
<plurals name="messages"><item quantity="one">Value</item></plurals>
</resources>''')
    write('app/Use.java', '''class Use {
 int count = fixture.library.R.integer.count;
 int enabled = fixture.library.R.bool.enabled;
 int labels = fixture.library.R.array.labels;
 int messages = fixture.library.R.plurals.messages;
}
''')
    runner.command('rebuild', '--force', '--max-files', 0)
    for line, kind, name in ((2, 'integer', 'count'), (3, 'bool', 'enabled'),
                              (4, 'array', 'labels'), (5, 'plurals', 'messages')):
        usage('type:' + kind, kind, name, [('app/Use.java', line)])
        unused('type-unused:' + kind, 'library', ['array/numbers'] if kind == 'array' else [], kind)

    # Indexed generated Java sources must not be mistaken for non-resource
    # imported fields, or lose same-package generated type ownership.
    write('library/R.java', 'package fixture.library; public class R { public static class integer { public static final int count=1; } }')
    write('app/Imported.java', 'import static fixture.library.R.integer.count;\nclass Imported { int value=count; }\n')
    write('app/Wildcard.java', 'import static fixture.library.R.integer.*;\nclass Wildcard { int value=count; }\n')
    write('app/Same.java', 'package fixture.library;\nclass Same { int value=R.integer.count; }\n')
    with (directory / 'javac-generated.stdout.log').open('wb') as stdout, \
            (directory / 'javac-generated.stderr.log').open('wb') as stderr:
        result = subprocess.run([javac, '-proc:none', '-d', str(directory / 'generated-classes'),
                                 *map(str, (runner.root / name for name in ('library/R.java', 'app/Imported.java', 'app/Same.java', 'app/Wildcard.java')))],
                                stdout=stdout, stderr=stderr, timeout=30)
    if result.returncode:
        raise ToolError('authored indexed R source did not compile; see private logs')
    expected['javac-indexed-R'], actual['javac-indexed-R'] = 0, result.returncode
    runner.command('rebuild', '--force', '--max-files', 0)
    usage('indexed-R', 'integer', 'count', [('app/Use.java', 2), ('app/Imported.java', 2), ('app/Same.java', 2), ('app/Wildcard.java', 2)])
    unused('indexed-R:owner', 'library', [], 'integer')

    # Colliding paths retain their root; the root name is not a resource kind.
    write('attached/library/build.gradle', build("namespace 'fixture.attached'"))
    write('attached/library/src/main/res/values/strings.xml',
          '<resources><string name="library_only">Attached</string></resources>')
    write('attached/library/Use.java', 'class Attached { int x = fixture.attached.R.string.library_only; }\n')
    write('app/Use.java', 'class Use { int x = fixture.attached.R.string.library_only; }\n')
    runner.command('subtree', 'add', 'extra', directory / 'drawable-cache')
    runner.command('rebuild', '--force', '--max-files', 0)
    usage('attached:consumer', 'string', 'library_only', [('app/Use.java', 1)])
    usage('attached:own-module', 'string', 'library_only', [('<fixture>/drawable-cache/library/Use.java', 1)], 'extra::library')
    unused('attached:owner', 'extra::library', [])
    unused('attached:primary-library', 'library', ['string/library_only'], 'string')
    unused('attached:decoy', 'decoy', ['string/library_only'])
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' +
                           mobile_contracts.SCHEMA + ANDROID_SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        want = {'.gradle': 3, '.properties': 1, '.xml': 5, '.java': 5}
        if counts != want:
            raise ToolError('Java resource ownership full inventory incomplete')
        expected['inventory'], actual['inventory'] = want, counts
        expected['applicability'], actual['applicability'] = 'pending', applicability(state, runner.root)[0]
        mobile_contracts.inventory(state, directory / 'drawable-cache')
        attached = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        expected['attached-inventory'], actual['attached-inventory'] = {'.gradle': 1, '.xml': 1, '.java': 1}, attached
    finally:
        state.close()
    return {FEATURE: expected}, {FEATURE: actual}
