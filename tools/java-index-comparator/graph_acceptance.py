"""Current-edition graph acceptance, retaining source and target proofs separately."""
import hashlib
import json
import sys

from common import canonical_json, stable_id
import parent_acceptance as engine
import graph_acceptance_spec as checklist
from scope_acceptance_spec import population_shape
import call_hierarchy_contracts as callers
import graph_mcp_contracts as graph
from call_tree_acceptance import target_population

LABEL = 'Java graph'
FEATURE = 'graph'
SUBJECT = 'java-graph-acceptance-v1'
ORACLE_PARENT = FEATURE + ':target-proof'
ORACLE_FEATURES = {callers.FEATURE, graph.FEATURE}
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('executed Java graph acceptance: finite advertised build/status, incoming/outgoing '
          'edges, impact/path/cycles/metrics/top, confidence/caps/freshness/selectors and '
          'Java declaration/receiver/overload identities from source/javac/CLI fixtures; '
          'request-bound target MCP direct-owner projection retained separately; '
          'not compiler equivalence or MCP equivalence for oracle-less graph options; '
          'foreign parsers and XML-only syntax explicitly out-of-scope')


def specification():
    return {'parent': FEATURE, 'cli_surface_sha256': checklist.REVIEWED_SURFACE,
            'criteria': checklist.criteria(),
            'target': {'features': sorted(ORACLE_FEATURES),
                       'input': 'every independently JDK-planned callable name and exact target declaration anchor',
                       'expected': 'complete MCP direct-owner identities and metadata, graph projection excludes internal subjects only',
                       'stop': 'every retained ID executed/pass with complete captured request dependencies'},
            'boundaries': 'all advertised Java graph CLI options and kinds; conservative ambiguous/unresolved edges remain explicit; no inferred compiler/JDK binary-loader requirement; XML-only syntax and foreign resolution out-of-scope'}


def required_acceptance_features():
    from audit import required_features
    import selector_error_contracts
    import freshness_error_contracts
    return {f for f in required_features() if (f.startswith('graph:') and f != graph.FEATURE)
            or 'java-graph' in f} | selector_error_contracts.FEATURES | freshness_error_contracts.FEATURES


def unmapped_commands():
    return [] if checklist.cli_surface() == checklist.REVIEWED_SURFACE else ['changed Java graph API']


def validate_specification(spec):
    engine.validate_specification(spec, policy=sys.modules[__name__])
    if unmapped_commands():
        raise AcceptancePending('Java graph commands/options changed without checklist review')


def assertion_keys(samples, criterion):
    return sorted(samples)


def validate_population(samples, criterion):
    if population_shape(samples, criterion['feature']) != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained Java graph assertion population: ' + criterion['feature'])


def criterion_for_proof(row, criterion):
    if row['subject'] != criterion['subject']:
        raise AcceptancePending('unreviewed retained Java graph subject: ' + row['feature'])
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
        for feature in sorted(ORACLE_FEATURES):
            state.execute('INSERT OR IGNORE INTO parent_acceptance_families VALUES (?,?)', (ORACLE_PARENT, feature))
            state.execute('INSERT OR IGNORE INTO parent_acceptance_members SELECT ?,id,feature,subject FROM checks WHERE feature=?',
                          (ORACLE_PARENT, feature))


def pending_children(state):
    # The direct target MCP dependency must execute before its graph projection.
    return state.execute("""SELECT k.* FROM checks k JOIN parent_acceptance_members m ON m.id=k.id
        WHERE m.parent IN (?,?) AND k.status='pending' ORDER BY k.feature,k.subject LIMIT 1""",
                         (FEATURE, ORACLE_PARENT)).fetchone()


def proof(row):
    if (row['actual_feature'], row['actual_subject']) != (row['feature'], row['subject']) \
            or (row['status'], row['verdict'], row['coverage_status']) != ('complete', 'pass', 'implemented') \
            or row['completed_at'] is None or row['error'] is not None:
        raise AcceptancePending('retained target Java graph proof requires an executed pass')
    try:
        expected, actual, diff = (json.loads(row[key]) for key in ('expected_json', 'actual_json', 'diff_json'))
        if not isinstance(expected, dict) or not isinstance(expected.get('items'), list) \
                or diff != {'missing': [], 'unexpected': []} \
                or canonical_json(actual) != canonical_json({'items': expected['items']}):
            raise ValueError()
        if not str(expected.get('source', '')).startswith('live MCP'):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AcceptancePending('target Java graph owner proof is missing/incomplete') from None
    return expected


def readiness(state, bindings):
    result = engine.readiness(state, bindings, policy=sys.modules[__name__])
    anchors = target_population(state)
    for feature in ORACLE_FEATURES:
        for row in state.execute('SELECT DISTINCT name FROM call_hierarchy_anchors ORDER BY name'):
            identity = stable_id({'feature': feature, 'subject': row[0]})
            if not state.execute('SELECT 1 FROM parent_acceptance_members WHERE parent=? AND id=? AND feature=? AND subject=?',
                                 (ORACLE_PARENT, identity, feature, row[0])).fetchone():
                raise AcceptancePending('missing target Java graph acceptance identity')
    if state.execute("""SELECT 1 FROM checks k WHERE k.feature IN (?,?)
            AND NOT EXISTS(SELECT 1 FROM parent_acceptance_members m WHERE m.parent=? AND m.id=k.id) LIMIT 1""",
                     (*sorted(ORACLE_FEATURES), ORACLE_PARENT)).fetchone():
        raise AcceptancePending('target Java graph ledger is incomplete')
    digest, count = hashlib.sha256(), 0
    for row in state.execute("""SELECT m.id,m.feature,m.subject,k.status,k.verdict,k.completed_at,k.error,
            k.feature AS actual_feature,k.subject AS actual_subject,
            CASE WHEN length(CAST(k.expected_json AS BLOB))<=? THEN k.expected_json END AS expected_json,
            CASE WHEN length(CAST(k.actual_json AS BLOB))<=? THEN k.actual_json END AS actual_json,
            CASE WHEN length(k.diff_json)<=1024 THEN k.diff_json END AS diff_json,
            c.status AS coverage_status FROM parent_acceptance_members m LEFT JOIN checks k ON m.id=k.id
            LEFT JOIN coverage c ON c.feature=m.feature WHERE m.parent=? ORDER BY m.feature,m.subject""",
             (engine.MAX_PROOF_BYTES, engine.MAX_PROOF_BYTES, ORACLE_PARENT)):
        if row['feature'] not in ORACLE_FEATURES:
            raise AcceptancePending('unmapped retained target Java graph feature')
        expected = proof(row)
        if row['feature'] == callers.FEATURE:
            declarations = state.execute('SELECT count(*) FROM call_hierarchy_anchors WHERE name=?', (row['subject'],)).fetchone()[0]
            queries = state.execute('SELECT count(*) FROM oracle_pages WHERE check_id=?', (row['id'],)).fetchone()[0]
            if not declarations or expected.get('declarations') != declarations or not queries or expected.get('queries') != queries:
                raise AcceptancePending('target Java graph declaration/request metadata is incomplete')
        else:
            dependency = state.execute('''SELECT id,
                CASE WHEN length(CAST(expected_json AS BLOB))<=? THEN expected_json END AS expected_json
                FROM checks WHERE feature=? AND subject=?''',
                (engine.MAX_PROOF_BYTES, callers.FEATURE, row['subject'])).fetchone()
            try:
                source = json.loads(dependency['expected_json'])
                owners = sorted({tuple(item) for item in source['items']})
                seeds = sorted({tuple(item) for item in state.execute(
                    'SELECT path,line,name FROM call_hierarchy_anchors WHERE name=?', (row['subject'],))})
                projected = sorted(set(owners) - set(seeds))
                if expected.get('source_case_id') != dependency['id'] or canonical_json(expected.get('oracle_owners')) != canonical_json(owners) \
                        or canonical_json(expected.get('selected_seeds')) != canonical_json(seeds) \
                        or canonical_json(expected['items']) != canonical_json(projected):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise AcceptancePending('target Java graph source/seeds/projection metadata changed') from None
        digest.update((canonical_json([row['id'], stable_id(expected), row['completed_at']]) + '\n').encode())
        count += 1
    return {**result, 'target_anchors': anchors, 'target_executed_checks': count,
            'target_proof_sha256': digest.hexdigest()}


def exercise(fixture):
    return engine.exercise(fixture, policy=sys.modules[__name__])


def replay_children(fixture, source):
    from replay import StoredOracle
    from common import source_snapshot
    import mobile_contracts
    retain_note(fixture.state, source)
    _, files = source_snapshot(fixture.root)
    mobile_contracts.inventory(fixture.state, fixture.root)
    callers.plan_methods(fixture.state, fixture.root, files, fixture.structure)
    graph.plan(fixture.state)
    with fixture.state:
        fixture.state.execute("INSERT OR REPLACE INTO metadata VALUES ('java_files',?)", (str(len(files)),))
        text_mode = source.execute("SELECT value FROM metadata WHERE key='text_mode'").fetchone()
        fixture.state.execute("INSERT OR IGNORE INTO metadata VALUES ('text_mode',?)", (text_mode[0] if text_mode else 'batch',))
        for criterion in checklist.criteria():
            f, subject = criterion['feature'], criterion['subject']
            fixture.state.execute("INSERT OR IGNORE INTO coverage VALUES (?,'implemented',?)", (f, REASON))
            fixture.state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                  (stable_id({'feature': f, 'subject': subject}), f, subject))
        for f in required_acceptance_features() | ORACLE_FEATURES:
            for row in source.execute('SELECT id,feature,subject FROM checks WHERE feature=?', (f,)):
                fixture.state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', tuple(row))
        for ledger in (FEATURE, ORACLE_PARENT):
            fixture.state.executemany('INSERT OR IGNORE INTO parent_acceptance_members VALUES (?,?,?,?)',
                source.execute('SELECT parent,id,feature,subject FROM parent_acceptance_members WHERE parent=?', (ledger,)))
    plan(fixture.state, java_only=True)
    saved = fixture.client
    try:
        while child := pending_children(fixture.state):
            recorded = source.execute('SELECT 1 FROM checks WHERE id=?', (child['id'],)).fetchone()
            if child['feature'] in ORACLE_FEATURES and recorded is None:
                raise AcceptancePending('required target graph callable has no captured oracle check')
            oracle = StoredOracle(source, child['id']) if recorded else None
            fixture.client = oracle or saved
            fixture.evaluate(child)
            if oracle:
                oracle.assert_consumed()
    finally:
        fixture.client = saved
