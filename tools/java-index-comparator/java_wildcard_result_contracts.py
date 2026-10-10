"""Executed Java wildcard read-result ownership; independent source/javac/CLI, not MCP."""
from itertools import product
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-wildcard-results'}
SUBJECT = 'disposable-java-wildcard-results-v1'
REASON = ('independent source/javac/CLI: upper-bounded wildcard read results through '
          'source fields/methods/records/inheritance, supported JDK projections, '
          'arrays and var captures; write/access/arity/rank/invariant guards, attached '
          'imports and JSON/text/options/update/rebuild; not MCP equivalence')

# Declaration, parameter list, expression/body, downstream declaring owner used.
# Base is never named in consumer source. Crediting Base requires real member
# resolution, not DB self-consistency or a JSON envelope check.
CASES = {
    'method': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child> b', 'b.get().instance()', True),
    'field': ('class Box<T> { T value; }', 'Box<? extends shared.Child> b', 'b.value.instance()', True),
    'record': ('record Box<T>(T value) {}', 'Box<? extends shared.Child> b', 'b.value().instance()', True),
    'inherited': ('class Parent<T> { T get(){return null;} } class Box<A,B> extends Parent<B> {}', 'Box<String,? extends shared.Child> b', 'b.get().instance()', True),
    'nested-source': ('class Wrap<T> { T get(){return null;} } class Box<T> { T get(){return null;} }', 'Box<? extends Wrap<? extends shared.Child>> b', 'b.get().get().instance()', True),
    'list': ('', 'java.util.List<? extends shared.Child> b', 'b.get(0).instance()', True),
    'optional': ('', 'java.util.Optional<? extends shared.Child> b', 'b.get().instance()', True),
    'supplier': ('', 'java.util.function.Supplier<? extends shared.Child> b', 'b.get().instance()', True),
    'map-list': ('record Box(java.util.Map<String,java.util.List<? extends shared.Child>> value) {}', 'Box b', 'b.value().get("x").get(0).instance()', True),
    'result': ('class Box { java.util.List<? extends shared.Child> get(){return null;} }', 'Box b', 'b.get().get(0).instance()', True),
    'method-variable-result': ('class Box { <T> java.util.List<? extends T> get(T c){return null;} }', 'Box b, shared.Child c', 'b.get(c).get(0).instance()', True),
    'class-variable-result': ('class Box<T> { java.util.List<? extends T> get(){return null;} }', 'Box<shared.Child> b', 'b.get().get(0).instance()', True),
    'captured-array-guard': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child[]> b', 'b.get()[0].instance()', False),
    'generic-array': ('class Box<T> { T[] get(){return null;} }', 'Box<? extends shared.Child> b', 'b.get()[0].instance()', True),
    'capture-reference': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)c::instance).getAsInt()', True),
    'capture-lambda': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)()->c.instance()).getAsInt()', True),
    'upper-bound-control': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box<?> b', 'b.get().instance()', True),
    'declared-bound': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box<? extends Object> b', 'b.get().instance()', True),
    'declared-record-bound': ('record Box<T extends shared.Child>(T value) {}', 'Box<? extends Object> b', 'b.value().instance()', True),
    'lower-bound-control': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box<? super shared.Child> b', 'b.get().instance()', True),
    'write-guard': ('class Box<T> { T put(T c){return null;} }', 'Box<? extends shared.Child> b, shared.Child c', 'b.put(c).instance()', False),
    'array-write-guard': ('class Box<T> { T put(T[] c){return null;} }', 'Box<? extends shared.Child> b, shared.Child[] c', 'b.put(c).instance()', False),
    'invariant-witness-guard': ('class Carrier<T> {} class Box { <T> T get(Carrier<T> c){return null;} }', 'Box b, Carrier<? extends shared.Child> c', 'b.<shared.Child>get(c).instance()', False),
    'lower-bound-guard': ('class Box<T> { T get(){return null;} }', 'Box<? super shared.Child> b', 'b.get().instance()', False),
    'unbounded-guard': ('class Box<T> { T get(){return null;} }', 'Box<?> b', 'b.get().instance()', False),
    'private-guard': ('class Box<T> { private T get(){return null;} }', 'Box<? extends shared.Child> b', 'b.get().instance()', False),
    'non-instance-guard': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child> b', 'Box.get().instance()', False),
    'wrong-bound-guard': ('', 'java.util.List<? extends String> b', 'b.get(0).instance()', False),
    'remaining-rank-guard': ('class Box<T> { T get(){return null;} }', 'Box<? extends shared.Child[][]> b', 'b.get()[0].instance()', False),
}


def plan_results(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': SUBJECT}), feature, SUBJECT))
        note = ('; separate executed Java wildcard read-result checklist covers upper bounds, '
                'source/JDK/record/inherited/array projections and initializer-site captures, '
                'write/invariant/access/rank guards and attached import ownership with '
                'JSON/text/options/update/rebuild; wildcard formal inference, target/common/intersection '
                'inference, override erasure, raw unchecked formals, external/platform lookup and '
                'classpath-order ownership remain pending; independent source/javac/CLI, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='unused-deps:semantic-resolution' "
                      "AND status='pending' AND instr(reason,'Java wildcard read-result checklist')=0", (note,))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('wildcard result artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-wildcard-results-', dir=base) as temporary:
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

        def compile(label, sources):
            with (runner.directory / (label + '.javac.log')).open('wb') as log:
                return subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                    *map(str, sources)], stdout=log, stderr=log, timeout=30).returncode

        providers = [write('base/Base.java', 'package shared; public class Base { public int OPEN=1; public int instance(){return 1;} }'),
                     write('lib/Child.java', 'package shared; public class Child extends Base {}')]
        write('base/build.gradle', 'plugins {}')
        write('lib/build.gradle', 'dependencies { api(project(":base")) }')
        positives, guards = [], []
        for label, (declaration, parameters, body, used) in CASES.items():
            body = body if 'return ' in body else 'return ' + body
            source = ('package fixture.' + label.replace('-', '_') + '; ' + declaration +
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
                raise ToolError('wildcard result full inventory incomplete')
        finally:
            state.close()
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('wildcard result validation requires javac')
        record('javac-positive', 0, compile('positive', providers + positives))
        for path in guards:
            record('javac-guard:' + path.parent.name, True, compile(path.parent.name, providers + [path]) != 0)
        if any(expected[k] != actual[k] for k in expected):
            raise ToolError('wildcard result source/inventory validation failed; see private logs')
        runner.command('rebuild', '--force', '--max-files', '0')

        def classification(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            if not isinstance(doc.get('items'), list) or doc.get('error'):
                raise ToolError('wildcard results did not execute unused-deps')
            return sorted((row['name'], row['category'], row['usage']['direct'], row['examples']['direct']) for row in doc['items'])

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
            'box/Box.java': 'package api; import shared.Child; public class Box { public java.util.List<? extends Child> get(){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box b){var row=b.get().get(0); return row.instance();} }',
        }.items():
            write(relative, source, attached)
        record('attached:javac', 0, compile('attached', sorted(attached.rglob('*.java'))))
        runner.command('subtree', 'add', 'attached', '../attached')
        runner.command('rebuild', '--force', '--max-files', '0')
        # Direct identities are declarations actually named by the consumer
        # or members actually used. An intermediate result type itself is not
        # an extra declaring-member credit.
        want = [('attached::base', 'direct', 1, ['Base']), ('attached::box', 'direct', 1, ['Box']),
                ('attached::lib', 'unused', 0, [])]
        for choices in product((False, True), repeat=3):
            flags = tuple(flag for enabled, flag in zip(choices, ('--no-transitive', '--no-xml', '--no-resources')) if enabled)
            # lib re-exports Base through api; the default check credits that
            # used API chain even though Child itself has no direct use.
            option_want = want if choices[0] else [*want[:2], ('attached::lib', 'transitive', 0, [])]
            record('attached:options:' + ','.join(flags), option_want, classification('consumer', flags, attached))
        for args in (('update',), ('rebuild', '--force', '--max-files', '0')):
            runner.command(*args)
            record('attached:' + args[0], want, classification('consumer', ('--strict',), attached))
        _, text = runner.command('unused-deps', 'consumer', '--strict', '--verbose', cwd=attached)
        record('attached:text', True, 'Base' in text and 'attached::base' in text)
        write('box/Box.java', 'package api; public class Box { public java.util.List<? super shared.Child> get(){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box b){return b.get().get(0).hashCode();} }', attached)
        record('attached:changed-javac', 0, compile('attached-changed', sorted(attached.rglob('*.java'))))
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', 0, []), want[1], ('attached::lib', 'unused', 0, [])],
               classification('consumer', ('--strict',), attached))
        write('box/Box.java', 'package api; import shared.Child; public class Box<T extends Child> { public T get(){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box<? extends Object> b){var c=b.get(); return c.instance();} }', attached)
        record('attached:declared-bound:javac', 0, compile('attached-bound', sorted(attached.rglob('*.java'))))
        runner.command('update')
        record('attached:declared-bound', want, classification('consumer', ('--strict',), attached))
        feature = next(iter(FEATURES))
        return {feature: expected}, {feature: actual}
