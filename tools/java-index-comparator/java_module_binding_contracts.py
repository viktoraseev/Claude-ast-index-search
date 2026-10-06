"""Java declaration provenance on disposable module roots, not MCP equivalence."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-module-binding'}
REASON = ('independent source/state and javac: Java module classpath ownership, '
          'direct/package/member/static imports, visibility, local precedence, API exports '
          'and selected-root JSON/text classifications; not MCP equivalence')


def plan_binding(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-module-declaration-binding'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ['unused-deps:semantic-resolution', 'global:scope-command-matrix']:
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,'module declaration provenance')=0",
                          ('; separate executed Java module declaration provenance fixture covers selected '
                           'direct/API-export classpaths, local precedence, import visibility and static '
                           'metadata across colliding roots; inherited/protected-subclass imports, '
                           'external/platform and ambiguous classpath-order resolution remain pending; '
                           'not MCP equivalence', parent))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('module binding fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='module-binding-', dir=base)).resolve()
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

    # Opposite accessibility/static metadata for the same qualified identities.
    # The primary copies must never supply metadata to an attached classpath.
    cases = [
        ('explicit-hidden', 'import shared.Hidden; class Use { Hidden x; }', False, []),
        ('package-hidden', 'import shared.*; class Use { Hidden x; }', False, []),
        ('member-hidden', 'import shared.Api.*; class Use { Nested x; }', False, []),
        ('static-type-hidden', 'import static shared.Api.*; class Use { Nested x; }', False, []),
        ('static-missing', 'import static shared.Api.*; class Use { int x = read(); }', False, []),
        ('static-private', 'import static shared.Api.VALUE; class Use { int x = VALUE; }', False, []),
        ('static-present', 'import static shared.Api.*; class Use { int x = own(); }', True, ['Api']),
        ('explicit-public', 'import shared.Api; class Use { Api x; }', True, ['Api']),
        ('qualified-public', 'class Use { shared.Api x; }', True, ['Api']),
        ('unreachable-package-shadow', 'import shared.*; class Use { Api x; }', True, ['Api']),
        ('local-priority', 'class Use { shared.Api x; }', True, []),
        ('accessor-public', 'import shared.Api; class Use { Api x; }', True, ['Api']),
        ('static-single-present', 'import static shared.Api.own; class Use { int x = own(); }', True, ['Api']),
    ]
    primary = 'public class Api { public static class Nested {} public static int VALUE=1; public static int read(){return 1;} }'
    attached = 'public class Api { private static class Nested {} private static int VALUE=1; public static int own(){return 1;} }'
    for owner, api, hidden in [('project', primary, 'public class Hidden {}'),
                               ('attached', attached, 'class Hidden {}')]:
        write(owner, 'lib/Api.java', 'package shared; ' + api)
        write(owner, 'lib/Hidden.java', 'package shared; ' + hidden)
        write(owner, 'lib/build.gradle', 'plugins {}')
        write(owner, 'inventory.txt', 'full inventory sentinel')
    # An unconnected module in the SAME root is also outside the classpath.
    write('attached', 'unreachable/Api.java', 'package consumer; public class Api {}')
    write('attached', 'unreachable/build.gradle', 'plugins {}')
    for label, body, _, _ in cases:
        write('attached', f'{label}/Use.java', 'package consumer; ' + body)
        write('attached', f'{label}/build.gradle', 'dependencies { implementation(project(":lib")) }')
    write('attached', 'accessor-public/build.gradle', 'dependencies {\n implementation(projects.lib)\n}')
    write('attached', 'local-priority/Api.java', 'package shared; public class Api {}')
    # A used exported type belongs to its declaring module, with the facade
    # classified as transitive. A private implementation edge is no export.
    write('attached', 'facade/Facade.java', 'package facade; public class Facade {}')
    write('attached', 'facade/build.gradle', 'dependencies { api(project(":lib")) }')
    write('attached', 'exported/Use.java', 'package consumer; class Use { shared.Api x; }')
    write('attached', 'exported/build.gradle', 'dependencies { implementation(project(":facade")) }')
    write('attached', 'sealed/Sealed.java', 'package sealed; public class Sealed {}')
    write('attached', 'sealed/build.gradle', 'dependencies { implementation(project(":lib")) }')
    write('attached', 'not-exported/Use.java', 'package consumer; class Use { shared.Api x; }')
    write('attached', 'not-exported/build.gradle', 'dependencies { implementation(project(":sealed")) }')
    write('project', 'ghost/Type.java', 'package ghost; public class Type {}')
    write('project', 'ghost/build.gradle', 'plugins {}')
    write('attached', 'missing-owner/Use.java', 'class Use { ghost.Type x; }')
    write('attached', 'missing-owner/build.gradle', 'dependencies { implementation(project(":ghost")) }')

    javac = shutil.which('javac')
    if not javac:
        raise ToolError('Java module binding checks require javac')

    def compile(label, sources, classpath=None):
        classes = directory / (label + '-classes')
        args = [javac, '-proc:none', '-d', str(classes)]
        if classpath:
            args += ['-cp', str(classpath)]
        # Do not inherit a user's CLASSPATH or source lookup directory.
        args += ['-sourcepath', str(directory / 'empty-sourcepath')]
        args += [str(p) for p in sources]
        with (directory / (label + '.javac.stdout.log')).open('wb') as stdout, \
                (directory / (label + '.javac.stderr.log')).open('wb') as stderr:
            code = subprocess.run(args, cwd=directory, env={**os.environ, 'CLASSPATH': ''},
                                  stdout=stdout, stderr=stderr, timeout=30).returncode
        return code == 0, classes

    for owner in ['project', 'attached']:
        valid, classes = compile(owner, sorted((directory / owner / 'lib').glob('*.java')))
        record('javac-libraries:' + owner, True, valid)
        if owner == 'attached':
            library_classes = classes
    for label, _, valid, _ in cases:
        compiled, _ = compile(label, sorted((directory / 'attached' / label).glob('*.java')), library_classes)
        record('javac:' + label, valid, compiled)

    for owner in ['project', 'attached']:
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            want = {'.java': 3, '.gradle': 2, '.txt': 1} if owner == 'project' else {
                '.java': len(cases) + 9, '.gradle': len(cases) + 7, '.txt': 1}
            if counts != want:
                raise ToolError('module binding full inventory incomplete')
            record('inventory:' + owner, want, counts)
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force', '--max-files', '0')
    module_names = sorted(['ghost', 'lib', *['attached::' + n for n in
        ['lib', 'unreachable', 'facade', 'exported', 'sealed', 'not-exported', 'missing-owner',
         *[label for label, *_ in cases]]]])
    for flags, scope, names in [([], 'all', module_names), (['--local'], 'local', ['ghost', 'lib']),
                              (['--subtree', 'attached'], 'attached', [n for n in module_names if n.startswith('attached::')])]:
        for limit in [0, 1, 100]:
            output = runner.json(*flags, 'module', '', '--limit', limit)
            record(f'modules:{scope}:{limit}', {'names': names[:limit], 'total': len(names)},
                   {'names': [r['name'] for r in output.get('items', [])], 'total': output['pagination']['total']})
        seed = 'attached::explicit-public'
        output = runner.json(*flags, 'deps', seed)
        record('edges:' + scope, ['attached::lib'] if scope != 'local' else [],
               [r['name'] for r in output.get('items', [])])
        output = runner.json(*flags, 'dependents', 'attached::lib')
        record('reverse:' + scope, sorted(['attached::facade', 'attached::sealed',
               *['attached::' + label for label, *_ in cases]]) if scope != 'local' else [],
               sorted(r['name'] for r in output.get('items', [])))
        output = runner.json(*flags, 'module-route', '--from', seed, '--to', 'attached::lib')
        record('route:' + scope, int(scope != 'local'), output.get('count'))
    for flags, scope in [([], 'all'), (['--subtree', 'attached'], 'attached')]:
        for label, _, _, names in cases:
            for mode in [[], ['--strict']]:
                output = runner.json(*flags, 'unused-deps', 'attached::' + label, '--verbose', *mode)
                key = scope + ':' + label + ':' + ('strict' if mode else 'default')
                want = [('attached::lib', 'direct' if names else 'unused', len(names), names)]
                record(key, want, [(r['name'], r['category'], r['usage']['direct'], r['examples']['direct'])
                                   for r in output.get('items', [])])
                _, text = runner.command(*flags, 'unused-deps', 'attached::' + label, '--verbose', *mode)
                record(key + ':text', True, f'Total: {0 if names else 1} unused, 0 exported, {1 if names else 0} used of 1 dependencies' in text)
        for label, category in [('exported', 'transitive'), ('not-exported', 'unused')]:
            output = runner.json(*flags, 'unused-deps', 'attached::' + label, '--verbose')
            record(scope + ':' + label, [category], [r['category'] for r in output.get('items', [])])
    record('local-selection', 'missing_module', runner.json('--local', 'unused-deps', 'attached::explicit-public').get('empty_reason'))
    record('missing-owner-edge', 'no_dependencies', runner.json('unused-deps', 'attached::missing-owner').get('empty_reason'))
    record('cwd-selection', 'missing_module', runner.json('unused-deps', 'attached::explicit-public',
           cwd=runner.root / 'lib').get('empty_reason'))
    for command in [('rebuild', '--type', 'modules'), ('update',)]:
        runner.command(*command)
        label = command[0]
        record('refresh-edges:' + label, ['attached::lib'], [r['name'] for r in
               runner.json('deps', 'attached::accessor-public').get('items', [])])
        record('refresh-binding:' + label, ['unused'], [r['category'] for r in
               runner.json('unused-deps', 'attached::member-hidden', '--strict').get('items', [])])
    runner.json('subtree', 'remove', 'attached')
    record('removed-owner', ['ghost', 'lib'], [r['name'] for r in runner.json('module', '', '--limit', 100).get('items', [])])
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
