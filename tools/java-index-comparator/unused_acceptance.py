"""Executed unused-dependency acceptance with all retained Java obligations."""
import hashlib
import json
from pathlib import Path
import sys

from common import ToolError, canonical_json, stable_id
import parent_acceptance as engine
import unused_acceptance_spec as spec
from scope_acceptance_spec import population_shape, assertion_keys as scope_assertion_keys
import mobile_contracts
import module_contracts
import unused_dep_contracts

LABEL = 'Java unused dependencies'
FEATURE = 'unused-deps:semantic-resolution'
SUBJECT = 'java-unused-dependency-acceptance-v1'
TARGET = 'unused-deps:target'
TARGET_PARENT = FEATURE + ':target-proof'
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('executed Java source/javac/CLI acceptance: imports, lexical/flow/member '
          'resolution, result inference, source classpaths, API exports, Java '
          'resources and CLI options; independent target module inventory proof; '
          'not MCP equivalence or compiler equivalence; retained failed Java '
          'inference cases block completion; XML-only and foreign syntax out-of-scope')
SHARED = {'global:format:java-modules', 'global:scope:java-module-aliases',
          'global:scope:java-module-directories', 'global:scope:java-module-roots'}


def specification():
    return {'parent': FEATURE, 'criteria': spec.criteria(),
            'cli_surface_sha256': spec.REVIEWED_SURFACE,
            'pending_contract_decisions': spec.API_DECISIONS,
            'scope': 'full advertised Java unused-deps API and retained semantic cases',
            'target': 'complete file inventory, independent Maven graph and executed target commands; nonempty/unmodelled graphs remain unresolved',
            'boundaries': [
                'src/main.rs UnusedDeps and Cli; README.md unused-deps',
                'external/platform obligations: indexed source owners, collision guards and explicitly exercised JDK projections',
                'no advertised binary-loader or compiler classpath-order API; adding such an API requires user direction, not an inferred exclusion or pass',
                'mixed Android criteria preserve Java class layout/R ownership; XML namespace/entity parsing and foreign parsers are explicitly out-of-scope']}


def required_acceptance_features():
    from audit import required_features, JAVA_EXCLUDED_FEATURES
    return {f for f in required_features() - JAVA_EXCLUDED_FEATURES
            if (f == 'unused-deps' or f.startswith('unused-deps:')) and f not in {FEATURE, TARGET}} | SHARED


def unmapped_commands():
    return [] if spec.cli_surface() == spec.REVIEWED_SURFACE else ['changed unused-deps API']


def validate_specification(checklist):
    engine.validate_specification(checklist, policy=sys.modules[__name__])
    if unmapped_commands() or any(not item.get(key) for item in checklist['criteria']
                                 for key in ('population_sha256', 'input', 'expected', 'stop')):
        raise AcceptancePending('unreviewed Java dependency API or incomplete acceptance criterion')


def assertion_keys(samples, criterion):
    return scope_assertion_keys(samples, criterion['feature'])


def validate_population(samples, criterion):
    try:
        digest = population_shape(samples, criterion['feature'])
    except ValueError as error:
        raise AcceptancePending(str(error)) from error
    if digest != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained Java dependency assertion branches: ' + criterion['feature'])


def criterion_for_proof(row, criterion):
    if row['subject'] != criterion['subject']:
        raise AcceptancePending('unreviewed retained Java dependency subject: ' + row['feature'])
    return criterion


def retain_note(state, source):
    row = source.execute('SELECT reason FROM coverage WHERE feature=?', (FEATURE,)).fetchone()
    if row:
        with state:
            state.execute('INSERT OR IGNORE INTO parent_acceptance_notes VALUES (?,?,?)',
                          (FEATURE, stable_id(row[0]), row[0]))


def plan(state, *, java_only):
    if not java_only:
        return
    retain_note(state, state)
    engine.plan(state, java_only=True, policy=sys.modules[__name__])
    with state:
        state.execute('INSERT OR IGNORE INTO parent_acceptance_families VALUES (?,?)', (TARGET_PARENT, TARGET))
        state.execute('INSERT OR IGNORE INTO parent_acceptance_members VALUES (?,?,?,?)',
                      (TARGET_PARENT, stable_id({'feature': TARGET, 'subject': 'source-module-graph'}),
                       TARGET, 'source-module-graph'))
        state.execute('INSERT OR IGNORE INTO parent_acceptance_members SELECT ?,id,feature,subject FROM checks WHERE feature=?',
                      (TARGET_PARENT, TARGET))


def pending_children(state):
    return state.execute("""SELECT k.* FROM checks k JOIN parent_acceptance_members m ON m.id=k.id
        WHERE m.parent IN (?,?) AND k.status='pending' ORDER BY k.feature,k.subject LIMIT 1""",
                         (FEATURE, TARGET_PARENT)).fetchone()


def target_proof(state, bindings):
    root = Path(bindings['project_root'])
    # Compare the full inventory row by row. A Java-only or truncated inventory
    # cannot establish the absence of relevant build/framework descriptors.
    count = 0
    for live in mobile_contracts.inventory_rows(root):
        saved = state.execute('SELECT * FROM file_inventory WHERE path=?', (live[0],)).fetchone()
        if saved is None or tuple(saved) != live:
            raise AcceptancePending('Java dependency target inventory is stale or incomplete')
        count += 1
    if not count or count != state.execute('SELECT count(*) FROM file_inventory').fetchone()[0]:
        raise AcceptancePending('Java dependency target inventory population changed')
    try:
        modules, edges, descriptors = module_contracts.graph(state, root)
    except ToolError as error:
        raise AcceptancePending('target Java dependency applicability/ownership unresolved: ' + str(error)) from error
    if edges:
        raise AcceptancePending('nonempty target graph needs executed independent dependency usage evidence')
    status = state.execute('SELECT status FROM coverage WHERE feature=?', (TARGET,)).fetchone()
    if status is None or status[0] != 'implemented':
        raise AcceptancePending('target Java dependency contract has no executed implementation')
    digest, count = hashlib.sha256(), 0
    for row in state.execute('''SELECT m.id,m.subject,k.subject AS actual_subject,k.feature,k.status,k.verdict,
            k.completed_at,k.error,
            CASE WHEN length(CAST(k.expected_json AS BLOB))<=? THEN k.expected_json END AS expected_json,
            CASE WHEN length(CAST(k.actual_json AS BLOB))<=? THEN k.actual_json END AS actual_json,
            CASE WHEN length(k.diff_json)<=1024 THEN k.diff_json END AS diff_json
        FROM parent_acceptance_members m LEFT JOIN checks k ON k.id=m.id WHERE m.parent=? ORDER BY m.id''',
        (engine.MAX_PROOF_BYTES, engine.MAX_PROOF_BYTES, TARGET_PARENT)):
        if (row['subject'], row['actual_subject'], row['feature']) != ('source-module-graph', 'source-module-graph', TARGET) \
                or (row['status'], row['verdict']) != ('complete', 'pass') \
                or row['completed_at'] is None or row['error'] is not None:
            raise AcceptancePending('retained target Java dependency check requires an executed pass')
        try:
            expected, actual, diff = (json.loads(row[k]) for k in ('expected_json', 'actual_json', 'diff_json'))
            absent = '__audit_absent_module__'
            while absent in modules:
                absent += '_'
            samples = {canonical_json([name, flags]): f"Module '{name}' " +
                       ('has no dependencies.\n' if name in modules else 'not found in index.\n')
                       for name in [absent, *sorted(modules)]
                       for flags in (('--strict',), ('--verbose',), ('--no-transitive', '--no-xml', '--no-resources'))}
            want = {'source': unused_dep_contracts.TARGET_REASON, 'descriptors': descriptors, 'samples': samples}
            if canonical_json(expected) != canonical_json(want) or actual != samples \
                    or diff != {'missing': [], 'unexpected': []}:
                raise ValueError()
        except (ValueError, TypeError):
            raise AcceptancePending('target Java dependency proof lacks current complete command samples') from None
        digest.update(canonical_json([row['id'], expected, actual]).encode())
        count += 1
    if not count:
        raise AcceptancePending('missing retained target Java dependency check')
    return {'checks': count, 'proof_sha256': digest.hexdigest()}


def readiness(state, bindings):
    result = engine.readiness(state, bindings, policy=sys.modules[__name__])
    if spec.API_DECISIONS:
        raise AcceptancePending('retained source-classpath contract needs an executable decision: ' +
                                ', '.join(item['id'] for item in spec.API_DECISIONS))
    return {**result, 'target': target_proof(state, bindings)}


def exercise(fixture):
    return engine.exercise(fixture, policy=sys.modules[__name__])


def replay_children(fixture, source):
    retain_note(fixture.state, source)
    mobile_contracts.inventory(fixture.state, fixture.root)
    unused_dep_contracts.plan_unused(fixture.state, fixture.root)
    engine.replay_children(fixture, source, policy=sys.modules[__name__])
    with fixture.state:
        for row in source.execute('SELECT id,feature,subject FROM checks WHERE feature=?', (TARGET,)):
            fixture.state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', tuple(row))
        fixture.state.executemany('INSERT OR IGNORE INTO parent_acceptance_members VALUES (?,?,?,?)',
                                  source.execute('SELECT parent,id,feature,subject FROM parent_acceptance_members WHERE parent=?',
                                                 (TARGET_PARENT,)))
    while child := pending_children(fixture.state):
        fixture.evaluate(child)
