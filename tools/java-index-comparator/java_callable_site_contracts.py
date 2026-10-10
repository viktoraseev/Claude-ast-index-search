"""Authored Java callable sites, not native DB truth or MCP equivalence."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
import mobile_contracts

FEATURE = 'call-tree:java-callable-sites'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-callable-sites-v1'
REASON = ('independent source/javac/CLI: exact Java callable byte sites for '
          'same-line overloaded bare/qualified/reference/variable-arity/constructor '
          'and recursive branches, reverse declaration order, zero/one/full pages '
          'and depth zero through three; not MCP equivalence; broader receiver '
          'dispatch and semantic attached-root acceptance remain separate')

SOURCE = '''package fixture;
class Probe {
 static class A {}
 static class B {}
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static int route(A a) { return alphaSeed(); } static int route(B b) { return betaSeed(); }
 static int alpha(A a) { return route(a); }
 static int beta(B b) { return route(b); }
 static int overloaded(A a) { return route(a); } static int overloaded(B b) { return route(b); }
 static int topAlpha(A a) { return overloaded(a); }
 static int topBeta(B b) { return overloaded(b); }
 static int qualified(A a) { return Probe.route(a); } static int qualified(B b) { return Probe.route(b); }
 static int qualifiedAlpha(A a) { return qualified(a); }
 static int qualifiedBeta(B b) { return qualified(b); }
 static int varied(A a) { return route(a); } static int varied(B b, int n) { return route(b); }
 static int variedAlpha(A a) { return varied(a); }
 static int variedBeta(B b) { return varied(b, 1); }
 static int reference(A a) { java.util.function.ToIntFunction<A> f = Probe::route; return f.applyAsInt(a); } static int reference(B b) { java.util.function.ToIntFunction<B> f = Probe::route; return f.applyAsInt(b); }
 static int referenceAlpha(A a) { return reference(a); }
 static int referenceBeta(B b) { return reference(b); }
 static class Construct {
  Construct(A a) { alphaSeed(); } Construct(B b) { betaSeed(); }
 }
 static Construct create(A a) { return new Construct(a); } static Construct create(B b) { return new Construct(b); }
 static Construct createAlpha(A a) { return create(a); }
 static Construct createBeta(B b) { return create(b); }
 static int cycle(A a) { alphaSeed(); return cycle(a); } static int cycle(B b, int n) { betaSeed(); return cycle(b, n); }
 static int cycleAlpha(A a) { return cycle(a); }
 static int cycleBeta(B b) { return cycle(b, 1); }
}
'''
# Every tuple is authored declaration identity plus exact caller branch.
BRANCHES = {
    'alphaSeed': [(7, 'route', [(8, 'alpha'), (10, 'overloaded'), (13, 'qualified'),
                              (16, 'varied'), (19, 'reference')]),
                  (23, 'Construct', [(25, 'create')]), (28, 'cycle', [(28, 'cycle'), (29, 'cycleAlpha')])],
    'betaSeed': [(7, 'route', [(9, 'beta'), (10, 'overloaded'), (13, 'qualified'),
                             (16, 'varied'), (19, 'reference')]),
                 (23, 'Construct', [(25, 'create')]), (28, 'cycle', [(28, 'cycle'), (30, 'cycleBeta')])],
}
TOP = {'alphaSeed': {'overloaded': (11, 'topAlpha'), 'qualified': (14, 'qualifiedAlpha'),
                    'varied': (17, 'variedAlpha'), 'reference': (20, 'referenceAlpha'), 'create': (26, 'createAlpha')},
       'betaSeed': {'overloaded': (12, 'topBeta'), 'qualified': (15, 'qualifiedBeta'),
                   'varied': (18, 'variedBeta'), 'reference': (21, 'referenceBeta'), 'create': (27, 'createBeta')}}


def plan_sites(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def reverse_declarations(source):
    lines = []
    for line in source.splitlines(True):
        # Swap whole authored declarations, preserving all expected coordinates.
        if '} static int ' in line:
            first, second = line.rstrip('\n').split('} static int ', 1)
            line = ' static int ' + second + first + '}\n'
        elif '} Construct(' in line:
            first, second = line.rstrip('\n').split('} Construct(', 1)
            line = '  Construct(' + second + first + '}\n'
        elif '} static Construct ' in line:
            first, second = line.rstrip('\n').split('} static Construct ', 1)
            line = ' static Construct ' + second + first + '}\n'
        lines.append(line)
    return ''.join(lines)


def wanted(seed, depth, limit):
    items = []
    if not limit or not depth:
        return items
    for line, name, children in BRANCHES[seed][:limit]:
        items.append((1, 'Probe.java', line, name, 'shown'))
        if depth < 2:
            continue
        for child_line, child in children[:limit]:
            recursive = child_line == line and child == name
            items.append((2, 'Probe.java', child_line, child, 'recursive' if recursive else 'shown'))
            if depth >= 3 and child in TOP[seed] and not recursive:
                top_line, top = TOP[seed][child]
                items.append((3, 'Probe.java', top_line, top, 'shown'))
    return items


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('callable site fixtures must stay inside repository .artifacts')
    directory = Path(tempfile.mkdtemp(prefix='java-callable-sites-', dir=base))
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    expected, actual = {}, {}
    for layout, source in (('forward', SOURCE), ('reverse', reverse_declarations(SOURCE))):
        (runner.root / 'Probe.java').write_text(source)
        state = connect(directory / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, runner.root)
            counts = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            if counts != {'.java': 1, '.kt': 1, '.xml': 1}:
                raise ToolError('callable fixture inventory incomplete')
            expected[layout + ':inventory'] = actual[layout + ':inventory'] = counts
        finally:
            state.close()
        with (directory / (layout + '-javac.log')).open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'),
                                     str(runner.root / 'Probe.java')], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored Java callable fixture does not compile; see private log')
        runner.command('rebuild', '--force', '--max-files', 0)
        runner.json('graph', 'build')
        for seed in BRANCHES:
            for depth in (0, 1, 2, 3):
                for limit in (0, 1, 100):
                    key = f'{layout}:{seed}:{depth}:{limit}'
                    doc = runner.json('call-tree', seed, '--depth', depth, '--limit', limit, '--in-file', '.java')
                    rows = [tuple(row[field] for field in ('depth', 'path', 'line', 'name', 'status'))
                            for row in doc['items']]
                    expected[key] = {'function': seed, 'max_depth': depth, 'limit_per_level': limit,
                                     'count': len(wanted(seed, depth, limit)), 'items': wanted(seed, depth, limit)}
                    actual[key] = {field: doc.get(field) for field in ('function', 'max_depth', 'limit_per_level', 'count')}
                    actual[key]['items'] = rows
    return expected, actual
