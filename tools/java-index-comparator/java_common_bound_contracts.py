"""Java dependency ownership after common source-bound inference; not MCP truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURE = 'unused-deps:java-common-bound-inference'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-common-bound-inference-v1'
REASON = ('independent source/javac/CLI: common source superclass inference, bounded '
          'and unbounded method results, arrays, nested slots, var captures, argument '
          'order and incompatible/invariant guards; not MCP equivalence')
CASES = {
    'bounded': ('<T extends shared.Child> T pick(T a,T b){return a;}',
                'b.pick(a,c).instance()', True),
    'unbounded': ('<T> T pick(T a,T b){return a;}',
                  'b.pick(a,c).instance()', True),
    'reverse': ('<T extends shared.Child> T pick(T a,T b){return a;}',
                'b.pick(c,a).instance()', True),
    'array': ('<T extends shared.Child> T[] pick(T a,T b){return null;}',
              'b.pick(a,c)[0].instance()', True),
    'slot': ('<T extends shared.Child> Carrier<T> pick(T a,T b){return null;}',
             'b.pick(a,c).value().instance()', True),
    'capture': ('<T extends shared.Child> T pick(T a,T b){return a;}',
                'var value=b.pick(a,c); return value.instance()', True),
    'spread': ('<T extends shared.Child> T pick(T... values){return null;}',
               'b.pick(a,c,a).instance()', True),
    'deep': ('<T extends shared.Child> T pick(T a,T b){return a;}',
             'b.pick(new Deep(),c).instance()', True),
    'bound-guard': ('<T extends shared.Child> T pick(T a,T b){return a;}',
                    'b.pick(a,"wrong").instance()', False),
    'invariant-guard': ('<T> T pick(Carrier<T> a,Carrier<T> b){return null;}',
                        'b.pick(new Carrier<Left>(),new Carrier<Right>()).instance()', False),
}


def plan_contracts(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base, *, cases=None, refresh=True):
    cases = CASES if cases is None else cases
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('common-bound fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='common-bound-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)

    def write(relative, source):
        path = runner.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source + '\n')
        return path

    providers = [write('base/Base.java', 'package shared; public class Base { public int instance(){return 1;} }'),
                 write('lib/Child.java', 'package shared; public class Child extends Base {}')]
    write('base/build.gradle', 'plugins {}')
    write('lib/build.gradle', 'dependencies { api(project(":base")) }')
    for label, (method, body, used) in cases.items():
        source = ('package sample.' + label.replace('-', '_') + '; '
                  'class Left extends shared.Child {} class Right extends shared.Child {} '
                  'class Deep extends Left {} class Carrier<T> { T value(){return null;} } '
                  'class Box {' + method + '} class Noise { int instance(){return 0;} } '
                  'class Use { int run(Box b,Left a,Right c){' +
                  (body if 'return ' in body else 'return ' + body) + ';} }')
        path = write(label + '/Use.java', source)
        write(label + '/build.gradle', 'dependencies { implementation(project(":base")); implementation(project(":lib")) }')
        with (runner.directory / (label + '.javac.log')).open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                     *map(str, providers), str(path)], stdout=log, stderr=log, timeout=30)
        if (result.returncode == 0) != used:
            raise ToolError('common-bound Java validity differs from authored expectation; see private logs')
    runner.command('rebuild', '--force', '--max-files', '0')
    expected, actual = {}, {}
    for label, (_, _, used) in cases.items():
        want = [('base', 'direct' if used else 'unused', ['Base'] if used else []),
                ('lib', 'direct', ['Child'])]
        for flags in ((), ('--strict',), ('--no-transitive', '--no-xml', '--no-resources')):
            key = label + ':' + ','.join(flags)
            doc = runner.json('unused-deps', label, '--verbose', *flags)
            expected[key] = want
            actual[key] = sorted((row['name'], row['category'], row['examples']['direct']) for row in doc['items'])
        _, output = runner.command('unused-deps', label, '--verbose', '--strict')
        expected[label + ':text'] = used
        actual[label + ':text'] = 'Base' in output
    # Refresh must retain the same semantic classification.
    write('lib/Child.java', 'package shared; public class Child extends Base { int unrelated; }')
    for command in ('update', 'rebuild') if refresh else ():
        runner.command(command)
        for label in ('bounded', 'invariant-guard'):
            doc = runner.json('unused-deps', label, '--verbose', '--strict')
            expected[label + ':' + command] = expected[label + ':--strict']
            actual[label + ':' + command] = sorted((row['name'], row['category'], row['examples']['direct']) for row in doc['items'])
    return expected, actual
