"""Nested Java array signature slots, with source-owned dependency expectations."""
from itertools import product
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-nested-array-slots'}
SUBJECT = 'disposable-java-nested-array-slots-v1'
REASON = ('independent source/javac/CLI: invariant nested Java array formal slots, '
          'fixed/class/method variables, ancestor projections, primitive/reference rank, '
          'witnesses, invocation phases and attached declaring ownership; not MCP equivalence')

# One compact signature family. Each positive must compile; every guard must
# fail compilation and must not credit the downstream Base declaring module.
CASES = {
    'fixed': ('class Box { shared.Child get(Carrier<shared.Child[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'fixed-primitive': ('class Box { shared.Child get(Carrier<int[]> c){return null;} }', 'Box b, Carrier<int[]> c', 'b.get(c).instance()', True),
    'primitive-kinds': ('class Box { shared.Child left(Carrier<boolean[]> c){return null;} shared.Child right(Carrier<double[][]> c){return null;} }', 'Box b, Carrier<boolean[]> c, Carrier<double[][]> d', 'b.left(c).instance()+b.right(d).instance()', True),
    'inferred': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'nested-rank': ('class Box { <T> T get(Carrier<Carrier<T[][]>> c){return null;} }', 'Box b, Carrier<Carrier<shared.Child[][]>> c', 'b.get(c).instance()', True),
    'class-slot': ('class Box<T> { T get(Carrier<T[]> c){return null;} }', 'Box<shared.Child> b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'array-class-slot': ('class Box<T> { shared.Child get(Carrier<T> c){return null;} }', 'Box<shared.Child[]> b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'inherited-formal': ('class Derived<U> extends Carrier<U[]> {} class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Derived<shared.Child> c', 'b.get(c).instance()', True),
    'inherited-method': ('class Parent<T> { T get(Carrier<T[]> c){return null;} } class Box<U> extends Parent<U> {}', 'Box<shared.Child> b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'witness': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.<shared.Child>get(c).instance()', True),
    'postfix': ('class Box { <T> T get(Carrier<T[]> c[]){return null;} }', 'Box b, Carrier<shared.Child[]>[] c', 'b.get(c).instance()', True),
    'spread': ('class Box { <T> T get(Carrier<T[]>... c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c,c).instance()', True),
    'fixed-spread': ('class Box { <T> T get(Carrier<T[]>... c){return null;} }', 'Box b, Carrier<shared.Child[]>[] c', 'b.get(c).instance()', True),
    'overload': ('class Box { <T> T get(Carrier<T[]> c){return null;} Object get(Object c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', True),
    'capture-site': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'class Carrier<U> {} var value=b.get(c); return ((java.util.function.IntSupplier)value::instance).getAsInt()', True),
    'rank-guard': ('class Box { <T> T get(Carrier<T[][]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', False),
    'primitive-guard': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<int[]> c', 'b.get(c).instance()', False),
    'invariant-guard': ('class Other {} class Box { shared.Child get(Carrier<Other[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', False),
    'class-guard': ('class Box<T> { T get(Carrier<T[]> c){return null;} }', 'Box<shared.Child> b, Carrier<String[]> c', 'b.get(c).instance()', False),
    'witness-guard': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.<String>get(c).instance()', False),
    'bound-guard': ('class Box { <T extends String> shared.Child get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', False),
    'private-guard': ('class Box { private shared.Child get(Carrier<shared.Child[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c).instance()', False),
    'arity-guard': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', 'b.get(c,c).instance()', False),
    'ambiguity-guard': ('class Box { shared.Child get(Carrier<shared.Child[]> c){return null;} shared.Child get(String c){return null;} }', 'Box b', 'b.get(null).instance()', False),
    'raw-guard': ('class Box { <T> T get(Carrier<T[]> c){return null;} }', 'Box b, Carrier c', 'b.get(c).instance()', False),
    'array-member-guard': ('class Box { Carrier<shared.Child[]>[] get(){return null;} }', 'Box b', 'b.get().value().instance()', False),
    'array-variable-method-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child[]> b', 'b.get().instance()', False),
    'array-variable-field-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child[]> b', 'b.get().OPEN', False),
    'array-variable-reference-guard': ('class Box { <T> T get(Carrier<T> c){return null;} }', 'Box b, Carrier<shared.Child[]> c', '((java.util.function.IntSupplier)b.get(c)::instance).getAsInt()', False),
}


def plan_slots(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': SUBJECT}), feature, SUBJECT))
        note = ('; executed nested Java array formal slot checklist covers invariant reference/primitive '
                'rank, fixed/class/method variables, witnesses, ancestor projections, capture sites, '
                'fixed/postfix/spread phases and attached JSON/text/options/update/rebuild ownership; '
                'wildcard captures, target/common/intersection inference, generic override erasure, '
                'raw unchecked formals, result indexing and classpath-order obligations remain pending; '
                'independent source/javac/CLI, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='unused-deps:semantic-resolution' "
                      "AND status='pending' AND instr(reason,'executed nested Java array formal slot checklist')=0", (note,))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('nested array artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-array-slots-', dir=base) as temporary:
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

        providers = [write('base/Base.java', 'package shared; public class Base { public int OPEN=1; public int instance(){return 1;} }'),
                     write('lib/Child.java', 'package shared; public class Child extends Base {}')]
        write('base/build.gradle', 'plugins {}')
        write('lib/build.gradle', 'dependencies { api(project(":base")) }')
        positives, guards = [], []
        for label, (declaration, parameters, body, used) in CASES.items():
            body = body if 'return ' in body else 'return ' + body
            source = ('package fixture.' + label.replace('-', '_') + '; '
                      'class Carrier<U> { U value(){return null;} } ' + declaration +
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
                raise ToolError('nested array full inventory incomplete')
        finally:
            state.close()
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('nested array validation requires javac')

        def compile(label, sources):
            with (runner.directory / (label + '.javac.log')).open('wb') as log:
                return subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                    *map(str, sources)], stdout=log, stderr=log, timeout=30).returncode

        record('javac-positive', 0, compile('positive', providers + positives))
        for path in guards:
            record('javac-guard:' + path.parent.name, True, compile(path.parent.name, providers + [path]) != 0)
        # Compiler evidence must succeed before claiming any CLI expectations.
        if any(expected[k] != actual[k] for k in expected):
            raise ToolError('nested array source/inventory validation failed; see private logs')
        runner.command('rebuild', '--force', '--max-files', '0')

        def classification(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            return sorted((row['name'], row['category'], row['usage']['direct'], row['examples']['direct']) for row in doc['items'])

        for label, (_, parameters, body, used) in CASES.items():
            child = int('shared.Child' in parameters + CASES[label][0])
            want = [('base', 'direct' if used else 'unused', int(used), ['Base'] if used else []),
                    ('lib', 'direct' if child else 'unused', child, ['Child'] if child else [])]
            for flags in ((), ('--strict',)):
                record(label + ':' + str(bool(flags)), want, classification(label, flags))
            _, text = runner.command('unused-deps', label, '--strict', '--verbose')
            record(label + ':text', True, ('Base' in text) == used)

        # Provider imports and leaf slots must bind in the attached classpath,
        # with primary decoys present, including after an incremental refresh.
        attached = runner.directory / 'attached'
        attached.mkdir()
        for relative, source in {
            'base/build.gradle': 'plugins {}',
            'base/Base.java': 'package shared; public class Base { public int OPEN=2; public int instance(){return 2;} }',
            'lib/build.gradle': 'dependencies { api(project(":base")) }',
            'lib/Child.java': 'package shared; public class Child extends Base {}',
            'box/build.gradle': 'dependencies { api(project(":lib")) }',
            'box/Carrier.java': 'package api; public class Carrier<T> {}',
            'box/Box.java': 'package api; public class Box { public <T> T get(Carrier<T[]> c){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box b, api.Carrier<shared.Child[]> c){return b.get(c).instance();} }',
        }.items():
            write(relative, source, attached)
        record('attached:javac', 0, compile('attached', sorted(attached.rglob('*.java'))))
        runner.command('subtree', 'add', 'attached', '../attached')
        runner.command('rebuild', '--force', '--max-files', '0')
        want = [('attached::base', 'direct', 1, ['Base']), ('attached::box', 'direct', 2, ['Box', 'Carrier']),
                ('attached::lib', 'direct', 1, ['Child'])]
        for choices in product((False, True), repeat=3):
            flags = tuple(flag for enabled, flag in zip(choices, ('--no-transitive', '--no-xml', '--no-resources')) if enabled)
            record('attached:options:' + ','.join(flags), want, classification('consumer', flags, attached))
        for args in (('update',), ('rebuild', '--force', '--max-files', '0')):
            runner.command(*args)
            record('attached:' + args[0], want, classification('consumer', ('--strict',), attached))
        write('box/Box.java', 'package api; public class Box { public <T> Object get(Carrier<T[]> c){return null;} }', attached)
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', 0, []), *want[1:]], classification('consumer', ('--strict',), attached))
        feature = next(iter(FEATURES))
        return {feature: expected}, {feature: actual}
