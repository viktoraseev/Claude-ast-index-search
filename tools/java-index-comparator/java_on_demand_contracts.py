"""Java on-demand import binding, independently authored and javac checked."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-on-demand-types', 'unused-deps:java-on-demand-members'}
REASON = ('independent source/state: disposable javac-validated Java package/member/static '
          'on-demand imports, ambiguity and accessibility guards, direct member usage and '
          'JSON/text dependency identities; not MCP equivalence or inherited-member dispatch')


def plan_imports(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-on-demand-imports'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='unused-deps:semantic-resolution' AND status='pending'",
                      ('Java lexical/boolean-flow/loop/resource shadows and direct on-demand '
                       'type/member imports with accessibility/ambiguity guards have separate '
                       'javac-validated JSON/text contracts; inherited/nested member lookup, '
                       'protected subclass access, external/compiler/platform symbols, receiver '
                       'dispatch, compiler-wide reachability and attached-root resolution remain '
                       'unresolved; independent source/state coverage is not MCP equivalence',))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java on-demand artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-on-demand-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

        def write(path, content):
            path = runner.root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        libraries = {
            'alpha/Widget.java': 'package alpha; public class Widget {}',
            'beta/Widget.java': 'package beta; public class Widget {}',
            'beta/Secret.java': 'package beta; class Secret {}',
            'alpha/Secret.java': 'package alpha; public class Secret {}',
            'alpha/Outer.java': '''package alpha; public class Outer {
                public static class Inner {} public class Instance {}
                private static class Hidden {} protected static class Guarded {}
                public static int VALUE = 1; public static int MAX_VALUE = 1;
                public static int read() { return 1; }
                public static int choose() { return 1; }
                public static String TEXT = "";
                private static int PRIVATE = 2;
            }''',
            'beta/HiddenOwner.java': 'package beta; class HiddenOwner { public static class Inner {} }',
            'beta/Outer.java': 'package beta; public class Outer { public static class Inner {} public static int VALUE = 2; public static int choose = 1; }',
            'alpha/Port.java': 'package alpha; public interface Port { class Nested {} int FLAG = 1; }',
            'alpha/Mode.java': 'package alpha; public enum Mode { ON; public static int read() { return 1; } }',
            'beta/TEXT.java': 'package beta; public class TEXT { public static int length() { return 1; } }',
        }
        for path, source in libraries.items():
            write(path, source + '\n')
        for name in ('alpha', 'beta'):
            write(name + '/build.gradle', 'plugins {}\n')
        # Valid positives and compiler-rejected guards share production checks.
        # Invalid sites must not become arbitrary dependency usage assertions.
        cases = [
            ('package-wildcard', 'import alpha.*; class Use { Widget x; }', True, {'alpha': ['Widget']}),
            ('member-wildcard', 'import alpha.Outer.*; class Use { Inner x; Instance y; }', True, {'alpha': ['Inner', 'Instance']}),
            ('static-type-wildcard', 'import static alpha.Outer.*; class Use { Inner x; }', True, {'alpha': ['Inner']}),
            ('implicit-interface-type', 'import static alpha.Port.*; class Use { Nested x; }', True, {'alpha': ['Nested']}),
            ('inaccessible-candidate', 'import alpha.*; import beta.*; class Use { Secret x; }', True, {'alpha': ['Secret']}),
            ('ambiguous-package', 'import alpha.*; import beta.*; class Use { Widget x; }', False, {}),
            ('ambiguous-member', 'import alpha.Outer.*; import beta.Outer.*; class Use { Inner x; }', False, {}),
            ('private-member', 'import alpha.Outer.*; class Use { Hidden x; }', False, {}),
            ('protected-member', 'import alpha.Outer.*; class Use { Guarded x; }', False, {}),
            ('hidden-enclosing', 'import beta.HiddenOwner.*; class Use { Inner x; }', False, {}),
            ('nonstatic-member', 'import static alpha.Outer.*; class Use { Instance x; }', False, {}),
            ('explicit-precedence', 'import beta.Widget; import alpha.*; class Use { Widget x; }', True, {'beta': ['Widget']}),
            ('unused-static', 'import static alpha.Outer.*; class Use {}', True, {}),
            ('static-field', 'import static alpha.Outer.*; class Use { int x = VALUE; }', True, {'alpha': ['Outer']}),
            ('static-method', 'import static alpha.Outer.*; class Use { int x = read(); }', True, {'alpha': ['Outer']}),
            ('static-interface-field', 'import static alpha.Port.*; class Use { int x = FLAG; }', True, {'alpha': ['Port']}),
            ('static-value-shadow', 'import static alpha.Outer.*; class Use { int VALUE = 3; int x = VALUE; }', True, {}),
            ('ambiguous-static-field', 'import static alpha.Outer.*; import static beta.Outer.*; class Use { int x = VALUE; }', False, {}),
            ('private-static-field', 'import static alpha.Outer.*; class Use { int x = PRIVATE; }', False, {}),
            ('static-field-receiver', 'import static alpha.Outer.*; class Use { int x = TEXT.length(); }', True, {'alpha': ['Outer']}),
            ('static-lambda-field', 'import static alpha.Outer.*; class Use { java.util.function.Supplier<String> x = () -> TEXT; }', True, {'alpha': ['Outer']}),
            ('static-method-shadow', 'import static alpha.Outer.*; class Use { int read() { return 2; } int x = read(); }', True, {}),
            ('external-single-precedence', 'import static java.lang.Integer.MAX_VALUE; import static alpha.Outer.*; class Use { int x = MAX_VALUE; }', True, {}),
            ('single-field-method-namespaces', 'import static beta.Outer.choose; import static alpha.Outer.*; class Use { int x = choose + choose(); }', True, {'alpha': ['Outer'], 'beta': ['Outer']}),
            ('static-single-instance', 'import static alpha.Outer.Instance; class Use { Instance x; }', False, {}),
            ('same-package-access', 'import beta.*; class Use { Secret x; }', True, {'beta': ['Secret']}),
            ('same-package-protected', 'import alpha.Outer.*; class Use { Guarded x; }', True, {'alpha': ['Guarded']}),
            ('static-enum-constant', 'import static alpha.Mode.*; class Use { Object x = ON; }', True, {'alpha': ['Mode']}),
            ('static-enum-method', 'import static alpha.Mode.*; class Use { int x = read(); }', True, {'alpha': ['Mode']}),
            ('static-value-type-shadow', 'import static alpha.Outer.*; import beta.*; class Use { int x = TEXT.length(); }', True, {'alpha': ['Outer']}),
            ('static-value-type-namespaces', 'import static alpha.Outer.*; import beta.*; class Use { TEXT typed; int x = TEXT.length(); }', True, {'alpha': ['Outer'], 'beta': ['TEXT']}),
        ]
        member_labels = {label for label, *_ in cases[12:]} - {
            'static-single-instance', 'same-package-access', 'same-package-protected'}
        for number, (label, source, _, _) in enumerate(cases):
            package = {'same-package-access': 'beta', 'same-package-protected': 'alpha'}.get(label, f'fixture.c{number}')
            write(f'consumer{number}/Use.java', f'package {package};\n{source}\n')
            write(f'consumer{number}/build.gradle',
                  'dependencies {\n implementation(project(":alpha"))\n implementation(project(":beta"))\n}\n')
        write('inventory.txt', 'complete inventory sentinel\n')
        inventory = connect(runner.directory / 'inventory.sqlite')
        try:
            inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(inventory, runner.root)
            counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            want = {'.java': len(libraries) + len(cases), '.gradle': len(cases) + 2, '.txt': 1}
            if counts != want:
                raise ToolError('Java on-demand full inventory incomplete')
            for feature in FEATURES:
                expected[feature]['inventory'], actual[feature]['inventory'] = want, counts
        finally:
            inventory.close()
        javac = shutil.which('javac')
        if not javac:
            raise ToolError('Java on-demand source checks require javac')
        library_files = [str(runner.root / path) for path in libraries]
        classes = runner.directory / 'classes'

        def compile_sources(label, arguments):
            with (runner.directory / (label + '.javac.stdout.log')).open('wb') as stdout, \
                    (runner.directory / (label + '.javac.stderr.log')).open('wb') as stderr:
                return subprocess.run([javac, '-proc:none', '-d', str(classes), *arguments],
                                      stdout=stdout, stderr=stderr, timeout=30).returncode

        if compile_sources('libraries', library_files):
            raise ToolError('authored on-demand library did not compile; see private logs')
        runner.command('rebuild', '--force', '--max-files', '0')
        for number, (label, _, valid, used) in enumerate(cases):
            feature = 'unused-deps:java-on-demand-' + ('members' if label in member_labels else 'types')
            compiled = compile_sources(label, ['-cp', str(classes), str(runner.root / f'consumer{number}/Use.java')])
            expected[feature][label + ':javac'], actual[feature][label + ':javac'] = valid, compiled == 0
            flags = ('unused-deps', f'consumer{number}', '--strict', '--verbose')
            document = runner.json(*flags)
            expected[feature][label + ':json'] = {
                'items': [(owner, 'direct' if owner in used else 'unused', len(used.get(owner, [])), used.get(owner, []))
                          for owner in ('alpha', 'beta')],
                'summary': {'unused': 2 - len(used), 'used': len(used), 'total': 2,
                            'exported': 0, 'direct': len(used), 'transitive': 0, 'xml': 0, 'resources': 0},
            }
            actual[feature][label + ':json'] = {
                'items': sorted((item['name'], item['category'], item['usage']['direct'], item['examples']['direct'])
                                for item in document.get('items', [])), 'summary': document.get('summary'),
            }
            _, text = runner.command(*flags)
            expected[feature][label + ':text'] = True
            actual[feature][label + ':text'] = (
                f'Total: {2 - len(used)} unused, 0 exported, {len(used)} used of 2 dependencies' in text
                and all(f'  ✓ {owner} - {len(names)} symbols: {", ".join(names)}' in text for owner, names in used.items())
                and all(f'  ✗ {owner} (implementation)' in text for owner in ('alpha', 'beta') if owner not in used))
        return expected, actual
