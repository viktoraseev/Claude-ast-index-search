"""Composition and resource limits on authored Java project markers, not MCP truth."""
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner


FEATURES = {'detect-stacks:composition-budgets'}
REASON = ('independent source/state: disposable Java project composition, module-local '
          'build markers, marker limits and exhausted filesystem/Gradle budgets; not MCP equivalence')
LABELS = {'android': 'Android (Kotlin/Java/JVM)', 'web': 'Web (TypeScript/JavaScript)',
          'kmp': 'Kotlin Multiplatform'}
WARNING = 'Warning: stack detection reached its scan budget; detected stacks may be incomplete\n'


def plan_stacks(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-composition-budgets'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('stack artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    # The entry-boundary fixture has 20,001 tiny files. Release it immediately;
    # these are owned synthetic inputs, not persistent copies of target source.
    with tempfile.TemporaryDirectory(prefix='stacks-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.environment['AST_INDEX_MAX_FILE_SIZE'] = str(1024 * 1024)
        expected, actual = {}, {}

        def project(label, files=None, directories=()):
            runner.root = runner.directory / label
            runner.root.mkdir()
            (runner.root / 'Probe.java').write_text('class Probe {}\n')
            for name, content in (files or {}).items():
                path = runner.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            for name in directories:
                (runner.root / name).mkdir(parents=True, exist_ok=True)

        def check(label, stacks, *, kmp=False, polyglot=False, truncated=False):
            wanted = [{'kind': kind, 'label': LABELS[kind], 'markers': markers}
                      for kind, markers in stacks]
            environment = {'AST_INDEX_ROOT': str(runner.root)}
            output = runner.json('detect-stacks', environment=environment)
            diagnostic = (runner.directory / f'{runner.sequence:03d}.stderr.log').read_text()
            expected[label + ':json'] = {'stacks': wanted, 'is_kmp': kmp,
                                        'is_polyglot': polyglot, 'scan_truncated': truncated}
            actual[label + ':json'] = output
            expected[label + ':diagnostic'] = WARNING if truncated else ''
            actual[label + ':diagnostic'] = diagnostic
            _, text = runner.command('detect-stacks', environment=environment)
            diagnostic = (runner.directory / f'{runner.sequence:03d}.stderr.log').read_text()
            if not wanted:
                expected[label + ':text-empty'] = True
                actual[label + ':text-empty'] = text.startswith('No known project stacks detected at this root.\n')
            else:
                setup = 'Kotlin Multiplatform' if kmp else 'Polyglot / monorepo' if polyglot else 'Single-stack'
                want_text = f'Detected setup: {setup}\n\n'
                for row in wanted:
                    want_text += f"  {row['label']} ({row['kind']})\n"
                    want_text += ''.join(f'    - {marker}\n' for marker in row['markers'])
                expected[label + ':text'], actual[label + ':text'] = want_text, text
            expected[label + ':text-diagnostic'] = WARNING if truncated else ''
            actual[label + ':text-diagnostic'] = diagnostic

        project('plain-java')
        check('plain-java', [])
        project('maven', {'pom.xml': '<project/>'})
        check('maven', [('android', ['pom.xml'])])
        project('java-web', {'pom.xml': '<project/>', 'package.json': '{}'})
        check('java-web', [('android', ['pom.xml']), ('web', ['package.json'])], polyglot=True)

        plugin = 'plugins { kotlin("multiplatform") }\n'
        project('plugin-only', {'build.gradle.kts': plugin})
        check('plugin-only', [('android', ['build.gradle.kts'])])
        project('source-set-only', {'build.gradle.kts': 'plugins { java }'}, ['src/commonMain'])
        check('source-set-only', [('android', ['build.gradle.kts'])])
        project('separate-modules', {'a/build.gradle.kts': plugin, 'b/build.gradle.kts': ''},
                ['b/src/commonMain'])
        check('separate-modules', [('android', ['a/build.gradle.kts', 'b/build.gradle.kts'])])
        for extra in (False, True):
            label = 'kmp-web' if extra else 'kmp-java'
            files = {'shared/build.gradle.kts': plugin}
            if extra:
                files['package.json'] = '{}'
            project(label, files, ['shared/src/commonMain', 'shared/src/jvmMain'])
            stacks = [('android', ['shared/build.gradle.kts']),
                      ('kmp', ['shared/src/commonMain', 'shared/build.gradle.kts'])]
            if extra:
                stacks.append(('web', ['package.json']))
            check(label, stacks, kmp=True, polyglot=extra)

        files = {'pom.xml': '<project/>', **{f'm{i:02d}/pom.xml': '<project/>' for i in range(33)}}
        files.update({f'{name}/build.gradle': plugin for name in ('.hidden', 'build', 'tests/fixture')})
        project('marker-cap', files)
        check('marker-cap', [('android', ['pom.xml', *[f'm{i:02d}/pom.xml' for i in range(31)]])])

        for depth in (7, 8):
            label = f'depth-{depth}'
            project(label, {'pom.xml': '<project/>'}, ['/'.join(['nested'] * depth)])
            check(label, [('android', ['pom.xml'])], truncated=depth == 8)

        project('entry-budget', {'pom.xml': '<project/>'})
        # Probe.java + pom.xml + 19,998 payloads equals the public 20,000-entry cap.
        for i in range(19998):
            (runner.root / f'payload-{i:05d}.txt').touch()
        check('entry-exact', [('android', ['pom.xml'])])
        (runner.root / 'payload-extra.txt').touch()
        check('entry-over', [('android', ['pom.xml'])], truncated=True)

        project('gradle-file-budget', {f'm{i:02d}/build.gradle': '' for i in range(64)})
        markers = [f'm{i:02d}/build.gradle' for i in range(32)]
        check('gradle-file-exact', [('android', markers)])
        (runner.root / 'm64').mkdir()
        (runner.root / 'm64/build.gradle').touch()
        check('gradle-file-over', [('android', markers)], truncated=True)

        project('gradle-byte-budget', {f'm{i}/build.gradle': ' ' * (1024 * 1024) for i in range(4)})
        markers = [f'm{i}/build.gradle' for i in range(4)]
        check('gradle-byte-exact', [('android', markers)])
        (runner.root / 'm4').mkdir()
        (runner.root / 'm4/build.gradle').write_text(' ')
        check('gradle-byte-over', [('android', [*markers, 'm4/build.gradle'])], truncated=True)
        project('gradle-single-file-budget', {'build.gradle': ' ' * (1024 * 1024 + 1)})
        check('gradle-single-file-over', [('android', ['build.gradle'])], truncated=True)
        del runner.environment['AST_INDEX_MAX_FILE_SIZE']
        project('gradle-default-single-file-budget', {'build.gradle': ' ' * 1_000_000})
        check('gradle-default-single-file-exact', [('android', ['build.gradle'])])
        (runner.root / 'build.gradle').write_text(' ' * 1_000_001)
        check('gradle-default-single-file-over', [('android', ['build.gradle'])], truncated=True)
        return expected, actual
