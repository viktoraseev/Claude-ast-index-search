"""Java caller/context contracts on authored source; no MCP oracle is claimed."""
import json
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'call-tree', 'explore'}
REASON = ('independent source/state: disposable Java invocations, caller ownership, '
          'depth/filter/limits and exploration source/outline/scoped neighbours; not MCP equivalence')
PENDING = {
    'call-tree:semantic-resolution': 'Name-based caller expansion does not establish Java receiver/overload dispatch, colliding same-name/same-line owners or attached-root scope; separate source/MCP contract required',
    'explore:semantic-resolution': 'Java local/field/generic receiver and overload type dispatch, same-name overloads sharing a line, type-reference binding and attached-root neighbour resolution remain unresolved; separate explicit parameter/arity and fresh-graph empty caller contracts do not establish compiler-wide dispatch or MCP equivalence',
}


def plan_context(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-context'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for feature, reason in PENDING.items():
            state.execute("INSERT OR REPLACE INTO coverage VALUES (?,'pending',?)", (feature, reason))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('context artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='context-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

        def write(path, source):
            path = runner.root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)

        def record(feature, label, want, got):
            expected[feature][label], actual[feature][label] = want, got

        write('a/Probe.java', '''package fixture;
class Probe {
    int leaf() { return 1; }
    int build() { return leaf(); }
    int build(int n) { return leaf() + n; }
    int pulse() {
        return 7;
    }
    int recursive() { return recursive(); }
    int methodReference() { java.util.function.IntSupplier s = this::leaf; return s.getAsInt(); }
    int noise() {
        // leaf() is not a call
        String text = "leaf()";
        return 0;
    }
}
''')
        write('a/Entry.java', '''package fixture;
class Entry {
    int start(Probe p) { return p.build(); }
    int invoke(Probe p) { return p.pulse(); }
}
''')
        write('a/ProbeTest.java', 'package fixture; class ProbeTest {}\n')
        write('b/Foreign.java', '''package fixture;
class Foreign extends Probe {
    int outside() { return pulse(); }
}
''')
        write('a/LocalChild.java', 'package fixture; class LocalChild extends Probe {}\n')
        # Enough foreign references to exhaust an unscoped RWR lookup before
        # the valid caller. The contents stay tiny and memory stays bounded.
        for i in range(45):
            write(f'outside/F{i:02d}.java', f'class F{i:02d} {{ int decoy(Probe p) {{ return p.pulse(); }} }}\n')

        def tree(label, arguments, wanted):
            _, output = runner.command('call-tree', *arguments)
            record('call-tree', label, wanted, output)

        header = "Call tree for 'leaf':\n  leaf\n"
        direct = ('    ← build (a/Probe.java:4)\n'
                  '    ← build (a/Probe.java:5)\n')
        tree('unindexed', ['leaf', '--depth', '1'], header + direct)
        runner.command('rebuild', '--force')
        tree('indexed', ['leaf', '--depth', '1'], header + direct)
        tree('depth-two', ['leaf', '--depth', '2'], header +
             '    ← build (a/Probe.java:4)\n      ← start (a/Entry.java:3)\n'
             '    ← build (a/Probe.java:5) (expanded above)\n')
        tree('limit-zero', ['leaf', '--limit', '0'], header)
        tree('depth-zero', ['leaf', '--depth', '0'], header)
        tree('limit-one', ['leaf', '--depth', '1', '--limit', '1'], header + direct.splitlines(True)[0])
        tree('filter-empty', ['leaf', '--in-file', 'absent'], header)
        tree('filter-before-limit', ['build', '--depth', '1', '--limit', '1', '--in-file', 'Entry'],
             "Call tree for 'build':\n  build\n    ← start (a/Entry.java:3)\n")
        tree('qualified-spelling', ['p.build', '--depth', '1'],
             "Call tree for 'p.build':\n  p.build\n    ← start (a/Entry.java:3)\n")
        tree('recursive-same-line', ['recursive', '--depth', '2'],
             "Call tree for 'recursive':\n  recursive\n    ← recursive (a/Probe.java:9) (expanded above)\n")

        def explore(label, query, *, flags=(), cwd=None):
            doc = runner.json('explore', query, *flags, cwd=cwd)
            # This deliberately checks production content/identity, not just
            # whether output happens to be valid JSON.
            return doc

        doc = explore('body', 'pulse', flags=('--max-files', '1'))
        record('explore', 'body', '    6\t    int pulse() {\n    7\t        return 7;\n    8\t    }\n',
               doc['files'][0].get('source'))
        record('explore', 'file-limit', ['a/Probe.java'], [r['path'] for r in doc['files']])
        doc = explore('outline', 'Probe', flags=('--max-files', '1'))
        record('explore', 'outline', [(2, 'class', 'Probe'), (3, 'function', 'leaf'),
               (4, 'function', 'build'), (5, 'function', 'build'), (6, 'function', 'pulse'),
               (9, 'function', 'recursive'), (10, 'function', 'methodReference'), (11, 'function', 'noise')],
               [(r['line'], r['kind'], r['name']) for r in doc['files'][0].get('outline', [])])
        record('explore', 'convention-tests', [{'source': 'a/Probe.java', 'tests': ['a/ProbeTest.java']}], doc['tests'])
        for rwr in (False, True):
            flags = ('--rwr',) if rwr else ()
            doc = explore('scoped', 'pulse', flags=flags, cwd=runner.root / 'a')
            record('explore', f'scope:{rwr}', True,
                   all(r['path'].startswith('a/') for section in ('symbols', 'files', 'neighbours') for r in doc[section]))
            if rwr:
                record('explore', 'scoped-caller', True,
                       any(r['path'] == 'a/Entry.java' and r['line'] == 4 and r['link'] == 'caller' for r in doc['neighbours']))
            doc = explore('zero-files', 'pulse', flags=(*flags, '--max-files', '0'))
            record('explore', f'zero-files:{rwr}', ([], []), (doc['files'], doc['tests']))
        for query in ('zzzzunfindablezzzz', 'how does it work'):
            _, output = runner.command('--format', 'json', 'explore', query)
            try:
                doc = json.loads(output)
                result = tuple(doc[k] for k in ('symbols', 'files', 'tests', 'neighbours'))
            except (ValueError, KeyError):
                result = {'invalid_empty_response': output}
            record('explore', f'empty:{query}', ([], [], [], []),
                   result)
        for built in (False, True):
            if built:
                runner.command('graph', 'build')
            doc = explore('scoped-inheritance', 'Probe', flags=('--rwr',), cwd=runner.root / 'a')
            record('explore', f'scoped-inheritance:{built}', True,
                   any(r['path'] == 'a/LocalChild.java' and r['link'] == 'subclass' for r in doc['neighbours']))
            record('explore', f'scoped-graph:{built}', True,
                   all(r['path'].startswith('a/') for section in ('symbols', 'files', 'neighbours') for r in doc[section]))
            record('explore', f'scoped-tests:{built}', True,
                   all(p.startswith('a/') for row in doc['tests'] for p in [row['source'], *row['tests']]))
        return expected, actual
