"""Inherited Java imports retain declaring modules; independent of MCP."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-inherited-imports'}
REASON = ('independent source/state and javac: inherited Java member type/static imports, '
          'canonical import guards, hiding/diamonds, accessibility and attached classpath '
          'declaring ownership in default/strict JSON/text; not MCP equivalence')


def plan_imports(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-inherited-import-ownership'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,'inherited import ownership fixture')=0",
                          ('; separate inherited import ownership fixture executes public nested/static '
                           'member aliases, canonical/access/hiding/diamond guards and attached '
                           'classpath provenance; protected subclass lexical access, external/platform '
                           'lookup, overload/receiver dispatch and classpath-order ambiguity remain '
                           'pending; not MCP equivalence', parent))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('inherited import fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='inherited-imports-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(directory / 'index.sqlite'))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    def write(owner, path, source):
        destination = directory / owner / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source + '\n')
        return destination

    sources = {
        'base/Parent.java': '''package shared; public class Parent {
            public static class Nested { public static class Deep {} }
            public class Instance {} protected static class Guarded {}
            private static class Hidden {} static class PackageOnly {}
            public static int VALUE=1; public static int read(){return 1;}
            protected static int SECRET=1; public int instance(){return 1;}
        }''',
        'base/Port.java': '''package shared; public interface Port {
            class Token {} int FLAG=1; static int own(){return 1;}
        }''',
        'base/Other.java': 'package shared; public interface Other { class Token {} int FLAG=2; }',
        'base/HiddenBase.java': 'package shared; class HiddenBase { public static class Exported {} public static int EXPORTED=1; }',
        'lib/Child.java': 'package shared; public class Child extends Parent {}',
        'lib/HiddenChild.java': 'package shared; public class HiddenChild extends HiddenBase {}',
        'lib/Left.java': 'package shared; public interface Left extends Port {}',
        'lib/Right.java': 'package shared; public interface Right extends Port {}',
        'lib/Diamond.java': 'package shared; public class Diamond implements Left, Right {}',
        'lib/Ambiguous.java': 'package shared; public class Ambiguous implements Port, Other {}',
        'lib/Hider.java': 'package shared; public class Hider extends Parent { private static class Nested {} public static int VALUE=2; }',
        'lib/PrivateToken.java': 'package shared; public class PrivateToken { private static class Token {} }',
        'lib/AccessibleToken.java': 'package shared; public class AccessibleToken extends PrivateToken implements Port {}',
        'lib/PrivateField.java': 'package shared; public class PrivateField extends Parent { private static int VALUE=2; }',
        'base/CrossParent.java': 'package shared; public class CrossParent { static class CrossType {} static int CROSS=1; }',
        'lib/Bridge.java': 'package other; public class Bridge extends shared.CrossParent {}',
        'lib/Return.java': 'package shared; public class Return extends other.Bridge {}',
    }
    cases = [
        ('member-types', 'import shared.Child.*; class Use { Nested x; Instance y; Nested.Deep z; }', False, {}),
        ('static-types', 'import static shared.Child.*; class Use { Nested x; Nested.Deep y; }', True, {'base': ['Deep', 'Nested']}),
        ('qualified-alias', 'class Use { shared.Child.Nested x; shared.Child.Nested.Deep y; }', True, {'base': ['Deep', 'Nested']}),
        ('qualified-instance-alias', 'class Use { shared.Child.Instance x; }', True, {'base': ['Instance']}),
        ('static-field', 'import static shared.Child.*; class Use { int x=VALUE; }', True, {'base': ['Parent']}),
        ('static-method', 'import static shared.Child.read; class Use { int x=read(); }', True, {'base': ['Parent']}),
        ('static-wildcard-method', 'import static shared.Child.*; class Use { int x=read(); }', True, {'base': ['Parent']}),
        ('hidden-owner-type', 'import static shared.HiddenChild.*; class Use { Exported x; }', True, {'base': ['Exported']}),
        ('hidden-owner-field', 'import static shared.HiddenChild.*; class Use { int x=EXPORTED; }', True, {'base': ['HiddenBase']}),
        ('diamond-type', 'import static shared.Diamond.*; class Use { Token x; }', True, {'base': ['Token']}),
        ('diamond-field', 'import static shared.Diamond.*; class Use { int x=FLAG; }', True, {'base': ['Port']}),
        ('ambiguous-type', 'import shared.Ambiguous.*; class Use { Token x; }', False, {}),
        ('ambiguous-field', 'import static shared.Ambiguous.*; class Use { int x=FLAG; }', False, {}),
        ('private-hiding', 'import shared.Hider.*; class Use { Nested x; }', False, {}),
        ('field-hiding', 'import static shared.Hider.*; class Use { int x=VALUE; }', True, {'lib': ['Hider']}),
        ('private-field-hiding', 'import static shared.PrivateField.*; class Use { int x=VALUE; }', False, {}),
        ('private-not-inherited', 'import static shared.AccessibleToken.*; class Use { Token x; }', True, {'base': ['Token']}),
        ('private-type', 'import shared.Child.*; class Use { Hidden x; }', False, {}),
        ('package-type', 'import shared.Child.*; class Use { PackageOnly x; }', False, {}),
        ('protected-type', 'import shared.Child.*; class Use { Guarded x; }', False, {}),
        ('protected-subclass-import', 'import static shared.Child.Guarded; class Use extends shared.Child { Guarded x; }', False, {'lib': ['Child']}),
        ('protected-field', 'import static shared.Child.*; class Use { int x=SECRET; }', False, {}),
        ('nonstatic-type', 'import static shared.Child.*; class Use { Instance x; }', False, {}),
        ('nonstatic-method', 'import static shared.Child.instance; class Use { int x=instance(); }', False, {}),
        ('interface-static-not-inherited', 'import static shared.Diamond.*; class Use { int x=own(); }', False, {}),
        ('noncanonical-single-type', 'import shared.Child.Nested; class Use { Nested x; }', False, {}),
        ('inherited-static-single-type', 'import static shared.Child.Nested; class Use { Nested x; }', True, {'base': ['Nested']}),
        ('canonical-single-type', 'import shared.Parent.Nested; class Use { Nested x; }', True, {'base': ['Nested']}),
        ('same-package-protected', 'import static shared.Child.*; class Use { Guarded x; }', True, {'base': ['Guarded']}),
        ('same-package-package', 'import static shared.Child.*; class Use { PackageOnly x; }', True, {'base': ['PackageOnly']}),
        ('same-package-qualified', 'class Use { Child.Nested x; }', True, {'base': ['Nested']}),
        ('same-package-crossing-type', 'import static shared.Return.*; class Use { CrossType x; }', False, {}),
        ('same-package-crossing-field', 'import static shared.Return.*; class Use { int x=CROSS; }', False, {}),
    ]
    # The primary names intentionally conflict with attached accessibility and
    # inheritance. Only the selected classpath can provide declaring metadata.
    for owner in ('project', 'attached'):
        for path, source in sources.items():
            if owner == 'project' and path == 'base/Parent.java':
                source = 'package shared; public class Parent {}'
            write(owner, path, source)
        write(owner, 'base/build.gradle', 'plugins {}')
        write(owner, 'lib/build.gradle', 'dependencies { api(project(":base")) }')
        write(owner, 'inventory.txt', 'bounded full inventory sentinel')
    for label, source, _, _ in cases:
        package = 'shared' if label.startswith('same-package-') else 'consumer'
        write('attached', label + '/Use.java', f'package {package}; ' + source)
        write('attached', label + '/build.gradle', 'dependencies { implementation(project(":lib")); implementation(project(":base")) }')
    javac = shutil.which('javac')
    if not javac:
        raise ToolError('inherited import checks require javac')
    classes = directory / 'classes'

    def compile(label, paths, classpath=None):
        args = [javac, '-proc:none', '-d', str(directory / (label + '-classes')),
                '-sourcepath', str(directory / 'empty-sourcepath')]
        if classpath:
            args += ['-cp', str(classpath)]
        with (directory / (label + '.javac.stdout.log')).open('wb') as stdout, \
                (directory / (label + '.javac.stderr.log')).open('wb') as stderr:
            return subprocess.run([*args, *map(str, paths)], cwd=directory,
                                  env={**os.environ, 'CLASSPATH': ''}, stdout=stdout,
                                  stderr=stderr, timeout=30).returncode == 0

    record('javac:library', True, compile('library', [directory / 'attached' / p for p in sources]))
    classes = directory / 'library-classes'
    for label, _, valid, _ in cases:
        record('javac:' + label, valid, compile(label, [directory / 'attached' / label / 'Use.java'], classes))
    for owner in ('project', 'attached'):
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            extra = len(cases) if owner == 'attached' else 0
            want = {'.java': len(sources) + extra, '.gradle': 2 + extra, '.txt': 1}
            if counts != want:
                raise ToolError('inherited import full inventory incomplete')
            record('inventory:' + owner, want, counts)
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force', '--max-files', '0')
    for label, _, _, used in cases:
        for flags, scope in (([], 'all'), (['--subtree', 'attached'], 'attached')):
            for mode in ([], ['--strict']):
                args = [*flags, 'unused-deps', 'attached::' + label, '--verbose', *mode]
                key = scope + ':' + label + ':' + ('strict' if mode else 'default')
                document = runner.json(*args)
                transitive = not mode and 'base' in used and 'lib' not in used
                want = [('attached::' + owner, 'direct' if owner in used else
                         'transitive' if transitive and owner == 'lib' else 'unused',
                         len(used.get(owner, [])), used.get(owner, [])) for owner in ('base', 'lib')]
                record(key, want, sorted((r['name'], r['category'], r['usage']['direct'], r['examples']['direct'])
                                        for r in document.get('items', [])))
                count = len(used) + int(transitive)
                record(key + ':summary', {'unused': 2-count, 'used': count, 'total': 2,
                       'exported': 0, 'direct': len(used), 'transitive': int(transitive),
                       'xml': 0, 'resources': 0}, document.get('summary'))
                record(key + ':transitive', len(used.get('base', [])) if transitive else 0,
                       next(r['usage']['transitive'] for r in document['items'] if r['name'] == 'attached::lib'))
                _, text = runner.command(*args)
                record(key + ':text', True, f'Total: {2-count} unused, 0 exported, {count} used of 2 dependencies' in text)
    for flags in (['--no-transitive'], ['--no-xml'], ['--no-resources'],
                  ['--no-transitive', '--no-xml', '--no-resources']):
        document = runner.json('unused-deps', 'attached::static-field', '--verbose', *flags)
        transitive = '--no-transitive' not in flags
        record('option:' + ','.join(flags), [('attached::base', 'direct', ['Parent']),
               ('attached::lib', 'transitive' if transitive else 'unused', [])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
    record('local-selection', 'missing_module', runner.json('--local', 'unused-deps', 'attached::member-types').get('empty_reason'))
    record('cwd-selection', 'missing_module', runner.json('unused-deps', 'attached::member-types', cwd=runner.root / 'lib').get('empty_reason'))
    for args in (('rebuild', '--type', 'modules'), ('update',)):
        runner.command(*args)
        record('refresh:' + args[0], ['Exported'], runner.json('unused-deps', 'attached::hidden-owner-type', '--verbose')['items'][0]['examples']['direct'])
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
