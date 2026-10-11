"""Java wildcard invocation ownership on authored source; not MCP equivalence."""
from itertools import product
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-wildcard-formals'}
SUBJECT = 'disposable-java-wildcard-formals-v1'
REASON = ('independent source/javac/CLI: wildcard formal containment and method '
          'inference, declaration-site arguments, source ancestry, invocation phases '
          'and attached declaring ownership; not MCP equivalence')

# Declaration, parameters, expression/body, downstream Base member is used.
# Base is not named in these consumers. Its credit requires resolving the call.
CASES = {
    'upper-fixed': ('class Box { shared.Child get(Carrier<? extends shared.Child> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    'lower-fixed': ('class Box { shared.Child get(Carrier<? super shared.Child> c){return null;} }', 'Box b, Carrier<Object> c', 'b.get(c).instance()', True),
    'unbounded': ('class Box { shared.Child get(Carrier<?> c){return null;} }', 'Box b, Carrier<String> c', 'b.get(c).instance()', True),
    'null-fixed': ('class Box { shared.Child get(Carrier<?> c){return null;} }', 'Box b', 'b.get(null).instance()', True),
    'upper-actual': ('class Box { shared.Child get(Carrier<? extends shared.Child> c){return null;} }', 'Box b, Carrier<? extends shared.Child> c', 'b.get(c).instance()', True),
    'lower-actual': ('class Box { shared.Child get(Carrier<? super shared.Child> c){return null;} }', 'Box b, Carrier<? super Object> c', 'b.get(c).instance()', True),
    'unknown-actual': ('class Box { shared.Child get(Carrier<?> c){return null;} }', 'Box b, Carrier<?> c', 'b.get(c).instance()', True),
    'class-slot': ('class Box<T> { T get(Carrier<? extends T> c){return null;} }', 'Box<shared.Child> b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    'method-upper': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    'method-upper-actual': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<? extends shared.Child> c', 'b.get(c).instance()', True),
    'method-lower': ('class Box { <T> T get(Carrier<? super T> c, T value){return value;} }', 'Box b, Carrier<Object> c, shared.Child value', 'b.get(c,value).instance()', True),
    'method-lower-first': ('class Box { <T> T get(T value, Carrier<? super T> c){return value;} }', 'Box b, Carrier<Object> c, shared.Child value', 'b.get(value,c).instance()', True),
    'method-lower-bound': ('class Box { <T extends shared.Child> T get(Carrier<? super T> c){return null;} }', 'Box b, Carrier<Object> c', 'b.get(c).instance()', True),
    'lower-witness': ('class Box { <T> T get(Carrier<? super T> c){return null;} }', 'Box b, Carrier<Object> c', 'b.<shared.Child>get(c).instance()', True),
    'upper-witness': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.<shared.Child>get(c).instance()', True),
    'source-ancestor': ('class Derived<A,B> extends Carrier<B> {} class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Derived<String,shared.Child> c', 'b.get(c).instance()', True),
    'ancestor-upper-actual': ('class Derived<A,B> extends Carrier<B> {} class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Derived<String,? extends shared.Child> c', 'b.get(c).instance()', True),
    'ancestor-lower-actual': ('class Derived<A,B> extends Carrier<B> {} class Box { shared.Child get(Carrier<? super shared.Child> c){return null;} }', 'Box b, Derived<String,? super shared.Child> c', 'b.get(c).instance()', True),
    'ancestor-unknown': ('class Derived<A,B> extends Carrier<B> {} class Box { shared.Child get(Carrier<?> c){return null;} }', 'Box b, Derived<String,?> c', 'b.get(c).instance()', True),
    'lower-only': ('class Box { <T> T get(Carrier<? super T> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    # A lower-bounded capture alone does not infer its lower bound as a read type.
    'lower-wildcard-inferred': ('class Box { <T> T get(Carrier<? super T> c){return null;} }', 'Box b, Carrier<? super shared.Child> c', 'b.get(c).instance()', False),
    'lower-wildcard-value': ('class Box { <T> T get(Carrier<? super T> c,T v){return v;} }', 'Box b, Carrier<? super shared.Child> c,shared.Child v', 'b.get(c,v).instance()', True),
    'upper-array-actual': ('class Box { <T> T get(Carrier<? extends T[]> c){return null;} }', 'Box b, Carrier<? extends shared.Child[]> c', 'b.get(c).instance()', True),
    'lower-array-fixed': ('class Box { shared.Child get(Carrier<? super shared.Child[]> c){return null;} }', 'Box b, Carrier<Object[]> c', 'b.get(c).instance()', True),
    'inherited-method': ('class Parent<T> { T get(Carrier<? extends T> c){return null;} } class Box<U> extends Parent<U> {}', 'Box<shared.Child> b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    'list': ('class Box { <T> T get(java.util.List<? extends T> c){return null;} }', 'Box b, java.util.List<shared.Child> c', 'b.get(c).instance()', True),
    'nested': ('class Box { <T> T get(Carrier<? extends Carrier<? extends T>> c){return null;} }', 'Box b, Carrier<Carrier<shared.Child>> c', 'b.get(c).instance()', True),
    'array-slot': ('class Box { <T> T get(Carrier<? extends T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'postfix': ('class Box { <T> T get(Carrier<? extends T> c[]){return null;} }', 'Box b, Carrier<shared.Child>[] c', 'b.get(c).instance()', True),
    'spread': ('class Box { <T> T get(Carrier<? extends T>... c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c,c).instance()', True),
    'fixed-spread': ('class Box { <T> T get(Carrier<? extends T>... c){return null;} }', 'Box b, Carrier<shared.Child>[] c', 'b.get(c).instance()', True),
    'capture-site': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'class Carrier<T> {} var value=b.get(c); return ((java.util.function.IntSupplier)value::instance).getAsInt()', True),
    'new-site': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b', 'b.get(new Carrier<shared.Child>()).instance()', True),
    'cast-site': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Object c', 'b.get((Carrier<shared.Child>)c).instance()', True),
    'bounded-empty-spread': ('class Box { <T extends shared.Child> T get(Carrier<? extends T>... c){return null;} }', 'Box b', 'b.get().instance()', True),
    'overload': ('class Box { <T> T get(Carrier<? extends T> c){return null;} Object get(Object c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c).instance()', True),
    'upper-guard': ('class Box { shared.Child get(Carrier<? extends shared.Child> c){return null;} }', 'Box b, Carrier<Object> c', 'b.get(c).instance()', False),
    'lower-guard': ('class Box { shared.Child get(Carrier<? super shared.Child> c){return null;} }', 'Box b, Carrier<String> c', 'b.get(c).instance()', False),
    'variance-guard': ('class Box { shared.Child get(Carrier<? extends shared.Child> c){return null;} }', 'Box b, Carrier<? super shared.Child> c', 'b.get(c).instance()', False),
    'lower-variance-guard': ('class Box { shared.Child get(Carrier<? super shared.Child> c){return null;} }', 'Box b, Carrier<? extends shared.Child> c', 'b.get(c).instance()', False),
    'unknown-upper-guard': ('class Box { shared.Child get(Carrier<? extends shared.Child> c){return null;} }', 'Box b, Carrier<?> c', 'b.get(c).instance()', False),
    'nested-invariant-guard': ('class Box { shared.Child get(Carrier<Carrier<? extends shared.Child>> c){return null;} }', 'Box b, Carrier<Carrier<shared.Child>> c', 'b.get(c).instance()', False),
    'capture-array-overload-guard': ('class Derived<T> extends Carrier<T[]> {} class Box { Object get(Carrier<?> c){return null;} shared.Child get(Object c){return null;} }', 'Box b, Derived<? extends shared.Child> c', 'b.get(c).instance()', False),
    'capture-array-generic-overload-guard': ('class Derived<T> extends Carrier<T[]> {} class Box { <T> Object get(Carrier<T> c){return null;} shared.Child get(Object c){return null;} }', 'Box b, Derived<? extends shared.Child> c', 'b.get(c).instance()', False),
    'lower-only-read-guard': ('class Box { <T> T get(Carrier<? super T> c){return null;} }', 'Box b, Carrier<Object> c', 'b.get(c).instance()', False),
    'invariant-guard': ('class Box { shared.Child get(Carrier<shared.Child> c){return null;} }', 'Box b, Carrier<? extends shared.Child> c', 'b.get(c).instance()', False),
    'bound-guard': ('class Box { <T extends String> shared.Child get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<shared.Child> c', 'b.get(c).instance()', False),
    'witness-guard': ('class Box { <T> T get(Carrier<? extends T> c){return null;} }', 'Box b, Carrier<String> c', 'b.<shared.Child>get(c).instance()', False),
    'lower-inference-guard': ('class Box { <T> T get(Carrier<? super T> c,T v){return v;} }', 'Box b, Carrier<String> c, shared.Child v', 'b.get(c,v).instance()', False),
    'rank-guard': ('class Box { <T> T get(Carrier<? extends T[][]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', False),
    'private-guard': ('class Box { private shared.Child get(Carrier<?> c){return null;} }', 'Box b, Carrier<String> c', 'b.get(c).instance()', False),
    'arity-guard': ('class Box { shared.Child get(Carrier<?> c){return null;} }', 'Box b, Carrier<String> c', 'b.get(c,c).instance()', False),
    'ambiguity-guard': ('class Box { shared.Child get(Carrier<?> c){return null;} shared.Child get(String c){return null;} }', 'Box b', 'b.get(null).instance()', False),
}


def plan_formals(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': SUBJECT}), feature, SUBJECT))
        # This executed family never closes the semantic parent by itself.
        note = ('; separate Java wildcard formal fixture covers fixed/class/method containment, '
                'upper inference and lower constraints, source ancestry, array/spread phases and '
                'declaring ownership; target/common/intersection inference, capture identity, '
                'override erasure, raw unchecked formals and classpath-order obligations remain '
                'pending; independent source/javac/CLI, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='unused-deps:semantic-resolution' "
                      "AND status='pending' AND instr(reason,'separate Java wildcard formal fixture')=0", (note,))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('wildcard formal artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-wildcard-formals-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = {}, {}

        def write(relative, source, root=None):
            path = (root or runner.root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source + '\n')
            return path

        def record(key, want, got):
            expected[key], actual[key] = want, got

        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('wildcard formal validation requires javac')

        def compile(label, sources):
            with (runner.directory / (label + '.javac.log')).open('wb') as log:
                return subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                    *map(str, sources)], stdout=log, stderr=log, timeout=30).returncode

        providers = [write('base/Base.java', 'package shared; public class Base { public int instance(){return 1;} }'),
                     write('lib/Child.java', 'package shared; public class Child extends Base {}')]
        write('base/build.gradle', 'plugins {}')
        write('lib/build.gradle', 'dependencies { api(project(":base")) }')
        positives, guards = [], []
        for label, (declaration, parameters, body, used) in CASES.items():
            body = body if 'return ' in body else 'return ' + body
            source = ('package fixture.' + label.replace('-', '_') + '; class Carrier<T> {} ' + declaration +
                      ' class Use { int run(' + parameters + '){' + body + ';} }')
            path = write(label + '/Use.java', source)
            write(label + '/build.gradle', 'dependencies { implementation(project(":base")); implementation(project(":lib")) }')
            (positives if used else guards).append(path)
        write('Foreign.kt', '// inventory only')
        write('marker.xml', '<marker/>')
        state = connect(runner.directory / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, runner.root)
            counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            record('inventory', {'.java': len(CASES)+2, '.gradle': len(CASES)+2, '.kt': 1, '.xml': 1}, counts)
            if expected['inventory'] != counts:
                raise ToolError('wildcard formal full inventory incomplete')
        finally:
            state.close()
        record('javac-positive', 0, compile('positive', providers + positives))
        for path in guards:
            record('javac-guard:' + path.parent.name, True, compile(path.parent.name, providers + [path]) != 0)
        if any(expected[k] != actual[k] for k in expected):
            raise ToolError('wildcard formal source/inventory validation failed')
        runner.command('rebuild', '--force', '--max-files', '0')

        def classification(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            if not isinstance(doc.get('items'), list) or doc.get('error'):
                raise ToolError('wildcard formals did not execute unused-deps')
            return sorted((r['name'], r['category'], r['usage']['direct'], r['examples']['direct']) for r in doc['items'])

        for label, (declaration, parameters, body, used) in CASES.items():
            child = int('shared.Child' in declaration + parameters + body)
            want = [('base', 'direct' if used else 'unused', int(used), ['Base'] if used else []),
                    ('lib', 'direct' if child else 'unused', child, ['Child'] if child else [])]
            for flags in ((), ('--strict',)):
                record(label + ':' + str(bool(flags)), want, classification(label, flags))
            _, text = runner.command('unused-deps', label, '--strict', '--verbose')
            record(label + ':text', True, ('Base' in text) == used)

        attached = runner.directory / 'attached'
        attached.mkdir()
        for relative, source in {
            'base/build.gradle': 'plugins {}',
            'base/Base.java': 'package shared; public class Base { public int instance(){return 2;} }',
            'lib/build.gradle': 'dependencies { api(project(":base")) }',
            'lib/Child.java': 'package shared; public class Child extends Base {}',
            'box/build.gradle': 'dependencies { api(project(":lib")) }',
            'box/Box.java': 'package api; import shared.Child; public class Box { public Child get(java.util.List<? super Child> c){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box b,java.util.List<Object> c){var row=b.get(c); return row.instance();} }',
        }.items():
            write(relative, source, attached)
        record('attached:javac', 0, compile('attached', sorted(attached.rglob('*.java'))))
        runner.command('subtree', 'add', 'attached', '../attached')
        runner.command('rebuild', '--force', '--max-files', '0')
        want = [('attached::base', 'direct', 1, ['Base']), ('attached::box', 'direct', 1, ['Box']),
                ('attached::lib', 'unused', 0, [])]
        for choices in product((False, True), repeat=3):
            flags = tuple(flag for enabled, flag in zip(choices, ('--no-transitive', '--no-xml', '--no-resources')) if enabled)
            option_want = want if choices[0] else [*want[:2], ('attached::lib', 'transitive', 0, [])]
            record('attached:options:' + ','.join(flags), option_want, classification('consumer', flags, attached))
        for args in (('update',), ('rebuild', '--force', '--max-files', '0')):
            runner.command(*args)
            record('attached:' + args[0], want, classification('consumer', ('--strict',), attached))
        _, text = runner.command('unused-deps', 'consumer', '--strict', '--verbose', cwd=attached)
        record('attached:text', True, 'Base' in text and 'attached::base' in text)
        write('box/Box.java', 'package api; public class Box { public Object get(java.util.List<?> c){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box b,java.util.List<Object> c){return b.get(c).hashCode();} }', attached)
        record('attached:changed-javac', 0, compile('attached-changed', sorted(attached.rglob('*.java'))))
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', 0, []), want[1], ('attached::lib', 'unused', 0, [])],
               classification('consumer', ('--strict',), attached))
        feature = next(iter(FEATURES))
        return {feature: expected}, {feature: actual}
