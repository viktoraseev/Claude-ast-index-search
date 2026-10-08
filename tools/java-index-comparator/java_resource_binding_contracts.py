"""Occurrence-scoped Java resource imports; independent source, not MCP truth."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
from android_contracts import observation, applicability, SCHEMA as ANDROID_SCHEMA
import mobile_contracts

FEATURE = 'resource-usages:java-lexical-bindings'
FEATURES = {FEATURE}
REASON = ('independent source/state: javac-validated Java R/type alias and static constant '
          'lexical shadows, declaration points, captures, pattern/loop/try scopes and '
          'source-classpath inherited fields/types/constants, same-package type precedence and '
          'field-versus-method import namespaces through resource-usages/unused ownership; '
          'not MCP equivalence or a complete compiler-resolution contract')

# Every expected line is authored independently of ast-index, including the
# two sites on a single line where only one refers to the imported R class.
# The support class is deliberately not an Android resource owner.
SUPPORT = '''class Values { public int hit; }
class Shadow { public static Values string = new Values(); }
class Prefix { public Chain library = new Chain(); }
class Chain { public Shadow R = new Shadow(); }
'''
CASES = {
    'imported-wildcard-field': ('import fixture.library.R;\nimport static plain.Owner.*;',
                                'class Use { int shadow() { return R.string.hit; } }\n', []),
    'imported-wildcard-method': ('import fixture.library.R;\nimport static plain.OwnerMethods.*;',
                                 'class Use { int real() { return R.string.hit; } }\n', [1]),
    'package-wildcard': ('import fixture.library.*;',
                         'class Use { int real() { return R.string.hit; } }\n', [1]),
    'inherited-field': ('import fixture.library.R;', '''class Base { Shadow R = new Shadow(); }
class Use extends Base { int shadow() { return R.string.hit; } }
class Other { int real() { return R.string.hit; } }
''', [3]),
    'inherited-type': ('import fixture.library.R;', '''class Base { static class R { static class string { static int hit; } } }
class Use extends Base { int shadow() { return R.string.hit; } }
class Other { int real() { return R.string.hit; } }
''', [3]),
    'inherited-constant': ('import static fixture.library.R.string.*;', '''class Base { int hit; }
class Use extends Base { int shadow() { return hit; } }
class Other { int real() { return hit; } }
''', [3]),
    'imported-field': ('import static plain.string.hit;\nimport static fixture.library.R.string.*;',
                       'class Use { int shadow() { return hit; } }\n', []),
    'imported-method': ('import static plain.Methods.hit;\nimport static fixture.library.R.string.*;',
                       'class Use { int real() { hit(); return hit; } }\n', [1]),
    'cross-file': ('import fixture.library.*;',
                  'class Use { int shadow() { return R.string.hit; } }\n', []),
    'parameter': ('import fixture.library.R;', '''class Use {
 int shadow(Shadow R) { return R.string.hit; }
 int real() { return R.string.hit; }
}
''', [3]),
    'field': ('import fixture.library.R;', '''class Use {
 Shadow R = new Shadow();
 int shadow() { return R.string.hit; }
}
class Other { int real() { return R.string.hit; } }
''', [5]),
    'block': ('import fixture.library.R;', '''class Use {
 int use() { int n = R.string.hit; { Shadow R = new Shadow(); n += R.string.hit; } return n + R.string.hit; }
}
''', [2, 2]),
    'local-type': ('import fixture.library.R;', '''class Use {
 int use() { int n = R.string.hit; { class R { static class string { static int hit; } } n += R.string.hit; } return n + R.string.hit; }
}
''', [2, 2]),
    'member-type': ('import fixture.library.R;', '''class Use {
 int shadow() { return R.string.hit; }
 static class R { static class string { static int hit; } }
}
class Other { int real() { return R.string.hit; } }
''', [5]),
    'type-parameter': ('import fixture.library.R;', '''class Use {
 <R extends Shadow> int shadow() { return R.string.hit; }
 int real() { return R.string.hit; }
}
''', [3]),
    'alias': ('import fixture.library.R.string;', '''class Use {
 int use() { int n = string.hit; { Values string = new Values(); n += string.hit; } return n + string.hit; }
 int type() { class string { static int hit; } return string.hit; }
}
''', [2, 2]),
    'static-field': ('import static fixture.library.R.string.hit;', '''class Use {
 int hit;
 int shadow() { return hit; }
}
class Other { int real() { return hit; } }
''', [5]),
    'static-local': ('import static fixture.library.R.string.*;', '''class Use {
 int use() { int n = hit; { int hit = 4; n += hit; } return n + hit; }
 int parameter(int hit) { return hit; }
 java.util.function.IntUnaryOperator capture(int hit) { return x -> hit + x; }
 int real() { return hit; }
}
''', [2, 2, 5]),
    'lambda': ('import fixture.library.R;', '''class Use {
 java.util.function.ToIntFunction<Shadow> shadow() { return R -> R.string.hit; }
 java.util.function.IntSupplier capture(Shadow R) { return () -> R.string.hit; }
 int real() { return R.string.hit; }
}
''', [4]),
    'pattern': ('import fixture.library.R;', '''class Use {
 int use(Object o) { if (o instanceof Shadow R && R.string.hit > 0) { return R.string.hit; } return R.string.hit; }
 int negative(Object o) { if (!(o instanceof Shadow R)) return R.string.hit; return R.string.hit; }
}
''', [2, 3]),
    'loops': ('import fixture.library.R;', '''class Use {
 int use(Shadow[] items) { for (Shadow R : items) { int n = R.string.hit; } return R.string.hit; }
 int loop() { for (Shadow R = new Shadow(); R.string.hit < 0; R.string.hit++) {} return R.string.hit; }
}
''', [2, 3]),
    'try': ('import fixture.library.R;', '''class Use {
 static class Handle extends Shadow implements AutoCloseable { public void close() {} }
 int use() { try (Handle R = new Handle()) { return R.string.hit; } finally { int n = R.string.hit; } }
}
''', [3]),
    'qualified': ('', '''class Use {
 Prefix fixture = new Prefix();
 int shadow() { return fixture.library.R.string.hit; }
}
class Other { int real() { return fixture.library.R.string.hit; } }
''', [5]),
    'static-alias': ('import static fixture.library.R.string;', '''class Use {
 int real() { return string.hit; }
 int shadow(Values string) { return string.hit; }
}
''', [2]),
    'nested-wildcard': ('import fixture.library.R.*;', '''class Use {
 int real() { return string.hit; }
 int shadow(Values string) { return string.hit; }
}
''', [2]),
    'static-type-wildcard': ('import static fixture.library.R.*;', '''class Use {
 int real() { return string.hit; }
 int shadow(Values string) { return string.hit; }
}
''', [2]),
    'constant-type-namespace': ('import static fixture.library.R.string.hit;', '''class Use {
 static class hit {}
 int real() { return hit; }
 int hit() { return hit; }
}
''', [3, 4]),
    'enum': ('import static fixture.library.R.string.*;', '''enum Use {
 hit;
 Object shadow() { return hit; }
}
class Other { int real() { return hit; } }
''', [5]),
    'explicit-type-precedence': ('import plain.string;\nimport fixture.library.R.*;', '''class Use {
 int shadow() { return string.hit; }
}
''', []),
    'type-expression-context': ('import static fixture.library.R.string.hit;', '''class Use {
 static class hit {}
 java.util.function.Supplier<hit> constructor = hit::new;
 Class<?> type = hit.class;
 int real() { return hit; }
}
''', [5]),
}


def plan_bindings(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-resource-lexical-bindings-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        note = ('; separate Java lexical resource binding checklist covers byte-scoped '
                'declarations, captures, pattern/loop/try boundaries and explicit/nested/static '
                'type imports plus source-classpath inherited/cross-file/member-kind and '
                'package-on-demand binding; other recorded parent obligations remain pending; '
                'independent source/state, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='android:syntax-resolution' "
                      "AND status='pending' AND instr(reason,?)=0", (note, note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java resource binding fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-resource-bindings-', dir=base))
    runner = Runner(binary, directory)
    runner.root.mkdir()

    def write(path, content):
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    for module in ('app', 'library'):
        write(module + '/build.gradle', "plugins { id 'com.android.library' }\n"
              + "android { namespace 'fixture." + module + "' }\n"
              + ('dependencies { implementation(project(":library")) }\n' if module == 'app' else ''))
        write(module + '/src/main/res/values/strings.xml',
              '<resources>' + ''.join('<string name="hit_' + label.replace('-', '_') +
              '">Value</string>' for label in CASES) + '</resources>\n')
    locations = {}
    names = {label: 'hit_' + label.replace('-', '_') for label in CASES}
    write('library/plain/string.java', 'package plain; public class string { ' +
          ''.join('public static int ' + name + ';' for name in names.values()) + ' }')
    write('library/plain/Methods.java', 'package plain; public class Methods { ' +
          ''.join('public static void ' + name + '() {}' for name in names.values()) + ' }')
    write('library/plain/Owner.java', 'package plain; public class Owner { '
          'public static class Values { ' + ''.join('public int ' + name + ';' for name in names.values()) + ' } '
          'public static class Shadow { public static Values string=new Values(); } '
          'public static Shadow R=new Shadow(); }')
    write('library/plain/OwnerMethods.java', 'package plain; public class OwnerMethods { public static void R() {} }')
    sources = [runner.root / ('library/plain/' + name + '.java')
               for name in ('string', 'Methods', 'Owner', 'OwnerMethods')]
    for label, (imports, body, lines) in CASES.items():
        path = 'app/' + label + '/Use.java'
        preamble = 'package probe.case_' + label.replace('-', '_') + ';\n' + imports + '\n'
        write(path, (preamble + body + SUPPORT).replace('hit', names[label]))
        sources.append(runner.root / path)
        locations[label] = [(path, line + preamble.count('\n')) for line in lines]
        if label == 'cross-file':
            extra = 'app/cross-file/R.java'
            write(extra, 'package probe.case_cross_file; class R { static class string { '
                  'static int ' + names[label] + '; } }')
            sources.append(runner.root / extra)
    # Generated R stubs validate compilation, but never enter the indexed tree.
    stub = directory / 'R.java'
    stub.write_text('package fixture.library; public class R { public static class string { ' +
                   ''.join('public static final int ' + name + ' = 1;' for name in names.values()) + ' } }')
    javac = shutil.which('javac')
    if javac is None:
        raise ToolError('Java resource binding validation requires JDK 17+')
    with (directory / 'javac.stdout.log').open('wb') as stdout, \
            (directory / 'javac.stderr.log').open('wb') as stderr:
        compilation = subprocess.run([javac, '-proc:none', '-d', str(directory / 'classes'),
                                      str(stub), *map(str, sources)],
                                     stdout=stdout, stderr=stderr, timeout=30)
    if compilation.returncode:
        raise ToolError('authored resource bindings did not compile; see private logs')
    expected, actual = {}, {}
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' +
                           mobile_contracts.SCHEMA + ANDROID_SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        want = {'.java': len(CASES) + 4 + int('cross-file' in CASES), '.gradle': 2, '.xml': 2}
        counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != want:
            raise ToolError('Java resource binding full inventory incomplete')
        expected['inventory'], actual['inventory'] = want, counts
        expected['applicability'], actual['applicability'] = 'pending', applicability(state, runner.root)[0]
    finally:
        state.close()
    expected['javac'], actual['javac'] = 0, compilation.returncode
    runner.command('rebuild', '--force', '--max-files', 0)
    for label, rows in locations.items():
        # Distinct names keep every site's result below the CLI's documented
        # text cap. A passing prefix cannot conceal an untested later case.
        _, output = runner.command('resource-usages', '@string/' + names[label], '--module', 'app')
        expected[label] = {**observation(''), 'locations': sorted(rows),
                           'groups': [('Kotlin/Java', len(rows))] if rows else [], 'total': len(rows)}
        actual[label] = observation(output)
    for module in ('app', 'library'):
        _, output = runner.command('resource-usages', '--unused', '--module', module)
        unused = sorted('string/' + names[label] for label, rows in locations.items()
                        if module == 'app' or not rows)
        expected['unused:' + module] = {**observation(''), 'unused': unused[:10],
                                       'unused_total': len(unused),
                                       'omitted': [len(unused) - 10] if len(unused) > 10 else []}
        actual['unused:' + module] = observation(output)
    return {FEATURE: expected}, {FEATURE: actual}
