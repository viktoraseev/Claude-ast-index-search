"""Java resource syntax on disposable sources; independent source, not MCP truth.

Literal Gradle namespaces establish ownership only for explicitly qualified or
imported R references. Compiler visibility, shadowing and merged dependency R
classes are separate pending contracts.
"""
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from android_contracts import observation

FEATURES = {'resource-usages:java-lexical', 'resource-usages:java-imports',
            'resource-usages:java-namespace-literals'}
REASON = ('independent source/state: disposable Java expression locations, comment/literal '
          'exclusion, explicit/static R imports and literal Gradle namespace ownership; '
          'not MCP equivalence or compiler-wide resource resolution')
SOURCES = {
    'app/Local.java': '''class Local {
 int direct = R.string.direct;
 int spaced = R /* separator */ . string . spaced;
 int multi = R
 .string
 .multi;
 // R.string.ghost
 /* R.string.ghost */
 String text = "R.string.ghost @string/ghost";
 String block = """
 R.string.ghost
 @string/ghost
 """;
 int foreign = fakeR.string.ghost;
 int platform = android.R.string.ghost;
 int unknown = missing.namespace.R.string.ghost;
 int shared = R.string.shared;
}
''',
    'app/Imported.java': '''import fixture.library.R;
class Imported {
 int shared = R.string.shared;
 int qualified = fixture.library.R.string.shared;
}
''',
    'app/Static.java': '''import static fixture.library.R.string.single;
import static fixture.library.R.string.*;
import static fixture.app.R.string.*;
class Static {
 int first = single;
 int second = many + single;
 void single() {}
 void use() { single(); }
}
''',
    'app/Nested.java': '''import fixture.library.R.string;
class Nested { int nested = string.nested; }
''',
    'app/Ambiguous.java': '''import static fixture.library.R.string.*;
import static fixture.app.R.string.*;
class Ambiguous { int collision = ambiguous; }
''',
}


def plan_java_resources(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-resource-syntax-v1'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Java resource fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-resource-syntax-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()

    def write(path, content):
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    names = ['direct', 'spaced', 'multi', 'ghost', 'shared', 'single', 'many', 'nested', 'ambiguous']
    for module in ('app', 'library'):
        write(module + '/build.gradle', "plugins { id 'com.android.library' }\n"
              "android { namespace 'fixture." + module + "' }\n")
        write(module + '/src/main/res/values/strings.xml', '<resources>\n' +
              ''.join('<string name="' + name + '">Value</string>\n' for name in names
                      if module == 'library' or name != 'many') +
              '</resources>\n')
    for path, source in SOURCES.items():
        write(path, source)
    runner.command('rebuild', '--force', '--max-files', 0)
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def usage(feature, name, locations):
        _, output = runner.command('resource-usages', '@string/' + name, '--module', 'app')
        expected[feature][name] = {**observation(''), 'locations': sorted(locations),
            'groups': [('Kotlin/Java', len(locations))] if locations else [], 'total': len(locations)}
        actual[feature][name] = observation(output)

    lexical, imports, ownership = ('resource-usages:java-lexical', 'resource-usages:java-imports',
                                   'resource-usages:java-namespace-literals')
    usage(lexical, 'direct', [('app/Local.java', 2)])
    usage(lexical, 'spaced', [('app/Local.java', 3)])
    usage(lexical, 'multi', [('app/Local.java', 4)])
    usage(lexical, 'ghost', [])
    usage(imports, 'single', [('app/Static.java', 5), ('app/Static.java', 6)])
    usage(imports, 'many', [('app/Static.java', 6)])
    usage(imports, 'nested', [('app/Nested.java', 2)])
    usage(imports, 'ambiguous', [])
    usage(ownership, 'shared', [('app/Imported.java', 3), ('app/Imported.java', 4),
                                ('app/Local.java', 17)])
    # Unused output independently proves owner binding despite colliding names.
    for module, unused in [('app', ['ghost', 'single', 'nested', 'ambiguous']),
                           ('library', ['direct', 'spaced', 'multi', 'ghost', 'ambiguous'])]:
        _, output = runner.command('resource-usages', '--unused', '--module', module)
        expected[ownership]['unused:' + module] = {**observation(''),
            'unused': sorted('string/' + name for name in unused), 'unused_total': len(unused)}
        actual[ownership]['unused:' + module] = observation(output)
    return expected, actual
