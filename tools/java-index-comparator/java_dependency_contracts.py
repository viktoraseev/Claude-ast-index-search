"""Authored Java import/type dependency identities, independent of MCP and DB."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from unused_dep_contracts import result

FEATURES = {'unused-deps:java-imports', 'unused-deps:java-qualified-types'}
REASON = ('independent source/state: disposable Java explicit/static/import-only and wildcard '
          'imports, qualified/nested/annotation types, package precedence and API exports; '
          'not MCP equivalence or compiler-wide semantic resolution')


def plan_dependencies(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-import-type-identities'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Java dependency artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-deps-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

        def write(path, content):
            path = runner.root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        def module(name, dependencies=(), kind='implementation'):
            write(name + '/build.gradle', 'dependencies {\n' + ''.join(
                f'  {kind}(project(":{dep}"))\n' for dep in dependencies) + '}\n')

        for name in ('alpha', 'beta', 'idle'):
            module(name)
        for name in ('alpha', 'beta'):
            write(f'{name}/Widget.java', f'package {name}; public class Widget {{}}\n')
        write('alpha/Tools.java', 'package alpha; public class Tools { public static int VALUE = 1; }\n')
        write('alpha/Outer.java', 'package alpha; public class Outer { public static class Inner {} }\n')
        write('alpha/Mark.java', 'package alpha; public @interface Mark {}\n')
        write('idle/Unused.java', 'package idle; public class Unused {}\n')
        module('facade', ('alpha',), 'api')
        module('alpha', ('facade',), 'api')  # An export cycle must still terminate.

        # Each entry is one independently authored dependency identity, not a
        # snapshot of native output. Colliding simple names expose false usage.
        cases = [
            ('explicit', 'import alpha.Widget; class Use { Widget field; }', 'alpha', 'Widget'),
            ('import-only', 'import beta.Widget; class Use {}', 'beta', 'Widget'),
            ('qualified', 'class Use { alpha.Widget field; }', 'alpha', 'Widget'),
            ('wildcard', 'import beta.*; class Use { Widget field; }', 'beta', 'Widget'),
            ('unused-wildcard', 'import alpha.*; class Use {}', None, None),
            ('static', 'import static alpha.Tools.VALUE; class Use { int field = VALUE; }', 'alpha', 'Tools'),
            ('static-wildcard', 'import static alpha.Tools.*; class Use { int field = VALUE; }', 'alpha', 'Tools'),
            ('nested', 'import alpha.Outer.Inner; class Use { Inner field; }', 'alpha', 'Inner'),
            ('relative-nested', 'import alpha.Outer; class Use { Outer.Inner field; }', 'alpha', 'Inner, Outer'),
            ('qualified-annotation', '@alpha.Mark class Use {}', 'alpha', 'Mark'),
            ('generic-array', 'class Use { java.util.List<beta.Widget[]> field; }', 'beta', 'Widget'),
            ('record-component', 'record Use(alpha.Widget field) {}', 'alpha', 'Widget'),
            ('local-precedence', 'import alpha.*; class Widget {} class Use { Widget field; }', None, None),
            ('same-package', 'class Use { Widget field; }', 'alpha', 'Widget'),
            ('literal-noise', 'class Use { String field = "alpha.Widget"; /* beta.Widget */ }', None, None),
            ('exported-qualified', 'class Use { alpha.Widget field; }', 'facade', 'Widget'),
        ]
        for number, (label, source, owner, names) in enumerate(cases):
            name = f'consumer{number}'
            deps = ('facade', 'beta', 'idle') if owner == 'facade' else ('alpha', 'beta', 'idle')
            module(name, deps)
            package = 'alpha' if label == 'same-package' else f'fixture.c{number}'
            write(f'{name}/Use.java', f'package {package};\n{source}\n')
        # Independently prove the small authored sources are valid Java. The
        # expected module identities above remain source assertions; compiler
        # success alone is neither CLI support nor MCP differential coverage.
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('Java dependency source validation requires JDK 17+')
        with (runner.directory / 'javac.stdout.log').open('wb') as stdout, \
                (runner.directory / 'javac.stderr.log').open('wb') as stderr:
            compilation = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                *map(str, sorted(runner.root.rglob('*.java')))], stdout=stdout, stderr=stderr, timeout=30)
        if compilation.returncode:
            raise ToolError('authored Java dependency sources did not compile; see private fixture logs')
        runner.command('rebuild', '--force', '--max-files', '0')
        for number, (label, source, owner, names) in enumerate(cases):
            transitive = owner == 'facade'
            feature = 'unused-deps:java-imports' if label in {
                'explicit', 'import-only', 'wildcard', 'unused-wildcard', 'static', 'static-wildcard'
            } else 'unused-deps:java-qualified-types'
            flags = ('--no-xml', '--no-resources', '--verbose') if transitive else ('--strict', '--verbose')
            _, output = runner.command('unused-deps', f'consumer{number}', *flags)
            deps = ['facade', 'beta', 'idle'] if transitive else ['alpha', 'beta', 'idle']
            sections = {'Direct': int(owner is not None and not transitive)}
            if transitive:
                sections['Transitive'] = 1
            expected[feature][label] = {
                'summary': [3 - int(owner is not None), 0, int(owner is not None), 3],
                'unused': sorted(dep for dep in deps if dep != owner), 'exported': [],
                'direct': [(owner, str(len(names.split(', '))), names)] if owner and not transitive else [],
                'via': [('alpha', names)] if transitive else [], 'sections': sections,
                'strict': not transitive, 'transitive_section': transitive,
            }
            actual[feature][label] = result(output, True)
        return expected, actual
