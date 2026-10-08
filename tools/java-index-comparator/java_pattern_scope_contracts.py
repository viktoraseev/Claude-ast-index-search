"""Java boolean-flow receiver scopes through graph consumers, not MCP truth."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from java_pattern_continuations import cases as continuation_cases
from root_contracts import Runner
from java_receiver_contracts import call_edges, identity

GRAPH = 'graph:java-pattern-flow-scopes'
EXPLORE = 'explore:java-pattern-flow-scopes'
TREE = 'call-tree:java-pattern-flow-scopes'
FEATURES = {GRAPH, EXPLORE, TREE}
REASON = ('independent source/state: disposable javac-validated Java instanceof receivers, '
          'boolean short-circuit scopes, negated/ternary branches, loop bodies/updates and '
          'field shadow boundaries, nested abrupt guards, labelled/unlabelled jumps and '
          'condition-exit continuations through graph pages/reverse/path, explore and call-tree; '
          'not MCP equivalence or compiler-wide flow/dispatch')
SOURCES = {
    'Item.java': '''package fixture;
class Item {
 int marker() { return 1; }
}
''',
    'Decoy.java': '''package fixture;
class Decoy {
 int marker() { return 2; }
}
''',
    'Probe.java': '''package fixture;
class Probe {
 Decoy slot;
 int conjunction(Object value) {
  boolean matched = value instanceof Item slot && slot.marker() > 0;
  return matched ? 1 : 0;
 }
 int negatedElse(Object value) {
  if (!(value instanceof Item slot)) return 0;
  else return slot.marker();
 }
 int doubleNot(Object value) {
  if (!!(value instanceof Item slot)) return slot.marker();
  return 0;
 }
 int whileLoop(Object value) {
  while (value instanceof Item slot) { return slot.marker(); }
  return 0;
 }
 int forLoop(Object value) {
  for (; value instanceof Item slot;
       value = slot.marker()) {
   return slot.marker();
  }
  return 0;
 }
 int ternary(Object value) {
  return value instanceof Item slot ? slot.marker() : 0;
 }
 int negatedOr(Object value) {
  return !(value instanceof Item slot) || slot.marker() > 0 ? 1 : 0;
 }
 int disjunction(Object value) {
  if (value instanceof Item slot || slot.marker() > 0) return 1;
  return slot.marker();
 }
 int outside(Object value) {
  while (value instanceof Item slot) { break; }
  return slot.marker();
 }
 int doBody(Object value) {
  do { slot.marker(); } while (value instanceof Item slot);
  return 0;
 }
}
''',
}
PATTERN = ('conjunction', 'negatedElse', 'doubleNot', 'whileLoop', 'forLoop', 'ternary', 'negatedOr')
FIELD = ('disjunction', 'outside', 'doBody')
TARGETS = {'Item': ('Item.java', 3, 'marker'), 'Decoy': ('Decoy.java', 3, 'marker')}
TREE_SOURCES = {
    'Item.java': '''package fixture;
class Item {
 public String toString() { return "item"; }
}
''',
    'Probe.java': '''package fixture;
class Probe {
 void whileCall(Object value) { while (value instanceof Item slot) { slot.toString(); break; } }
 void ternaryCall(Object value) { String text = value instanceof Item slot ? slot.toString() : ""; }
 void elseCall(Object value) { if (!(value instanceof Item slot)) return; else slot.toString(); }
 void outsideCall(Object value) { while (value instanceof Item slot) { break; } String slot = ""; slot.toString(); }
 void doCall(Object value) { do { String slot = ""; slot.toString(); } while (value instanceof Item slot); }
}
''',
}


def plan_scopes(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-pattern-flow-scopes'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ('graph', 'explore:semantic-resolution', 'call-tree:semantic-resolution'):
            note = ('; separate Java pattern-flow fixture covers short-circuit expressions, '
                    'branches, loop bodies/updates and field boundaries; compiler-wide '
                    'flow and dispatch remain unresolved')
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (note, parent, note))


def source_identity(method):
    lines = [line for line, source in enumerate(SOURCES['Probe.java'].splitlines(), 1)
             if f' int {method}(' in source]
    if len(lines) != 1:
        raise ToolError('authored pattern seed is not unique')
    return 'Probe.java', lines[0], method


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('pattern scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-pattern-scope-', dir=base)).resolve())
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for path, source in SOURCES.items():
        (runner.root / path).write_text(source)
    (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        if inventory != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('pattern scope fixture inventory incomplete')
    finally:
        state.close()
    with (runner.directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                 *[str(runner.root / path) for path in SOURCES]],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('authored Java pattern fixture does not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for methods, owner in ((PATTERN, 'Item'), (FIELD, 'Decoy')):
        target = TARGETS[owner]
        for method in methods:
            seed = 'fixture.Probe.' + method
            for cap in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', cap, '--include-ambiguous')
                calls = call_edges(doc)
                record(GRAPH, f'{method}:{cap}:selection', [source_identity(method)],
                       [identity(row) for row in doc.get('matched', [])])
                if cap == 100:
                    record(GRAPH, method + ':calls', [(target, 'scoped')], calls)
                    record(GRAPH, method + ':call-sites',
                           [(target, 2 if method in ('forLoop', 'disjunction') else 1)],
                           sorted((identity(row['other']), row.get('references'))
                                  for row in doc['items'] if row['other']['kind'] == 'function'))
                else:
                    record(GRAPH, f'{method}:{cap}:bounded', {'bounded': True, 'valid': True},
                           {'bounded': len(doc['items']) <= cap,
                            'valid': all(edge == (target, 'scoped') for edge in calls)})
            doc = runner.json('graph', 'path', seed, f'fixture.{owner}.marker', '--max-depth', 1)
            record(GRAPH, method + ':path', [(source_identity(method), target)],
                   [tuple(identity(hop['symbol']) for hop in hops) for hops in doc['items']])
        doc = runner.json('graph', 'dependents', f'fixture.{owner}.marker', '--limit', 100)
        record(GRAPH, owner + ':reverse', sorted(source_identity(m) for m in methods),
               sorted(identity(row['other']) for row in doc['items']))
    doc = runner.json('explore', 'marker', '--rwr', '--max-files', 100)
    record(EXPLORE, 'callers', sorted(('Probe.java', source_identity(m)[1], 'fixture.Probe.' + m)
                                     for m in (*PATTERN, *FIELD)),
           sorted((r['path'], r['line'], r['name']) for r in doc['neighbours'] if r['link'] == 'caller'))
    # Call-tree's dotted seed means a receiver spelling, not a qualified
    # declaration. Use a name seed with an SDK decoy so its precise graph
    # callers can still be checked independently of the broader graph fixture.
    isolated_directory = runner.directory / 'tree'
    isolated_directory.mkdir()
    isolated = Runner(binary, isolated_directory)
    isolated.root.mkdir()
    (isolated.root / '.git').mkdir()
    isolated.environment['AST_INDEX_ROOT'] = str(isolated.root)
    for path, source in TREE_SOURCES.items():
        (isolated.root / path).write_text(source)
    with (isolated.directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(isolated.directory / 'classes'),
                                 *[str(isolated.root / path) for path in TREE_SOURCES]],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('authored Java pattern caller fixture does not compile; see private logs')
    isolated.command('rebuild', '--force')
    isolated.json('graph', 'build')
    wanted = [('Probe.java', line, name) for line, source in enumerate(TREE_SOURCES['Probe.java'].splitlines(), 1)
              for name in ('whileCall', 'ternaryCall', 'elseCall') if f'void {name}(' in source]
    for cap in (0, 100):
        doc = isolated.json('call-tree', 'toString', '--depth', 1, '--limit', cap)
        record(TREE, f'callers:{cap}', sorted(wanted) if cap else [],
               sorted(identity(row) for row in doc['items']))
    extra_expected, extra_actual = exercise_continuations(binary, runner.directory)
    for feature in FEATURES:
        expected[feature].update(extra_expected[feature])
        actual[feature].update(extra_actual[feature])
    return expected, actual


def exercise_continuations(binary, base):
    """Execute continuation scopes through every graph consumer."""
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
    cases = continuation_cases('Item', 'slot', 'slot.itemMarker()', 'slot.decoyMarker()')
    # Keep every caller below explore's documented ten-neighbour page.
    for offset in range(0, len(cases), 5):
        directory = Path(base) / ('continuations-' + str(offset))
        directory.mkdir()
        runner = Runner(binary, directory)
        runner.root.mkdir()
        (runner.root / '.git').mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        sources = {'Item.java': 'package fixture;\nclass Item {\n int itemMarker() { return itemSeed(); }\n int itemSeed() { return 1; }\n}\n',
                   'Decoy.java': 'package fixture;\nclass Decoy {\n int decoyMarker() { return decoySeed(); }\n int decoySeed() { return 2; }\n}\n'}
        group = cases[offset:offset + 5]
        for index, (_, body, _) in enumerate(group):
            sources[f'Case{index}.java'] = f'package fixture;\nclass Case{index} {{\n Decoy slot;\n {body}\n}}\n'
        for path, source in sources.items():
            (runner.root / path).write_text(source)
        (runner.root / 'Inventory.kt').write_text('// inventory only\n')
        (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
        state = connect(directory / 'inventory.sqlite')
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, runner.root)
            inventory = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            want = {'.java': len(sources), '.kt': 1, '.xml': 1}
            if inventory != want:
                raise ToolError('pattern continuation inventory incomplete')
            expected[GRAPH][f'continuation-inventory:{offset}'] = want
            actual[GRAPH][f'continuation-inventory:{offset}'] = inventory
        finally:
            state.close()
        with (directory / 'javac.log').open('wb') as log:
            compiled = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'classes'),
                                       *[str(runner.root / path) for path in sources]],
                                      stdout=log, stderr=log, timeout=30)
        if compiled.returncode:
            raise ToolError('authored pattern continuations did not compile; see private logs')
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')

        def record(feature, key, want, got):
            expected[feature][key], actual[feature][key] = want, got

        for index, (label, _, shadows) in enumerate(group):
            method = 'itemMarker' if shadows else 'decoyMarker'
            target = ('Item.java' if shadows else 'Decoy.java', 3, method)
            seed = f'fixture.Case{index}.run'
            source = (f'Case{index}.java', 4, 'run')
            for ambiguous in (False, True):
                for cap in (0, 1, 100):
                    doc = runner.json('graph', 'dependencies', seed, '--limit', cap,
                                      *(['--include-ambiguous'] if ambiguous else []))
                    calls = call_edges(doc)
                    record(GRAPH, f'continuation:{label}:{ambiguous}:{cap}',
                           {'matched': [source], 'valid': True, 'complete': True, 'page': True},
                           {'matched': [identity(row) for row in doc['matched']],
                            'valid': all(edge == (target, 'scoped') for edge in calls),
                            'complete': cap < 100 or calls == [(target, 'scoped')],
                            'page': len(doc['items']) == min(cap, doc['pagination']['total'])})
            owner = 'Item' if shadows else 'Decoy'
            doc = runner.json('graph', 'path', seed, f'fixture.{owner}.{method}', '--max-depth', 1)
            record(GRAPH, 'continuation:' + label + ':path', [(source, target)],
                   [tuple(identity(hop['symbol']) for hop in path) for path in doc['items']])
        for shadows, owner in ((True, 'Item'), (False, 'Decoy')):
            wanted = sorted((f'Case{index}.java', 4, 'run')
                            for index, (_, _, matched) in enumerate(group) if matched == shadows)
            prefix = f'continuation:{offset}:{owner}'
            method = owner.lower() + 'Marker'
            reverse = runner.json('graph', 'dependents', f'fixture.{owner}.{method}', '--limit', 100)
            record(GRAPH, prefix + ':reverse', wanted, sorted(identity(row['other']) for row in reverse['items']))
            doc = runner.json('explore', method, '--rwr', '--max-files', 100)
            record(EXPLORE, prefix, [(path, line, 'fixture.' + path[:-5] + '.' + name) for path, line, name in wanted],
                   sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
            for cap in (0, 100):
                doc = runner.json('call-tree', owner.lower() + 'Seed', '--depth', 2, '--limit', cap)
                branch = None
                rows = []
                for row in doc['items']:
                    if row['depth'] == 1:
                        branch = identity(row)
                    rows.append((row['depth'], branch, identity(row), row['status']))
                target = (owner + '.java', 3, method)
                want = [(1, target, target, 'shown')] + [(2, target, caller, 'shown') for caller in wanted]
                record(TREE, prefix + ':' + str(cap), sorted(want) if cap else [], sorted(rows))
    return expected, actual
