"""Java unused-dependency contracts on authored source; never MCP equivalence.

Lexical type-name usage is distinct from Java import/receiver resolution. The
target contract currently proves only independently edgeless Maven graphs.
"""
from pathlib import Path
from itertools import product
import re
import tempfile

from common import ToolError, canonical_json, stable_id
from root_contracts import Runner
import module_contracts

FEATURES = {'unused-deps', 'unused-deps:java-ownership', 'unused-deps:java-types',
            'unused-deps:transitive'}
REASON = ('independent source/state: disposable Java lexical dependency usage, '
          'module ownership, type collection, API-chain usage and rendered options; not MCP equivalence')
PENDING = {
    'unused-deps:semantic-resolution': 'Java visibility, ambiguous wildcard imports, lexical type/variable '
        'shadowing, inherited/nested static members, receiver dispatch and attached-root resolution '
        'remain unresolved; authored import/type identities do not establish compiler-wide or MCP equivalence',
    'unused-deps:android-ownership': 'Default XML/resource dependency usage, qualified class/resource '
        'ownership and Android module collisions need a separate applicable Java Android contract; '
        'zero XML/resource samples in the lexical fixture do not establish it',
}
TARGET_REASON = 'independent source/state: full inventory and Maven descriptors prove an edgeless Java module graph; executed missing/edgeless unused-deps output; not MCP equivalence'


def plan_unused(state, root):
    if root is None:
        return
    status, reason = 'pending', 'Target dependency graph requires Java usage and Android ownership contracts'
    try:
        _, edges, _ = module_contracts.graph(state, root)
        if not edges:
            status, reason = 'implemented', TARGET_REASON
    except module_contracts.Unresolved as error:
        reason = str(error)
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-unused-dependencies'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for feature, why in {**PENDING, 'unused-deps:target': reason}.items():
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, status if feature == 'unused-deps:target' else 'pending', why))
        if status == 'implemented':
            feature, subject = 'unused-deps:target', 'source-module-graph'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def result(output, verbose):
    """Read identities and arithmetic, retaining unrecognized output as failure."""
    summary = re.search(r'^Total: (\d+) unused, (\d+) exported, (\d+) used of (\d+) dependencies$', output, re.M)
    unused = sorted(re.findall(r'^  ✗ (.+) \([^)]+\)$', output, re.M))
    exported = sorted(re.findall(r'^  ⚡ (.+) \(api\)$', output, re.M))
    direct = sorted(re.findall(r'^  ✓ (.+) - (\d+) symbols(?:: (.*))?$',
                              output.split('=== Transitive Usage ===')[0], re.M))
    via = sorted(re.findall(r'^    └─ via (.+): (.+)$', output, re.M))
    sections = {name: int(n) for name, n in re.findall(r'^  - (Direct|Transitive|XML|Resources|Exported \(api\)): (\d+)$', output, re.M)}
    return {'summary': list(map(int, summary.groups())) if summary else None,
            'unused': unused, 'exported': exported, 'direct': direct if verbose else [],
            'via': via if verbose else [], 'sections': sections,
            'strict': 'Checking: direct imports only (strict mode)' in output,
            'transitive_section': '=== Transitive Usage ===' in output if verbose else False}


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('unused-deps artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='unused-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
        declared_edges = {}

        def write(path, source):
            file = runner.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)

        def module(name, dependencies=()):
            declared_edges[name.replace('/', '.')] = sorted(
                (dep.replace('/', '.'), dep, kind) for kind, dep in dependencies)
            write(name + '/build.gradle', 'dependencies {\n' + ''.join(
                f'    {kind}(project(\":{dep.replace("/", ":")}\"))\n' for kind, dep in dependencies) + '}\n')

        for name in ('lib', 'libExtra', 'lib/child', 'dead', 'modes', 'many', 'facade', 'leaf', 'unusedFacade', 'unusedLeaf', 'appExtra', 'app/child', 'a_p', 'axp'):
            module(name)
        module('facade', [('api', 'leaf')])
        module('unusedFacade', [('api', 'unusedLeaf')])
        module('app', [('implementation', name) for name in ('lib', 'dead', 'modes', 'many', 'facade', 'unusedFacade', 'unusedLeaf')]
               + [('api', 'lib/child')])
        module('a_p', [('implementation', 'dead')])
        write('lib/Owned.java', 'class Owned {}\n')
        write('libExtra/PrefixOnly.java', 'class PrefixOnly {}\n')
        write('lib/child/ChildOnly.java', 'class ChildOnly {}\n')
        write('dead/Dead.java', 'class Dead {}\n')
        write('modes/Mode.java', 'enum Mode { FIRST }\nrecord Pair(int n) {}\ninterface Port {}\n')
        write('many/Types.java', ''.join(f'class Type{i:03d} {{}}\n' for i in range(110)))
        write('leaf/Leaf.java', 'class Leaf {}\n')
        write('unusedLeaf/UnusedLeaf.java', 'class UnusedLeaf {}\n')
        write('facade/Facade.java', 'class Facade {}\n')
        write('unusedFacade/UnusedFacade.java', 'class UnusedFacade {}\n')
        write('app/Main.java', '''class Main {
    Mode mode; Pair pair; Port port; Type109 last; Leaf leaf;
    PrefixOnly prefix; ChildOnly child;
    // Dead Owned UnusedLeaf are comment noise
    String noise = "Dead Owned UnusedLeaf";
}
''')
        write('appExtra/Other.java', 'class Other { Dead dead; Owned owned; }\n')
        write('app/child/Other.java', 'class Other { Dead dead; Owned owned; }\n')
        module('app/child')
        write('a_p/Empty.java', 'class Empty {}\n')
        write('axp/Other.java', 'class Other { Dead dead; }\n')
        # A root Maven module owns only root files, not every nested module.
        write('pom.xml', '<project><groupId>fixture</groupId><artifactId>rootOwner</artifactId>'
              '<dependencies><dependency><groupId>fixture</groupId><artifactId>rootlib</artifactId>'
              '</dependency></dependencies></project>')
        write('rootlib/pom.xml', '<project><groupId>fixture</groupId><artifactId>rootlib</artifactId></project>')
        write('rootlib/RootDead.java', 'class RootDead {}\n')
        write('Root.java', 'class Root {}\n')
        module('rootconsumer')
        write('rootconsumer/Use.java', 'class Use { RootDead dead; }\n')
        declared_edges['rootOwner'] = [('rootlib', 'rootlib', 'compile')]
        runner.command('rebuild', '--force', '--max-files', '0')
        for name, edges in declared_edges.items():
            if not edges:
                continue
            _, output = runner.command('deps', name)
            if sorted(module_contracts.edge_rows(output, 'deps')) != edges:
                raise ToolError('authored unused-deps fixture did not establish its independent dependency graph')

        def sample(label, feature, module_name, args, want):
            _, output = runner.command('unused-deps', module_name, *args)
            expected[feature][label] = want
            actual[feature][label] = result(output, '--verbose' in args)

        strict_unused = ['dead', 'facade', 'lib', 'unusedFacade', 'unusedLeaf']
        direct = [('many', '1', 'Type109'), ('modes', '3', 'Mode, Pair, Port')]
        for verbose in (False, True):
            for flags in (('--strict',), ('--no-transitive', '--no-xml', '--no-resources')):
                want = {'summary': [5, 0, 3, 8], 'unused': strict_unused, 'exported': [],
                        'direct': direct + [('lib.child', '1', 'ChildOnly')] if verbose else [],
                        'via': [], 'sections': {'Direct': 3}, 'strict': True, 'transitive_section': False}
                # lib.child is directly referenced, even though its parent lib is not.
                want['direct'] = sorted(want['direct'])
                sample(str([verbose, flags]), 'unused-deps', 'app', (*flags, *(('--verbose',) if verbose else ())), want)
        for feature in ('unused-deps:java-ownership', 'unused-deps:java-types'):
            sample('scope-and-type-collection', feature, 'app', ('--strict', '--verbose'),
                   {'summary': [5, 0, 3, 8], 'unused': strict_unused, 'exported': [],
                    'direct': sorted(direct + [('lib.child', '1', 'ChildOnly')]), 'via': [],
                    'sections': {'Direct': 3}, 'strict': True, 'transitive_section': False})
        sample('literal-underscore', 'unused-deps:java-ownership', 'a_p', ('--strict', '--verbose'),
               {'summary': [1, 0, 0, 1], 'unused': ['dead'], 'exported': [], 'direct': [], 'via': [],
                'sections': {'Direct': 0}, 'strict': True, 'transitive_section': False})
        sample('root-ownership', 'unused-deps:java-ownership', 'rootOwner', ('--strict', '--verbose'),
               {'summary': [1, 0, 0, 1], 'unused': ['rootlib'], 'exported': [], 'direct': [], 'via': [],
                'sections': {'Direct': 0}, 'strict': True, 'transitive_section': False})
        for flags in (('--no-xml', '--no-resources'), ()):
            sections = {'Direct': 3, 'Transitive': 1}
            if not flags:
                sections.update(XML=0, Resources=0)
            sample(str(flags), 'unused-deps:transitive', 'app', (*flags, '--verbose'),
                   {'summary': [4, 0, 4, 8], 'unused': ['dead', 'lib', 'unusedFacade', 'unusedLeaf'],
                    'exported': [], 'direct': sorted(direct + [('lib.child', '1', 'ChildOnly')]),
                    'via': [('leaf', 'Leaf')], 'sections': sections, 'strict': False, 'transitive_section': True})
        # An unused re-export is intentionally exported, not used merely because
        # a dependency chain exists. No source mentions UnusedLeaf.
        sample('unused-api-export', 'unused-deps:transitive', 'unusedFacade', ('--verbose',),
               {'summary': [0, 1, 0, 1], 'unused': [], 'exported': ['unusedLeaf'], 'direct': [], 'via': [],
                'sections': {'Direct': 0, 'Transitive': 0, 'XML': 0, 'Resources': 0, 'Exported (api)': 1},
                'strict': False, 'transitive_section': True})
        for name, wanted in [('absent', "Module 'absent' not found in index.\n"),
                             ('leaf', "Module 'leaf' has no dependencies.\n")]:
            _, output = runner.command('unused-deps', name, '--strict')
            expected['unused-deps'][name], actual['unused-deps'][name] = wanted, output

        # All independently toggled checks, with and without verbose rendering.
        for transitive, xml, resources, verbose in product((False, True), repeat=4):
            flags = tuple(flag for enabled, flag in ((transitive, '--no-transitive'),
                          (xml, '--no-xml'), (resources, '--no-resources')) if not enabled)
            if verbose:
                flags += ('--verbose',)
            sections = {'Direct': 3}
            sections.update({name: count for enabled, name, count in (
                (transitive, 'Transitive', 1), (xml, 'XML', 0), (resources, 'Resources', 0)) if enabled})
            sample(str([transitive, xml, resources, verbose]), 'unused-deps', 'app', flags,
                   {'summary': [4 if transitive else 5, 0, 4 if transitive else 3, 8],
                    'unused': ['dead', 'lib', 'unusedFacade', 'unusedLeaf'] if transitive else strict_unused,
                    'exported': [], 'direct': sorted(direct + [('lib.child', '1', 'ChildOnly')]) if verbose else [],
                    'via': [('leaf', 'Leaf')] if transitive and verbose else [], 'sections': sections,
                    'strict': not (transitive or xml or resources), 'transitive_section': transitive and verbose})

        # The API closure must terminate on cycles, deduplicate diamond paths,
        # and not inherit the old transitive_deps table's five-hop ceiling.
        for i in range(1, 7):
            module(f'middle{i}', [('api', f'middle{i+1}' if i < 6 else 'leaf')])
        module('leaf', [('api', 'facade')])
        for label, edges in [('deep-cycle', [('api', 'middle1')]),
                             ('diamond-cycle', [('api', 'middle1'), ('api', 'leaf')]),
                             ('implementation-is-not-export', [('implementation', 'leaf')])]:
            module('facade', edges)
            runner.command('rebuild', '--force', '--max-files', '0')
            used = label != 'implementation-is-not-export'
            sample(label, 'unused-deps:transitive', 'app', ('--no-xml', '--no-resources', '--verbose'),
                   {'summary': [4 if used else 5, 0, 4 if used else 3, 8],
                    'unused': ['dead', 'lib', 'unusedFacade', 'unusedLeaf'] if used else strict_unused,
                    'exported': [], 'direct': sorted(direct + [('lib.child', '1', 'ChildOnly')]),
                    'via': [('leaf', 'Leaf')] if used else [],
                    'sections': {'Direct': 3, 'Transitive': int(used)}, 'strict': False, 'transitive_section': True})
        return expected, actual


def verify_target(fixture):
    from audit import Unsupported
    try:
        modules, edges, fingerprints = module_contracts.graph(fixture.state, fixture.root)
        if edges:
            raise module_contracts.Unresolved('nonempty target requires a Java dependency usage/Android ownership oracle')
    except module_contracts.Unresolved as error:
        raise Unsupported(str(error)) from error
    absent = '__audit_absent_module__'
    while absent in modules:
        absent += '_'
    expected, actual = {}, {}
    for name in [absent, *sorted(modules)]:
        for flags in (('--strict',), ('--verbose',), ('--no-transitive', '--no-xml', '--no-resources')):
            key = canonical_json([name, flags])
            expected[key] = f"Module '{name}' " + ('has no dependencies.\n' if name in modules else 'not found in index.\n')
            actual[key] = fixture.text_cli('unused-deps', name, *flags)
    return {'source': TARGET_REASON, 'descriptors': fingerprints, 'samples': expected}, actual, \
        {(key, value) for key, value in expected.items()}, {(key, value) for key, value in actual.items()}
