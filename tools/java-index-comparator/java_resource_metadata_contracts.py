"""Java attr/styleable ownership and constant namespaces; independent source/CLI."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from android_contracts import observation

FEATURE = 'resource-usages:java-metadata-ownership'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-resource-metadata-ownership-v1'
REASON = ('independent source/javac/CLI: Java attr/id/styleable declaring ownership and '
          'proven constant namespace expressions with unknown/reassigned guards; '
          'no Gradle execution or XML syntax equivalence; not MCP equivalence')


def plan_metadata(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java metadata fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-resource-metadata-', dir=base) as temporary:
        directory = Path(temporary).resolve()
        runner = Runner(binary, directory)
        expected, actual = {}, {}

        def write(path, content):
            file = runner.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
            return file

        write('app/build.gradle', "plugins { id 'com.android.library' }\nandroid { namespace 'fixture.app' }\n"
              'dependencies { implementation(project(":library")) }\n')
        build = write('library/build.gradle', "plugins { id 'com.android.library' }\nandroid { namespace 'fixture.library' }\n")
        write('library/src/main/res/values/attrs.xml', '''<resources>
<attr name="outside" format="integer"/>
<declare-styleable name="Panel">
 <attr name="title" format="string"/>
 <attr name="android:orientation"/>
</declare-styleable>
<declare-styleable name="Other"><attr name="other" format="integer"/></declare-styleable>
<item type="id" name="button"/>
<string name="hit">Value</string>
</resources>
''')
        use = write('app/Use.java', '''package fixture.app;
class Use {
 int title=fixture.library.R.attr.title;
 int button=fixture.library.R.id.button;
 int[] panel=fixture.library.R.styleable.Panel;
 int index=fixture.library.R.styleable.Panel_title;
 int platform=fixture.library.R.styleable.Panel_android_orientation;
 int hit=fixture.library.R.string.hit;
}
''')
        stub = directory / 'R.java'
        stub.write_text('package fixture.library; public class R { '
                        'public static class attr { public static int title; } '
                        'public static class id { public static int button; } '
                        'public static class styleable { public static int[] Panel; '
                        'public static int Panel_title,Panel_android_orientation; } '
                        'public static class string { public static int hit; } }')
        with (directory / 'javac.log').open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'),
                                     str(stub), str(use)], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('Java resource metadata source rejected; see private log')
        expected['javac'], actual['javac'] = 0, result.returncode
        runner.command('rebuild', '--force', '--max-files', 0)

        def usage(label, kind, name, lines):
            _, output = runner.command('resource-usages', '@' + kind + '/' + name, '--module', 'app')
            expected[label] = {**observation(''), 'locations': [('app/Use.java', n) for n in lines],
                               'groups': [('Kotlin/Java', len(lines))] if lines else [], 'total': len(lines)}
            actual[label] = observation(output)

        for kind, name, line in [('attr', 'title', 3), ('id', 'button', 4),
                                 ('styleable', 'Panel', 5), ('styleable', 'Panel_title', 6),
                                 ('styleable', 'Panel_android_orientation', 7)]:
            usage('definition:' + kind + ':' + name, kind, name, [line])
        for kind, unused in [('attr', ['attr/other', 'attr/outside']), ('id', []),
                              ('styleable', ['styleable/Other', 'styleable/Other_other'])]:
            _, output = runner.command('resource-usages', '--unused', '--module', 'library', '--type', kind)
            expected['unused:' + kind] = {**observation(''), 'unused': unused, 'unused_total': len(unused)}
            actual['unused:' + kind] = observation(output)
        # All values are source-provable; unknown runtime values remain
        # unresolved, without falling back to a colliding manifest namespace.
        for label, preamble, expression, found in (
                ('concat', '', "'fixture.' + 'library'", True),
                ('aliases', "def prefix='fixture.'; def suffix='library';\n", 'prefix + suffix', True),
                ('parenthesized', '', "('fixture.' + ('library'))", True),
                ('unknown', '', "'fixture.' + suffix", False),
                ('reassigned', "def prefix='fixture.'; prefix='wrong.';\n", "prefix + 'library'", False),
                ('call', '', "'fixture.library'.toString()", False)):
            build.write_text(preamble + "plugins { id 'com.android.library' }\nandroid { namespace " + expression + ' }\n')
            runner.command('update')
            usage('namespace:' + label, 'string', 'hit', [8] if found else [])
        return {FEATURE: expected}, {FEATURE: actual}
