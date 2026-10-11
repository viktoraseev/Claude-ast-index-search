"""Duplicate Java source owners are unresolved, never ordered by native paths.

The CLI has no compiler classpath-order option. A referenced duplicate must
report ambiguity instead of claiming either dependency is unused. Consumer
sources still take precedence. These are source/javac/CLI checks, not MCP truth.
"""
import os
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'unused-deps:java-source-classpath-ambiguity'
FEATURES = {FEATURE}
SUBJECT = 'duplicate-source-classpath-order'
REASON = ('independent source/javac/CLI: duplicate Java source classpath owners, '
          'reversed descriptor/path order, explicit ambiguity, consumer precedence, '
          'API exports, attached ownership and refresh; not MCP equivalence')


def plan_contracts(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('Java classpath fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-classpath-', dir=base))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    # Reverse source-path precedence independently of descriptor precedence.
    for layout in ('forward', 'reverse'):
        case_dir = directory / layout
        case_dir.mkdir()
        runner = Runner(binary, case_dir)
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        root = runner.root if layout == 'forward' else case_dir / 'attached'
        prefix = '' if layout == 'forward' else 'attached::'

        def write(path, source):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source + '\n')
            return target

        sources = {}
        for provider, member in zip(('alpha', 'omega'),
                                    ('left', 'right') if layout == 'forward' else ('right', 'left')):
            sources[provider] = write(provider + '/Api.java', 'package shared; public class Api { '
                'public static class Nested {} public static int VALUE=1; '
                'public static int ' + member + '(){return 1;} }')
            write(provider + '/build.gradle', 'plugins {}')
        uses = {
            'qualified': 'class Use { shared.Api value; }',
            'imported': 'import shared.Api; class Use { Api value; }',
            'wildcard': 'import shared.*; class Use { Api value; }',
            'nested': 'class Use { shared.Api.Nested value; }',
            'static': 'import static shared.Api.VALUE; class Use { int value=VALUE; }',
            'receiver': 'class Use { int use(shared.Api value){return value.left();} }',
        }
        for order, providers in [('forward', ('alpha', 'omega')), ('reverse', ('omega', 'alpha'))]:
            deps = 'dependencies { ' + '; '.join('implementation(project(":' + p + '"))' for p in providers) + ' }'
            for label, source in uses.items():
                write(order + '-' + label + '/Use.java', source)
                write(order + '-' + label + '/build.gradle', deps)
        for label, deps, source in [
                ('local', ('alpha', 'omega'), 'class Use { shared.Api value; }'),
                ('single', ('alpha',), 'class Use { shared.Api value; }'),
                ('unreferenced', ('alpha', 'omega'), 'class Use {}'),
                ('exported', ('facade', 'omega'), 'class Use { shared.Api value; }'),
                ('hidden', ('sealed', 'omega'), 'class Use { shared.Api value; }')]:
            write(label + '/Use.java', source)
            write(label + '/build.gradle', 'dependencies { ' + '; '.join(
                'implementation(project(":' + p + '"))' for p in deps) + ' }')
        write('local/Api.java', 'package shared; public class Api {}')
        write('facade/build.gradle', 'dependencies { api(project(":alpha")) }')
        write('sealed/build.gradle', 'dependencies { implementation(project(":alpha")) }')
        write('inventory.txt', 'full file-type inventory sentinel')
        # XML syntax is explicitly out of scope; its presence cannot skip Java.
        write('AndroidManifest.xml', '<manifest/>')

        def compile(label, inputs, classpath=()):
            output = case_dir / ('classes-' + label)
            with (case_dir / (label + '.javac.log')).open('wb') as log:
                result = subprocess.run(['javac', '-proc:none', '-sourcepath', str(case_dir / 'no-sources'),
                    '-cp', os.pathsep.join(map(str, classpath)), '-d', str(output), *map(str, inputs)],
                    cwd=case_dir, env={**os.environ, 'CLASSPATH': ''}, stdout=log, stderr=log, timeout=30)
            return result.returncode == 0, output

        classes = {}
        for provider, path in sources.items():
            valid, classes[provider] = compile(provider, [path])
            record(layout + ':javac:' + provider, True, valid)
        for providers in [('alpha', 'omega'), ('omega', 'alpha')]:
            valid, _ = compile('probe-' + providers[0], [root / 'forward-receiver/Use.java'],
                               [classes[p] for p in providers])
            record(layout + ':javac-order:' + providers[0],
                   (providers[0] == 'alpha') == (layout == 'forward'), valid)

        state = connect(case_dir / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, root)
            record(layout + ':inventory', {'.java': 20, '.gradle': 21, '.txt': 1, '.xml': 1},
                   dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension')))
        finally:
            state.close()
        runner.root.mkdir(exist_ok=True)
        (runner.root / '.git').mkdir()
        if layout == 'reverse':
            (runner.root / 'Api.java').write_text('package shared; public class Api {}')
            runner.command('rebuild', '--force', '--max-files', '0')
            runner.json('subtree', 'add', 'attached', '../attached')
        runner.command('rebuild', '--force', '--max-files', '0')

        def ambiguous(label, flags, key):
            identity = 'shared.Api.Nested' if label.endswith('-nested') else 'shared.Api'
            diagnostic = "ambiguous Java source classpath type '" + identity + "'"
            for format in ('json', 'text'):
                code, output = runner.command('--format', format, 'unused-deps', prefix + label,
                                              '--verbose', *flags, acceptable=(0, 1))
                error = (case_dir / f'{runner.sequence:03d}.stderr.log').read_text()
                record(key + ':' + format, {'code': 1, 'ambiguity': True, 'no_classification': True},
                       {'code': code, 'ambiguity': diagnostic in error,
                        'no_classification': output == '' if format == 'json' else 'Total:' not in output})

        for label in [order + '-' + use for order in ('forward', 'reverse') for use in uses] + ['exported']:
            for flags in ((), ('--strict',), ('--no-transitive', '--no-xml', '--no-resources')):
                ambiguous(label, flags, layout + ':' + label + ':' + ','.join(flags))
        controls = {'local': [('alpha', 'unused'), ('omega', 'unused')],
                    'single': [('alpha', 'direct')],
                    'unreferenced': [('alpha', 'unused'), ('omega', 'unused')],
                    'hidden': [('omega', 'direct'), ('sealed', 'unused')]}

        def control(label, key):
            doc = runner.json('unused-deps', prefix + label, '--verbose', '--strict')
            record(key, [(prefix + name, category) for name, category in controls[label]],
                   sorted((row['name'], row['category']) for row in doc['items']))

        for label in controls:
            control(label, layout + ':control:' + label)
        original = sources['omega'].read_text()
        sources['omega'].write_text('package distinct; public class Api {}\n')
        runner.command('update')
        doc = runner.json('unused-deps', prefix + 'forward-qualified', '--strict')
        record(layout + ':resolved-update', [(prefix + 'alpha', 'direct'), (prefix + 'omega', 'unused')],
               sorted((row['name'], row['category']) for row in doc['items']))
        sources['omega'].write_text(original)
        runner.command('rebuild', '--force', '--max-files', '0')
        ambiguous('forward-qualified', ('--strict',), layout + ':restored-rebuild')
        control('local', layout + ':local-rebuild')
    return expected, actual
