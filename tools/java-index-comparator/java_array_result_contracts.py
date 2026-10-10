"""Executed Java array-result ownership; independent source/javac/CLI, not MCP."""
from itertools import product
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-array-results'}
SUBJECT = 'disposable-java-array-results-v1'
REASON = ('independent source/javac/CLI: Java array-result indexing, declarations, '
          'fields/records/class and method substitutions, captures, rank/index/access '
          'guards and attached declaring ownership with refresh; not MCP equivalence')

# Declaration, parameter list, expression/body, downstream declaring owner used.
# Base is never named in consumer source. Crediting Base requires real member
# resolution, not DB self-consistency or a JSON envelope check.
CASES = {
    'nominal-result': ('class Box { shared.Child[] get(){return null;} }', 'Box b', 'b.get()[0].instance()', True),
    'postfix-result': ('class Box { shared.Child get()[]{return null;} }', 'Box b', 'b.get()[0].instance()', True),
    'rank-result': ('class Box { shared.Child[][] get(){return null;} }', 'Box b', 'b.get()[0][0].instance()', True),
    'field-result': ('class Box { shared.Child[] values; }', 'Box b', 'b.values[0].OPEN', True),
    'record-result': ('record Box(shared.Child[] values) {}', 'Box b', 'b.values()[0].instance()', True),
    'generic-result': ('class Box<T> { T[] get(){return null;} }', 'Box<shared.Child> b', 'b.get()[0].instance()', True),
    'array-class-slot': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child[]> b', 'b.get()[0].instance()', True),
    'generic-field': ('class Box<T> { T[] values; }', 'Box<shared.Child> b', 'b.values[0].OPEN', True),
    'generic-record': ('record Box<T>(T[] values) {}', 'Box<shared.Child> b', 'b.values()[0].instance()', True),
    'inherited-reordered': ('class Parent<A,B> { B[] get(){return null;} } class Box<U,V> extends Parent<V,U> {}', 'Box<shared.Child,String> b', 'b.get()[0].instance()', True),
    'method-result': ('class Box { <T> T[] get(T c){return null;} }', 'Box b, shared.Child c', 'b.get(c)[0].instance()', True),
    'method-array-slot': ('class Box { <T> T get(T c){return c;} }', 'Box b, shared.Child[] c', 'b.get(c)[0].instance()', True),
    'method-witness': ('class Box { <T> T[] get(){return null;} }', 'Box b', 'b.<shared.Child>get()[0].instance()', True),
    'method-spread': ('class Box { <T> T[] get(T... c){return c;} }', 'Box b, shared.Child c', 'b.get(c,c)[0].instance()', True),
    'nested-container': ('class Box<T> { java.util.List<T[]> get(){return null;} }', 'Box<shared.Child> b', 'b.get().get(0)[0].instance()', True),
    'array-container': ('class Carrier<T> { T value(){return null;} } class Box<T> { Carrier<T>[] get(){return null;} }', 'Box<shared.Child> b', 'b.get()[0].value().instance()', True),
    'capture': ('class Box { shared.Child[][] get(){return null;} }', 'Box b', 'var row=b.get()[0]; return ((java.util.function.IntSupplier)row[0]::instance).getAsInt()', True),
    'initializer-site': ('class Box { shared.Child[] get(){return null;} }', 'Box b', 'var values=b.get(); class Box {} return values[0].instance()', True),
    'parameter': ('', 'shared.Child[] c', 'c[0].instance()', True),
    'postfix-parameter': ('', 'shared.Child c[]', 'c[0].instance()', True),
    'spread-parameter': ('', 'shared.Child... c', 'c[0].instance()', True),
    'local': ('', 'shared.Child[] c', 'shared.Child[] values=c; return values[0].instance()', True),
    'this-field': ('class UseBase { shared.Child[] values; }', '', 'this.values[0].instance()', True),
    'cast': ('', 'Object c', '((shared.Child[])c)[0].instance()', True),
    'new-array': ('', '', '(new shared.Child[1])[0].instance()', True),
    'narrow-index': ('class Box { shared.Child[] get(){return null;} }', 'Box b, byte i', 'b.get()[i].instance()', True),
    'boxed-index': ('class Box { shared.Child[] get(){return null;} }', 'Box b, Integer i', 'b.get()[i].instance()', True),
    'index-expression': ('class Box { shared.Child[] get(){return null;} }', 'Box b, char i', 'b.get()[i+1].instance()', True),
    'method-index': ('class Box { shared.Child[] get(){return null;} int index(){return 0;} }', 'Box b', 'b.get()[b.index()].instance()', True),
    'field-index': ('class Box { shared.Child[] get(){return null;} short index; }', 'Box b', 'b.get()[b.index].instance()', True),
    'boxed-method-index': ('class Box { shared.Child[] get(){return null;} Integer index(){return 0;} }', 'Box b', 'b.get()[b.index()].instance()', True),
    'postfix-field': ('class Box { shared.Child values[]; }', 'Box b', 'b.values[0].instance()', True),
    'spread-record': ('record Box(shared.Child... values) {}', 'Box b', 'b.values()[0].instance()', True),
    'bounded-array-parameter': ('', 'T[] c', 'c[0].instance()', True),
    'remaining-rank-guard': ('class Box { shared.Child[][] get(){return null;} }', 'Box b', 'b.get()[0].instance()', False),
    'excess-rank-guard': ('class Box { shared.Child[] get(){return null;} }', 'Box b', 'b.get()[0][0].instance()', False),
    'nonarray-guard': ('class Box { shared.Child get(){return null;} }', 'Box b', 'b.get()[0].instance()', False),
    'long-index-guard': ('class Box { shared.Child[] get(){return null;} }', 'Box b, long i', 'b.get()[i].instance()', False),
    'boxed-long-index-guard': ('class Box { shared.Child[] get(){return null;} }', 'Box b, Long i', 'b.get()[i].instance()', False),
    'boolean-index-guard': ('class Box { shared.Child[] get(){return null;} }', 'Box b', 'b.get()[true].instance()', False),
    'primitive-leaf-guard': ('class Box { int[] get(){return null;} }', 'Box b', 'b.get()[0].instance()', False),
    'private-result-guard': ('class Box { private shared.Child[] get(){return null;} }', 'Box b', 'b.get()[0].instance()', False),
    'array-reference-guard': ('class Box { shared.Child[][] get(){return null;} }', 'Box b', '((java.util.function.IntSupplier)b.get()[0]::instance).getAsInt()', False),
    'static-context-guard': ('class UseBase { shared.Child[] values; }', '', 'this.values[0].instance()', False),
    'method-long-index-guard': ('class Box { shared.Child[] get(){return null;} long index(){return 0;} }', 'Box b', 'b.get()[b.index()].instance()', False),
    'method-private-index-guard': ('class Box { shared.Child[] get(){return null;} private int index(){return 0;} }', 'Box b', 'b.get()[b.index()].instance()', False),
    'wrapper-shadow-index-guard': ('class Integer {} class Box { shared.Child[] get(){return null;} }', 'Box b, Integer i', 'b.get()[i].instance()', False),
}


def plan_results(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': SUBJECT}), feature, SUBJECT))
        note = ('; separate executed Java array-result indexing checklist covers rank, nominal/generic '
                'fields/methods/records, inheritance/method inference, declaration/initializer sites, '
                'captures/references, integral index and access guards with attached ownership and '
                'JSON/text/options/update/rebuild; wildcard/target/common/intersection inference, '
                'generic override erasure, raw unchecked formals, external/platform lookup and '
                'classpath-order ownership remain pending; independent source/javac/CLI, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='unused-deps:semantic-resolution' "
                      "AND status='pending' AND instr(reason,'Java array-result indexing checklist')=0", (note,))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('array result artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-array-results-', dir=base) as temporary:
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
            inheritance = ' extends UseBase' if label in ('this-field', 'static-context-guard') else ''
            static = 'static ' if label == 'static-context-guard' else ''
            generic = '<T extends shared.Child>' if label == 'bounded-array-parameter' else ''
            source = ('package fixture.' + label.replace('-', '_') + '; ' + declaration +
                      ' class Use' + generic + inheritance + ' { ' + static + 'int run(' + parameters + '){' + body + ';} }')
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
                raise ToolError('array result full inventory incomplete')
        finally:
            state.close()
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('array result validation requires javac')
        record('javac-positive', 0, compile('positive', providers + positives))
        for path in guards:
            record('javac-guard:' + path.parent.name, True, compile(path.parent.name, providers + [path]) != 0)
        if any(expected[k] != actual[k] for k in expected):
            raise ToolError('array result source/inventory validation failed; see private logs')
        runner.command('rebuild', '--force', '--max-files', '0')

        def classification(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            if not isinstance(doc.get('items'), list) or doc.get('error'):
                raise ToolError('array results did not execute unused-deps')
            return sorted((row['name'], row['category'], row['usage']['direct'], row['examples']['direct']) for row in doc['items'])

        for label, (declaration, parameters, body, used) in CASES.items():
            child = int('shared.Child' in declaration + parameters + body)
            if label == 'bounded-array-parameter':
                child = 1
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
            'box/Box.java': 'package api; import shared.Child; public class Box { public Child[] get(){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box b){var row=b.get(); return row[0].instance();} }',
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
        write('box/Box.java', 'package api; public class Box { public Object[] get(){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box b){return b.get()[0].hashCode();} }', attached)
        record('attached:changed-javac', 0, compile('attached-changed', sorted(attached.rglob('*.java'))))
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', 0, []), want[1], ('attached::lib', 'unused', 0, [])],
               classification('consumer', ('--strict',), attached))
        feature = next(iter(FEATURES))
        return {feature: expected}, {feature: actual}
