"""Chained/generic Java declaration sites: authored source truth, not MCP."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect
import mobile_contracts
from root_contracts import Runner
from java_receiver_contracts import call_edges, identity

GRAPH = 'graph:java-chained-generic-receiver-sites'
EXPLORE = 'explore:java-chained-generic-receiver-sites'
FEATURES = {GRAPH, EXPLORE}
SOURCES = {
    'Leaf.java': '''package sites;
class Leaf {
 int marker() { return 1; }
 Leaf next() { return this; }
}
''',
    'Holder.java': '''package sites;
class Holder { Leaf leaf; }
''',
    'Box.java': '''package sites;
class Box<T> {
 T get() { return null; }
}
''',
    'Probe.java': Path(__file__).with_name('JavaReceiverSiteProbe.java').read_text(),
    'LocalProbe.java': '''package sites;
import java.util.List;
class LocalProbe {
 int localGeneric() {
  class Leaf {
   int localOnly() { return 1; }
  }
  List<Leaf> input = null;
  return input.get(0).localOnly();
 }
 int localBox() {
  class Leaf {
   int localBoxOnly() { return 2; }
  }
  class Box<T> {
   T get() { return null; }
  }
  Box<Leaf> input = null;
  return input.get().localBoxOnly();
 }
}
''',
    'other/Leaf.java': 'package sites.other; public class Leaf {}\n',
}
CALLERS = ('chain', 'field', 'list', 'optional', 'nested', 'box', 'invoke', 'reference',
           'variable', 'fieldGeneric')


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('receiver site artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-receiver-sites-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    for name, source in SOURCES.items():
        path = runner.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (runner.root / 'Inventory.kt').write_text('// non-Java inventory sentinel\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': len(SOURCES), '.kt': 1, '.xml': 1}:
            raise ToolError('receiver site inventory incomplete')
    finally:
        inventory.close()

    def compile_sources(label):
        with (runner.directory / (label + '.stdout.log')).open('wb') as stdout, \
                (runner.directory / (label + '.stderr.log')).open('wb') as stderr:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                   *[str(runner.root / name) for name in SOURCES]],
                                  stdout=stdout, stderr=stderr, timeout=30).returncode
    if compile_sources('positive-javac'):
        raise ToolError('authored receiver sites do not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    record(GRAPH, 'complete-inventory', {'.java': len(SOURCES), '.kt': 1, '.xml': 1}, counts)
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    marker = ('Leaf.java', 3, 'marker')
    sources = {}
    for name in CALLERS:
        sources[name] = ('Probe.java', next(line for line, source in enumerate(SOURCES['Probe.java'].splitlines(), 1)
                                           if 'int ' + name + '(' in source), name)
        seed = name if name == 'invoke' else 'sites.Probe.' + name
        wanted = [(marker, 'scoped')]
        if name == 'chain':
            wanted.append((('Leaf.java', 4, 'next'), 'scoped'))
        if name == 'box':
            wanted.append((('Box.java', 3, 'get'), 'scoped'))
        wanted.sort()
        for ambiguous in (False, True):
            for limit in (0, 1, 100):
                doc = runner.json('graph', 'dependencies', seed, '--limit', limit,
                                  *(['--include-ambiguous'] if ambiguous else []))
                calls = call_edges(doc)
                # Pages can also contain type/field edges; the complete callable
                # page is compared independently of the native DB or its rows.
                record(GRAPH, f'{name}:{limit}:{ambiguous}',
                       {'matched': [sources[name]], 'valid': True, 'complete': True, 'page': True},
                       {'matched': [identity(row) for row in doc['matched']],
                        'valid': all(edge in wanted for edge in calls) and len(set(calls)) == len(calls),
                        'complete': limit < 100 or calls == wanted,
                        'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
        path = runner.json('graph', 'path', seed, 'sites.Leaf.marker', '--max-depth', 1)
        record(GRAPH, name + ':path', [(sources[name], marker)],
               sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']))
    reverse = runner.json('graph', 'dependents', 'sites.Leaf.marker', '--limit', 100)
    record(GRAPH, 'reverse', sorted(sources.values()), sorted(identity(row['other']) for row in reverse['items']))
    doc = runner.json('explore', 'marker', '--rwr', '--max-files', 100)
    record(EXPLORE, 'callers', sorted((path, line, name if name == 'invoke' else 'sites.Probe.' + name)
                                    for path, line, name in sources.values()),
           sorted((row['path'], row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'sites.LocalProbe.localGeneric',
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'local-generic:' + str(ambiguous),
               [(('LocalProbe.java', 6, 'localOnly'), 'local')], call_edges(doc))
        doc = runner.json('graph', 'dependencies', 'sites.LocalProbe.localBox',
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'local-generic-container:' + str(ambiguous),
               [(('LocalProbe.java', 13, 'localBoxOnly'), 'local'),
                (('LocalProbe.java', 16, 'get'), 'scoped')], call_edges(doc))

    # Applicable but invalid generic receivers must not inherit a package Leaf.
    guard = '''package sites; import java.util.*;
class Probe {
 int invalid() {
  class Leaf {}
  List<Leaf> input = null;
  return input.get(0).marker();
 }
}
'''
    (runner.root / 'Probe.java').write_text(guard)
    if not compile_sources('negative-javac'):
        raise ToolError('javac accepted a generic local shadow member leak')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'sites.Probe.invalid',
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'missing-member:' + str(ambiguous), [], call_edges(doc))
    (runner.root / 'Probe.java').write_text('''package sites;
import missing.Leaf; import java.util.List;
class Probe {
 int invalid(List<Leaf> input) { return input.get(0).marker(); }
}
''')
    if not compile_sources('external-negative-javac'):
        raise ToolError('javac accepted an unresolved external type import')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'sites.Probe.invalid',
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'external-type:' + str(ambiguous), [], call_edges(doc))
    (runner.root / 'Probe.java').write_text('''package guards;
import sites.*; import sites.other.*; import java.util.List;
class Probe {
 int invalid(List<Leaf> input) { return input.get(0).marker(); }
}
''')
    if not compile_sources('ambiguous-negative-javac'):
        raise ToolError('javac accepted ambiguous generic type imports')
    runner.command('rebuild', '--force')
    runner.json('graph', 'build')
    for ambiguous in (False, True):
        doc = runner.json('graph', 'dependencies', 'guards.Probe.invalid',
                          *(['--include-ambiguous'] if ambiguous else []))
        record(GRAPH, 'ambiguous-type:' + str(ambiguous), [], call_edges(doc))
    return expected, actual
