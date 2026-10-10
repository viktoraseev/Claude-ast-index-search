"""Java-referenced definitions and indexed R access; authored source/javac/CLI."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
from android_contracts import observation, applicability, SCHEMA as ANDROID_SCHEMA
import mobile_contracts

FEATURE = 'resource-usages:java-definition-bindings'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-resource-definition-bindings-v1'
REASON = ('independent source/javac/CLI: Java-referenced file resource kinds and '
          'indexed R type/field visibility, static and missing-member guards; '
          'XML syntax and foreign parsers excluded; not MCP equivalence')
# These are the file-definition directories advertised by the existing Android
# inventory/indexer, not an extension to XML syntax or build execution.
FILE_KINDS = ('drawable', 'mipmap', 'layout', 'menu', 'navigation', 'anim',
              'animator', 'color', 'font', 'interpolator', 'raw', 'transition', 'xml')
ACCESS_NAMES = ('visible', 'package_only', 'protected_only', 'private_only',
                'instance_only', 'missing')


def plan_definitions(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java definition fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-resource-definitions-', dir=base) as temporary:
        directory = Path(temporary).resolve()
        runner = Runner(binary, directory)
        expected, actual = {}, {}

        def write(path, content):
            file = runner.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
            return file

        for module in ('app', 'library'):
            write(module + '/build.gradle', "plugins { id 'com.android.library' }\n"
                  "android { namespace 'fixture." + module + "' }\n"
                  + ('dependencies { implementation(project(":library")) }\n' if module == 'app' else ''))
        for kind in FILE_KINDS:
            for folder in (kind, kind + '-land'):
                filename = {'drawable': 'entry.9.png', 'mipmap': 'entry.png',
                            'font': 'entry.ttf', 'raw': 'entry.bin'}.get(kind, 'entry.xml')
                write(f'library/src/main/res/{folder}/{filename}', '<root/>\n')
        write('library/src/main/res/values/strings.xml', '<resources>' +
              ''.join(f'<string name="{name}">Value</string>' for name in ACCESS_NAMES) + '</resources>')
        indexed = write('library/R.java', '''package fixture.library;
public class R {
 public static class string {
  public static int visible;
  static int package_only;
  protected static int protected_only;
  private static int private_only;
  public int instance_only;
 }
 int nest() { return fixture.library.R.string.private_only; }
}
''')
        use = write('app/Use.java', 'package fixture.app;\nclass Use {\n' +
                    ''.join(f' int v{i}=fixture.library.R.{kind}.entry;\n'
                            for i, kind in enumerate(FILE_KINDS)) +
                    ' int value=fixture.library.R.string.visible;\n}\n')
        same = write('library/Same.java', 'package fixture.library;\n'
                     'class Same { int x=R.string.package_only; }\n')
        child = write('app/Child.java', 'package fixture.app;\n'
                      'class Child extends fixture.library.R.string {\n'
                      ' int x=fixture.library.R.string.protected_only;\n}\n')
        # Stubs for file kinds validate the positive inputs but are never indexed.
        stub = directory / 'R.java'
        stub.write_text(indexed.read_text().replace(' int nest()',
            ''.join(f' public static class {kind} {{ public static int entry; }}\n'
                    for kind in FILE_KINDS) + ' int nest()'))

        def compile_sources(label, sources, accepted):
            with (directory / (label + '.javac.log')).open('wb') as log:
                result = subprocess.run(['javac', '-proc:none', '-d', str(directory / label),
                                         str(stub), *map(str, sources)],
                                        stdout=log, stderr=log, timeout=30)
            expected['javac:' + label], actual['javac:' + label] = accepted, result.returncode == 0
            if (result.returncode == 0) != accepted:
                raise ToolError('Java definition compiler guard disagrees; see private log')

        compile_sources('positive', [use, same, child], True)
        # The stub models generated file classes; those absent from the indexed
        # R source must not be guessed when that source is authoritative.
        indexed.write_text(stub.read_text())
        runner.command('rebuild', '--force', '--max-files', 0)

        def usage(label, kind, name, sites):
            _, output = runner.command('resource-usages', '@' + kind + '/' + name)
            expected[label] = {**observation(''), 'locations': sorted(sites),
                               'groups': [('Kotlin/Java', len(sites))] if sites else [],
                               'total': len(sites)}
            actual[label] = observation(output)

        for line, kind in enumerate(FILE_KINDS, 3):
            usage('definition:' + kind, kind, 'entry', [('app/Use.java', line)])
            _, output = runner.command('resource-usages', '--unused', '--module', 'library', '--type', kind)
            expected['unused:' + kind] = {**observation(''), 'unused_total': 0}
            actual['unused:' + kind] = observation(output)
        usage('access:public', 'string', 'visible', [('app/Use.java', len(FILE_KINDS) + 3)])
        usage('access:package', 'string', 'package_only', [('library/Same.java', 2)])
        usage('access:protected', 'string', 'protected_only', [('app/Child.java', 3)])
        usage('access:nest', 'string', 'private_only', [('library/R.java', len(FILE_KINDS) + 10)])
        for name in ACCESS_NAMES[1:]:
            kind = 'string'
            negative = write('app/Rejected.java', 'package fixture.app;\n'
                             f'class Rejected {{ int x=fixture.library.R.{kind}.{name}; }}\n')
            compile_sources('reject-' + name, [negative], False)
            runner.command('update')
            # Keep the earlier positive sites: rejection cannot drop the family.
            sites = {'package_only': [('library/Same.java', 2)],
                     'protected_only': [('app/Child.java', 3)],
                     'private_only': [('library/R.java', len(FILE_KINDS) + 10)]}.get(name, [])
            usage('rejected:' + name, kind, name, sites)
            negative.unlink()
        # An indexed inaccessible nested owner must not be treated as an absent
        # generated class. Keep a legal nest site as the positive control.
        hidden = indexed.read_text().replace('public static class string', 'private static class string')
        indexed.write_text(hidden)
        stub.write_text(hidden)
        negative = write('app/Rejected.java', 'package fixture.app;\n'
                         'class Rejected { int x=fixture.library.R.string.visible; }\n')
        compile_sources('reject-owner', [negative], False)
        runner.command('update')
        usage('rejected:owner', 'string', 'visible', [])
        usage('retained:nest', 'string', 'private_only', [('library/R.java', len(FILE_KINDS) + 10)])
        negative.unlink()
        state = connect(directory / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' +
                               mobile_contracts.SCHEMA + ANDROID_SCHEMA)
            mobile_contracts.inventory(state, runner.root)
            inventory = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            expected['inventory'] = {'.java': 4, '.gradle': 2, '.xml': 2 * (len(FILE_KINDS) - 4) + 1,
                                     '.png': 4, '.ttf': 2, '.bin': 2}
            actual['inventory'] = inventory
            expected['applicability'], actual['applicability'] = 'pending', applicability(state, runner.root)[0]
        finally:
            state.close()
        return {FEATURE: expected}, {FEATURE: actual}
