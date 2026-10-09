"""Explicit Java field projections; authored/javac/CLI evidence, not MCP."""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, connect, stable_id
from root_contracts import Runner
from java_receiver_contracts import call_edges
import mobile_contracts

GRAPH = 'graph:java-generic-fields'
EXPLORE = 'explore:java-generic-fields'
FEATURES = {GRAPH, EXPLORE}
REASON = ('independent source/state: javac-validated explicit generic field parameters, '
          'ordered/nested projections, bounds, same-line declaration ownership, captures, '
          'references, attached roots and graph pages/reverse/path/RWR callers; '
          'inherited class field mappings, raw erasure, hiding and declaration-site parent '
          'arguments, bare/this/super/lambda receivers and static/local-shadow guards; '
          'generic arrays and other semantic obligations remain pending; not MCP equivalence')
SOURCE = '''package fields;
class Alpha { int fieldAlpha(){return 1;} }
class Beta extends Alpha { int fieldBeta(){return 2;} }
class Box<T> { T value; }
class Pair<A,B> { B value; }
class Nest<T> { java.util.List<T> values; Box<T> box; }
class Bound<T extends Alpha> { T value; java.util.List<T> values; }
class Fixed extends Box<Alpha> {}
class Reordered<X,Y> extends Pair<Y,X> {}
class Relay<U> extends Reordered<U,Beta> {}
class Nested<U> extends Box<java.util.List<U>> {}
class BoundRelay<U extends Alpha> extends Bound<U> {}
class RawRelay extends Bound {}
class Hidden extends Box<Alpha> { Beta value; }
class Probe {
 int direct(Box<Alpha> a){return a.value.fieldAlpha();}
 int ordered(Pair<Beta,Alpha> a){return a.value.fieldAlpha();}
 int nested(Nest<Alpha> a){return a.values.get(0).fieldAlpha();}
 int sourceNested(Nest<Alpha> a){return a.box.value.fieldAlpha();}
 int subtype(Bound<Beta> a){return a.value.fieldBeta();}
 int rawBound(Bound a){return a.value.fieldAlpha();}
 int captured(Box<Alpha> a){class Alpha {} return ((java.util.function.IntSupplier)()->a.value.fieldAlpha()).getAsInt();}
 int reference(Box<Alpha> a){return ((java.util.function.IntSupplier)a.value::fieldAlpha).getAsInt();}
 int colliding(){int n=0; {class Holder<T> { T value; } Holder<Alpha> a=null; n+=a.value.fieldAlpha();} {class Holder<X,T> { T value; } Holder<Alpha,Beta> b=null; n+=b.value.fieldBeta();} return n;}
 int wildcardBound(Bound<?> a){return a.value.fieldAlpha();}
 int wildcardNested(Bound<?> a){return a.values.get(0).fieldAlpha();}
 int inherited(Fixed a){return a.value.fieldAlpha();}
 int reordered(Reordered<Alpha,Beta> a){return a.value.fieldAlpha();}
 int relayed(Relay<Alpha> a){return a.value.fieldAlpha();}
 int nestedParent(Nested<Alpha> a){return a.value.get(0).fieldAlpha();}
 int inheritedBound(BoundRelay<Beta> a){return a.value.fieldBeta();}
 int inheritedRaw(RawRelay a){return a.value.fieldAlpha();}
 int rawSubclass(Relay a){return a.value.toString().length();}
 int inheritedWildcard(BoundRelay<?> a){return a.value.fieldAlpha();}
 int inheritedNested(BoundRelay<Beta> a){return a.values.get(0).fieldBeta();}
 int inheritedCaptured(Relay<Alpha> a){class Alpha {} return ((java.util.function.IntSupplier)()->a.value.fieldAlpha()).getAsInt();}
 int inheritedReference(Relay<Alpha> a){return ((java.util.function.IntSupplier)a.value::fieldAlpha).getAsInt();}
 int inheritedHiding(Hidden a){return a.value.fieldBeta();}
 int inheritedLocal(){int n=0; {class Holder<X,Y> extends Pair<Y,X> {} Holder<Alpha,Beta> a=null; n+=a.value.fieldAlpha();} {class Holder<X,Y> extends Pair<X,Y> {} Holder<Alpha,Beta> a=null; n+=a.value.fieldBeta();} return n;}
 int crossFile(Imported<Alpha> a){return a.value.fieldAlpha();}
 int inheritedBare(){return value.fieldAlpha();}
 int inheritedThis(){return this.value.fieldAlpha();}
 int inheritedSuper(){return super.value.fieldAlpha();}
 int inheritedLambda(){return ((java.util.function.IntSupplier)()->value.fieldAlpha()).getAsInt();}
}
'''
IMPORTED = '''package fields;
import fields.Pair;
class Imported<X> extends Pair<Beta,X> { class Alpha {} }
'''
CALLS = {name: [('Probe.java', 2, 'fieldAlpha')] for name in
         ('direct', 'ordered', 'nested', 'sourceNested', 'rawBound', 'captured', 'reference',
          'wildcardBound', 'wildcardNested')}
CALLS['subtype'] = [('Probe.java', 3, 'fieldBeta')]
CALLS['colliding'] = [('Probe.java', 2, 'fieldAlpha'), ('Probe.java', 3, 'fieldBeta')]
ORIGINAL_CALLS = dict(CALLS)
CALLS.update({name: [('Probe.java', 2, 'fieldAlpha')] for name in
              ('inherited', 'reordered', 'relayed', 'nestedParent', 'inheritedRaw',
               'inheritedWildcard', 'inheritedCaptured', 'inheritedReference', 'crossFile')})
CALLS.update({name: [('Probe.java', 3, 'fieldBeta')] for name in
              ('inheritedBound', 'inheritedNested', 'inheritedHiding')})
CALLS['inheritedLocal'] = [('Probe.java', 2, 'fieldAlpha'), ('Probe.java', 3, 'fieldBeta')]
CALLS['rawSubclass'] = []
LEXICAL_CALLS = {name: [('Probe.java', 2, 'fieldAlpha')] for name in
                 ('inheritedBare', 'inheritedThis', 'inheritedSuper', 'inheritedLambda')}
INHERITED_CALLS = {name: targets for name, targets in CALLS.items() if name not in ORIGINAL_CALLS}


def phase_source(calls, inherited):
    """Keep each authored neighbour population within explore's ten-row cap."""
    declarations, body = SOURCE.split('class Probe {\n', 1)
    if not inherited:
        declarations = declarations.split('class Fixed extends', 1)[0]
    methods = [line for line in body.splitlines() if any('int ' + name + '(' in line for name in calls)]
    owner = 'class Probe extends Relay<Alpha> {' if calls is LEXICAL_CALLS else 'class Probe {'
    return declarations + owner + '\n' + '\n'.join(methods) + '\n}\n'


def plan_fields(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-generic-fields-v1'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ('graph', 'explore:semantic-resolution'):
            note = '; explicit Java generic field checklist executed separately; inherited generic field mappings, generic arrays and other recorded obligations remain pending; not MCP equivalence'
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' AND instr(reason,?)=0",
                          (note, parent, note))
            completed = '; separate inherited Java field checklist executes explicit/fixed/reordered/multihop/nested and bounded/raw/wildcard owner projections, hiding, local/cross-file declaration sites, captures/references, bare/this/super/lambda receivers and static/local-shadow guards, with attached declaring roots through graph pages/reverse/path and bounded RWR callers; generic arrays and other retained parent obligations remain pending; independent source/javac/CLI, not MCP equivalence'
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' AND instr(reason,?)=0",
                          (completed, parent, completed))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('generic field fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-generic-fields-', dir=base)))
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    source = runner.root / 'Probe.java'
    source.write_text(phase_source(ORIGINAL_CALLS, False))
    (runner.root / 'Inventory.kt').write_text('// inventory witness only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    inventory = connect(runner.directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(inventory, runner.root)
        counts = dict(inventory.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
        if counts != {'.java': 1, '.kt': 1, '.xml': 1}:
            raise ToolError('generic field inventory incomplete')
    finally:
        inventory.close()

    def compile_source(label):
        with (runner.directory / (label + '.javac.log')).open('wb') as log:
            return subprocess.run(['javac', '-proc:none', '-d', str(runner.directory / 'classes'),
                                   *map(str, sorted(runner.root.glob('*.java')))],
                                  stdout=log, stderr=log, timeout=30).returncode

    if compile_source('positive'):
        raise ToolError('authored generic field sources do not compile; see private logs')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    def source_path(value):
        relative = runner.path(value)
        if relative.startswith('project/'):
            return relative.removeprefix('project/')
        if relative.startswith('attached/'):
            return 'extra/' + relative.removeprefix('attached/')
        raise ToolError('generic field result has an unknown owning root')

    def identity(row):
        return source_path(row['path']), row['line'], row['name']

    record(GRAPH, 'inventory', {'.java': 1, '.kt': 1, '.xml': 1}, counts)
    for phase, active_calls, inherited in (('', ORIGINAL_CALLS, False),
                                           ('inherited/', INHERITED_CALLS, True),
                                           ('lexical/', LEXICAL_CALLS, True)):
        active_source = phase_source(active_calls, inherited)
        source.write_text(active_source)
        if inherited:
            (runner.root / 'Imported.java').write_text(IMPORTED)
            if compile_source('inherited-positive'):
                raise ToolError('authored inherited generic field sources do not compile')
        callers = {name: ('Probe.java', next(i for i, row in enumerate(active_source.splitlines(), 1)
                                           if 'int ' + name + '(' in row), name) for name in active_calls}
        for prefix in ('', 'extra/'):
            if prefix:
                attached = runner.directory / 'attached'
                attached.mkdir(exist_ok=True)
                (attached / 'Probe.java').write_text(active_source)
                if inherited:
                    (attached / 'Imported.java').write_text(IMPORTED)
                if not inherited:
                    runner.command('subtree', 'add', 'extra', attached)
            runner.command('rebuild', '--force')
            runner.json('graph', 'build')
            for name, wanted in active_calls.items():
                seed = 'fields.Probe.' + name
                flags = ('--subtree', 'extra') if prefix else ('--local',)
                def rooted(value):
                    return (prefix + value[0], *value[1:])
                for ambiguous in (False, True):
                    for limit in (0, 1, 100):
                        doc = runner.json('graph', 'dependencies', seed, '--limit', limit, *flags,
                                          *(['--include-ambiguous'] if ambiguous else []))
                        calls = [(source_path(p), line, member) for (p, line, member), _ in call_edges(doc)]
                        targets = sorted(rooted(target) for target in wanted)
                        record(GRAPH, phase + prefix + f'{name}:{limit}:{ambiguous}',
                               {'matched': [rooted(callers[name])], 'valid': True, 'complete': True, 'page': True},
                               {'matched': [identity(row) for row in doc['matched']],
                                'valid': all(target in targets for target in calls),
                                'complete': limit < 100 or sorted(calls) == targets,
                                'page': len(doc['items']) == min(limit, doc['pagination']['total'])})
                for target in wanted:
                    path = runner.json('graph', 'path', seed, 'fields.' + ('Alpha' if target[1] == 2 else 'Beta') + '.' + target[2],
                                       '--max-depth', 1, *flags)
                    record(GRAPH, phase + prefix + name + ':path:' + target[2], [(rooted(callers[name]), rooted(target))],
                           sorted(tuple(identity(hop['symbol']) for hop in hops) for hops in path['items']))
            for target in ('fieldAlpha', 'fieldBeta'):
                wanted = [rooted(callers[name]) for name, targets in active_calls.items() if any(t[2] == target for t in targets)]
                doc = runner.json('graph', 'dependents', 'fields.' + ('Alpha' if target == 'fieldAlpha' else 'Beta') + '.' + target,
                                  '--limit', 100, *flags)
                record(GRAPH, phase + prefix + target + ':reverse', sorted(wanted), sorted(identity(row['other']) for row in doc['items']))
                doc = runner.json('explore', target, '--rwr', '--max-files', 100, *flags)
                # Explore searches signatures too. Beta and fieldBeta share a
                # signature line, so the authored seed union includes the class's
                # incoming type references too. Each full authored population
                # fits the ten-neighbour cap. Exact callable dispatch stays
                # independently asserted by dependencies/reverse/path above.
                neighbours = wanted + ([rooted(callers['ordered'])] if not inherited and target == 'fieldBeta' else [])
                extra_neighbours = ([('Imported.java', 3, 'fields.Imported'), ('Probe.java', 10, 'fields.Relay'),
                                     ('Probe.java', 14, 'fields.Hidden.value')] +
                                    ([(callers['reordered'][0], callers['reordered'][1], 'fields.Probe.reordered')]
                                     if 'reordered' in callers else [])
                                    if inherited and target == 'fieldBeta' else [])
                if phase == 'lexical/' and target == 'fieldAlpha':
                    # This small population also surfaces all incoming class
                    # type references from Alpha's signature seed. The other
                    # populations fill the ten-row cap with callable results.
                    extra_neighbours.extend((('Probe.java', 7, 'fields.Bound'),
                                             ('Probe.java', 8, 'fields.Fixed'),
                                             ('Probe.java', 12, 'fields.BoundRelay'),
                                             ('Probe.java', 14, 'fields.Hidden'),
                                             ('Probe.java', 15, 'fields.Probe')))
                record(EXPLORE, phase + prefix + target + ':callers',
                       sorted([(p, line, 'fields.Probe.' + name) for p, line, name in neighbours] +
                              [(prefix + p, line, name) for p, line, name in extra_neighbours]),
                       sorted((source_path(row['path']), row['line'], row['name']) for row in doc['neighbours'] if row['link'] == 'caller'))
    (runner.root / 'Imported.java').unlink()
    for label, receiver, expression in (
            ('raw', 'Box', 'a.value.fieldAlpha()'),
            ('wildcard', 'Box<?>', 'a.value.fieldAlpha()'),
            ('missing', 'Box<Beta>', 'a.value.absent()'),
            ('arity', 'Box<Alpha,Beta>', 'a.value.fieldAlpha()'),
            ('raw-nested', 'Bound', 'a.values.get(0).fieldAlpha()'),
            ('inherited-raw', 'Relay', 'a.value.fieldAlpha()'),
            ('inherited-wildcard', 'Relay<?>', 'a.value.fieldAlpha()'),
            ('inherited-arity', 'Relay<Alpha,Beta>', 'a.value.fieldAlpha()'),
            ('inherited-raw-nested', 'RawRelay', 'a.values.get(0).fieldAlpha()'),
            ('inherited-hidden', 'Hidden', 'a.value.fieldAlpha()'),
            ('inherited-local-shadow', 'Object value', 'value.fieldAlpha()'),
            ('inherited-static-bare', '', 'value.fieldAlpha()'),
            ('inherited-static-this', '', 'this.value.fieldAlpha()'),
            ('inherited-static-super', '', 'super.value.fieldAlpha()')):
        modifier = 'static ' if label.startswith('inherited-static-') else ''
        source.write_text('package fields;\nclass Alpha { int fieldAlpha(){return 0;} }\n'
                          'class Beta {}\nclass Box<T> { T value; }\n'
                          'class Bound<T extends Alpha> { java.util.List<T> values; }\n'
                          'class Relay<U> extends Box<U> {}\n'
                          'class RawRelay extends Bound {}\n'
                          'class Hidden extends Box<Alpha> { Beta value; }\n'
                          'class Probe extends Relay<Alpha> { ' + modifier + 'int invalid(' +
                          (receiver if label == 'inherited-local-shadow' else receiver + ' a' if receiver else '') +
                          '){return ' + expression + ';} }\n')
        if not compile_source('negative-' + label):
            raise ToolError('javac accepted a claimed generic field guard')
        runner.command('rebuild', '--force')
        runner.json('graph', 'build')
        for ambiguous in (False, True):
            record(GRAPH, label + ':guard:' + str(ambiguous), [],
                   call_edges(runner.json('graph', 'dependencies', 'fields.Probe.invalid', '--local',
                                          *(['--include-ambiguous'] if ambiguous else []))))
    return expected, actual
