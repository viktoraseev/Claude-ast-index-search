"""Source result ownership on disposable Java classpaths; not MCP equivalence."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'unused-deps:java-source-results'}
REASON = ('independent source/javac/CLI: explicit class generic result substitution, '
          'fields/records/inheritance and initializer-site var chains with access, '
          'ambiguity and attached classpath guards; not MCP equivalence')

# Each input has an authored declaring owner. Invalid-Java guards must remain
# uncredited; VALID_NEGATIVE_CASES separately compile valid ownership controls.
CASES = {
    'generic': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', True),
    'inferred': ('class Box { shared.Child get(){return null;} }', 'Box b', 'var c=b.get(); return c.instance()', True),
    'generic-var': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return c.instance()', True),
    'generic-field': ('class Box<T> { T value; }', 'Box<shared.Child> b', 'b.value.instance()', True),
    'nested-result': ('class Box<T> { T get(){return null;} } class Wrap<U> { Box<U> get(){return null;} }', 'Wrap<shared.Child> b', 'b.get().get().instance()', True),
    'ordered-parameters': ('class Box<A,B> { B get(){return null;} }', 'Box<String,shared.Child> b', 'b.get().instance()', True),
    'inherited-result': ('class Box<T> { T get(){return null;} } class Wrap<U> extends Box<U> {}', 'Wrap<shared.Child> b', 'b.get().instance()', True),
    'record-result': ('record Box<T>(T value) {}', 'Box<shared.Child> b', 'b.value().instance()', True),
    'record-projection': ('class Box<T> { T get(){return null;} } record Wrap<U>(Box<U> value) {}', 'Wrap<shared.Child> b', 'b.value().get().instance()', True),
    'inherited-overload': ('', 'shared.Child b', 'b.choose("x")', True),
    'jdk-list-projection': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', True),
    'boxed-result': ('class Box { shared.Child get(Object value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'unboxed-result': ('class Box { shared.Child get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'array-covariant-result': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, String[] value', 'b.get(value).instance()', True),
    'boxed-exact-result': ('class Box { shared.Child get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'unboxed-widened-result': ('class Box { shared.Child get(long value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'array-nested-result': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, int[][] value', 'b.get(value).instance()', True),
    'primitive-array-object-result': ('class Box { shared.Child get(Object value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', True),
    'strict-before-boxing-result': ('class Box { shared.Child get(long value){return null;} Object get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'strict-before-unboxing-result': ('class Box { shared.Child get(Object value){return null;} Object get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'strict-before-boxing-guard': ('class Box { Object get(long value){return null;} shared.Child get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'primitive-array-covariance-guard': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', False),
    'array-element-boxing-guard': ('class Box { shared.Child get(Integer[] value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', False),
    'widen-before-boxing-guard': ('class Box { shared.Child get(Long value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'unboxing-narrowing-guard': ('class Box { shared.Child get(int value){return null;} }', 'Box b, Long value', 'b.get(value).instance()', False),
    'shadowed-wrapper-guard': ('class Integer {} class Box { shared.Child get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', False),
    'inherited-overload-variable': ('', 'shared.Child b, String value', 'b.choose(value)', True),
    'inherited-overload-null': ('', 'shared.Child b', 'b.choose(null)', True),
    'child-overload-control': ('', 'shared.Child b', 'b.choose(0)', False),
    'overloaded-result': ('class Box { shared.Child get(String value){return null;} Object get(int value){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'overload-private-sibling': ('class Box { shared.Child get(String value){return null;} private Object get(int value){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'argument-declaration-site': ('class Arg {} class Box { shared.Child get(Arg value){return null;} Object get(int value){return null;} }', 'Box b, Arg value', 'class Arg {} return b.get(value).instance()', True),
    'argument-local-shadow-guard': ('class Arg {} class Box { shared.Child get(Arg value){return null;} Object get(int value){return null;} }', 'Box b', 'class Arg {} Arg value=new Arg(); return b.get(value).instance()', False),
    'inherited-overloaded-result': ('class Parent<T> { T get(String value){return null;} } class Box<T> extends Parent<T> { Object get(int value){return null;} }', 'Box<shared.Child> b', 'b.get("x").instance()', True),
    'inherited-arity-result': ('class Parent<T> { T get(int value){return null;} } class Box<T> extends Parent<T> { Object get(){return null;} }', 'Box<shared.Child> b', 'b.get(0).instance()', True),
    'jdk-imported-list': ('import java.util.List; record Box(List<shared.Child> values) {}', 'Box b', 'var c=b.values().get(0); return c.instance()', True),
    'jdk-wildcard-list': ('import java.util.*; record Box(List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', True),
    'jdk-nested-projection': ('record Box(java.util.Map<String,java.util.List<shared.Child>> values) {}', 'Box b', 'b.values().get("x").get(0).instance()', True),
    'jdk-optional-projection': ('record Box(java.util.Optional<shared.Child> value) {}', 'Box b', 'b.value().get().instance()', True),
    'jdk-supplier-projection': ('record Box(java.util.function.Supplier<shared.Child> value) {}', 'Box b', 'b.value().get().instance()', True),
    'inherited-overload-arity-guard': ('', 'shared.Child b', 'b.choose()', False),
    'inherited-overload-type-guard': ('', 'shared.Child b', 'b.choose(new Object())', False),
    'overloaded-result-type-guard': ('class Box { shared.Child get(String value){return null;} Object get(int value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'overload-private-guard': ('class Box { public Object get(String value){return null;} private shared.Child get(int value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'jdk-index-type-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get(0L).instance()', False),
    'jdk-list-arity-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get().instance()', False),
    'jdk-list-wildcard-guard': ('record Box(java.util.List<?> values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-list-raw-guard': ('record Box(java.util.List values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-shadow-guard': ('class List<T> { Object get(int value){return null;} } record Box(List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-static-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'java.util.List.get(0).instance()', False),
    'capture': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)()->c.instance()).getAsInt()', True),
    'reference': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)c::instance).getAsInt()', True),
    'later-shadow': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); class Child {} return c.instance()', True),
    'declaration-site': ('import shared.Child; class Box<T> { T get(){return null;} }', 'Box<Child> b', 'var c=b.get(); class Child {} class Box<T> {} return c.instance()', True),
    'bounded-result': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', True),
    'raw-bounded-result': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box b', 'b.get().instance()', True),
    'bounded-record': ('record Box<T extends shared.Child>(T value) {}', 'Box<shared.Child> b', 'b.value().instance()', True),
    'bounded-record-field': ('record Box<T extends shared.Child>(T value) { int bound(){return value.instance();} }', 'Box<shared.Child> b', '0', True),
    'sibling-bound-guard': ('class Box<T> { T get(){return null;} } class Other<T extends shared.Child> {}', 'Box b', 'b.get().instance()', False),
    'raw-guard': ('class Box<T> { T get(){return null;} }', 'Box b', 'b.get().instance()', False),
    'wildcard-guard': ('class Box<T> { T get(){return null;} }', 'Box<?> b', 'b.get().instance()', False),
    'type-arity-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child,String> b', 'b.get().instance()', False),
    'method-shadow-guard': ('class Box<T> { <T> T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'array-guard': ('class Box<T> { T[] get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'private-guard': ('class Box<T> { private T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'arity-guard': ('class Box<T> { T get(int n){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'ambiguous-guard': ('class Box<T> { T get(Integer n){return null;} T get(String n){return null;} }', 'Box<shared.Child> b', 'b.get(null).instance()', False),
    'block-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', '{var c=b.get();} return c.instance()', False),
    'sibling-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return 0; } int other(){return c.instance()', False),
}

# Valid calls selecting the child overload must not borrow the parent's owner.
# This is a positive Java compilation with an authored negative ownership result.
VALID_NEGATIVE_CASES = {'child-overload-control'}


def plan_results(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-source-result-ownership'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        # This finite child never closes the full shared/semantic parents.


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('source result fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-results-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
        feature = next(iter(FEATURES))

        def write(relative, source, root=None):
            path = (root or runner.root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source + '\n')
            return path

        write('base/build.gradle', 'plugins {}')
        write('lib/build.gradle', 'dependencies { api(project(":base")) }')
        base_source = write('base/Base.java', 'package shared; public class Base { public int instance(){return 1;} public int choose(String value){return 2;} }')
        child_source = write('lib/Child.java', 'package shared; public class Child extends Base { public int choose(int value){return 3;} }')
        valid, invalid = [], []
        for label, (declarations, parameter, body, used) in CASES.items():
            body = body if 'return ' in body else 'return ' + body
            source = 'package fixture.' + label.replace('-', '_') + '; ' + declarations + ' class Use { int run(' + parameter + '){' + body + ';} }'
            path = write(label + '/Use.java', source)
            write(label + '/build.gradle', 'dependencies { implementation(project(":base")); implementation(project(":lib")) }')
            if used or label in VALID_NEGATIVE_CASES:
                valid.append(path)
            else:
                invalid.append(path)
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('source result validation requires javac')
        with (runner.directory / 'javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                                     str(base_source), str(child_source), *map(str, valid)],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored positive result fixtures failed javac; see private log')
        for path in invalid:
            with (runner.directory / (path.parent.name + '.javac.log')).open('wb') as log:
                result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'guard-classes'),
                    str(base_source), str(child_source), str(path)], stdout=log, stderr=log, timeout=30)
            if result.returncode == 0:
                raise ToolError('a claimed ambiguity/access guard compiled; retain as a pending Java obligation')
        runner.command('rebuild', '--force', '--max-files', '0')

        def record(label, want, got):
            expected[feature][label], actual[feature][label] = want, got

        def classifications(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            if not isinstance(doc.get('items'), list) or doc.get('error'):
                raise ToolError('source results did not execute unused-deps')
            return sorted((row['name'], row['category'], row['examples']['direct']) for row in doc['items'])

        for label, (declarations, parameter, _, used) in CASES.items():
            # Even a guard has the explicitly written Child type as a direct
            # dependency. Its downstream Base must never be inferred by name.
            child = ['Child'] if 'Child' in parameter + declarations else []
            want = [('base', 'direct' if used else 'unused', ['Base'] if used else []),
                    ('lib', 'direct' if child else 'unused', child)]
            for flags in ((), ('--strict',)):
                record(label + ':' + str(bool(flags)), want, classifications(label, flags))
            _, text = runner.command('unused-deps', label, '--verbose', '--strict')
            record(label + ':text', True, ('Base' in text) == used)

        # A provider result is resolved in its own imports, across the selected
        # attached classpath. Colliding primary definitions are a negative
        # control, never a reason to borrow the primary root's declaring owner.
        attached = runner.directory / 'attached'
        attached.mkdir()
        for relative, source in {
            'base/build.gradle': 'plugins {}',
            'base/Base.java': 'package shared; public class Base { public int instance(){return 2;} }',
            'lib/build.gradle': 'dependencies { api(project(":base")) }',
            'lib/Child.java': 'package shared; public class Child extends Base {}',
            'box/build.gradle': 'plugins {}',
            'box/Box.java': 'package api; public class Box<T> { public T get(){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box<shared.Child> b){var c=b.get(); return c.instance();} }',
        }.items():
            write(relative, source, attached)
        runner.command('subtree', 'add', 'attached', '../attached')
        with (runner.directory / 'attached.javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'attached-classes'),
                *map(str, sorted(attached.rglob('*.java')))], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('attached source result fixtures failed javac; see private log')
        runner.command('rebuild', '--force', '--max-files', '0')
        want = [('attached::base', 'direct', ['Base']), ('attached::box', 'direct', ['Box']), ('attached::lib', 'direct', ['Child'])]
        for flags in ((), ('--strict',)):
            record('attached:' + str(bool(flags)), want, classifications('consumer', flags, attached))
        # Keep exact source expectations after incremental graph/module refresh.
        write('box/Box.java', 'package api; public class Box<T> { public T get(){return null;} public int unused(){return 0;} }', attached)
        runner.command('update')
        record('attached:update', want, classifications('consumer', ('--strict',), attached))
        write('box/Wrap.java', 'package api; public class Wrap<U> extends Box<U> {}', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Wrap<shared.Child> b){var c=b.get(); return c.instance();} }', attached)
        runner.command('update')
        record('attached:inherited-result', [('attached::base', 'direct', ['Base']),
            ('attached::box', 'direct', ['Box', 'Wrap']), ('attached::lib', 'direct', ['Child'])],
            classifications('consumer', ('--strict',), attached))
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box<shared.Child> b){var c=b.get(); return c.instance();} }', attached)
        write('box/Box.java', 'package api; public class Box<T> { public Object get(){return null;} }', attached)
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', []), ('attached::box', 'direct', ['Box']), ('attached::lib', 'direct', ['Child'])],
               classifications('consumer', ('--strict',), attached))
        return expected, actual
