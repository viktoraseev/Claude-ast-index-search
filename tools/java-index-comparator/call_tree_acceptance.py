"""Finite Java call-tree acceptance with separate source and MCP proof ledgers.

A current-edition source fixture does not establish target MCP equivalence.
Every position-bound target check remains required, including retained errors.
"""
import hashlib
import json
import sys

from common import canonical_json, stable_id
import parent_acceptance as engine
from scope_acceptance_spec import population_shape
import call_hierarchy_contracts as callers
import call_tree_mcp_contracts as trees

LABEL = 'Java call-tree'
FEATURE = 'call-tree:semantic-resolution'
SUBJECT = 'java-call-tree-acceptance-v1'
ORACLE_PARENT = FEATURE + ':target-proof'
ORACLE_FEATURES = {callers.FEATURE, trees.FEATURE}
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('executed acceptance composition: Java callable identity and nominal/generic '
          'receiver/overload contexts, depth/caps/cycles, root/path selectors and '
          'fresh versus lexical fallback; request-bound target direct and two-level '
          'MCP proofs are retained separately; not compiler equivalence or MCP '
          'equivalence for oracle-less options/attached roots; XML-only and foreign '
          'parsers explicitly out-of-scope')

# Fixed independently authored populations; never selected by current verdict.
RETAINED = (
    ('call-tree', 'disposable-java-context', 18, '82f95a1d8f9dca102e45377785c551402ffebd579b1cca2b32b4e4357478ed95', 'ce2543cf1bfae732a0ed5f2741d3863fb452491a5fc383dfbd1d53988ab17444', 'context_contracts', 'bare/qualified names, literal/prose/method references, unindexed/indexed fallback, depth/caps and recursive ownership'),
    ('global:scope:java-call-tree', 'disposable-java-caller-scope', 656, 'e09dc695efb82aabbb52c7c0296ca0ad8f2ff9c40f29dae17042fa991a697f4c', '038fc3c573a1414602b12f7469f4e1cd044eaa565faa212f2932a2ad5bbce807', 'caller_scope_contracts', 'colliding Java roots, local/subtree/cwd/file selector intersections, path wildcards treated literally, scope before all zero/one/full caps, unbuilt/fresh graphs'),
    ('global:format:java-call-tree', 'disposable-java-caller-formats', 118, 'f8c7633a9554cb5ecdf6834dfb8d3d5a56a9113ca74ff2e0f88df6801e71d246', '0377b16b1ebc238f88a3b1552b366c4dad01a094773aeb067c0e7a8a5e0a1a03', 'caller_format_contracts', 'JSON/text exact trees, metadata, cycles/repeated expansion, noncallable terminals, owner/overload/declaration-site identities and attached root rendering'),
    ('call-tree:java-pattern-flow-scopes', 'disposable-java-pattern-flow-scopes', 18, 'aa7d3522a7b90ae425996dbacdba0780f2735e8ac2626ea862b5ce63e6a21c8c', '4f588992cfa3fb327946d48f6a0fb187d5a86a169fac92a5ba676bec25ed90db', 'java_pattern_scope_contracts', 'while/ternary/else pattern receivers versus String shadow guards; exact invocation owners through fresh graph'),
    ('call-tree:java-callable-sites', 'disposable-java-callable-sites-v1', 50, 'af62024888de922be62d9504ecdd7f679ad53636f7fe14d075c4406b4372171f', 'b2962e7008cc91bab276b72d8ab8f9c2586f1cd8d2cb02e9a0b3c7d7da4c5219', 'java_callable_site_contracts', 'bare/qualified/reference/variable-arity/constructor/recursive same-line overloads in both declaration orders; exact depth-three branches and zero/one/full pages'),
    ('call-tree:java-overload-contexts', 'disposable-java-overload-contexts-v1', 11, 'c3cd7e3916803fc60adfdbea245c8c652d491bc988d2a4afcb09b7059cdbfe6a', '7d76064176e77a34bdb004e12e081e0084c16d66ee3f763c3fcd3c388cc86d90', 'java_overload_context_contracts', 'source and unindexed nominal argument identities, List/Stream/future callbacks/references, Optional/getter/lambda overloads, generic receiver parameters, Collection wildcards and source factory precedence'),
)


def cli_surface():
    from pathlib import Path
    import re
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    cli = source.split('struct Cli {', 1)[1].split('\n}', 1)[0]
    command = source.split('    CallTree {', 1)[1].split('\n    },', 1)[0]
    return stable_id([re.sub(r'\s+', '', re.sub(r'^\s*//[^\n]*', '', body, flags=re.MULTILINE))
                      for body in (cli, command)])


REVIEWED_SURFACE = 'b016a618d5d9cb3870f94ba1b645872c34fe561501445d2fd008551521cba009'


def specification():
    return {'parent': FEATURE, 'cli_surface_sha256': REVIEWED_SURFACE,
            'criteria': [dict(feature=f, subject=s, samples_count=n, sample_keys_sha256=k,
                              population_sha256=p, fixture=m + '.exercise',
                              production='src/commands/grep.rs and src/commands/graph/{java,resolve,mod}.rs',
                              contract=c) for f, s, n, k, p, m, c in RETAINED],
            'target': {'features': sorted(ORACLE_FEATURES), 'input': 'every JDK-planned Java callable name and exact source anchors in this target edition',
                       'expected': 'complete request-bound MCP direct-owner and two-level branch/cycle expectations; metadata and queries retained',
                       'stop': 'every retained ID executed and passed, every independently planned name has both checks and complete captured oracle dependencies'},
            'boundaries': 'full advertised call-tree name/depth/limit/in-file/global selectors and rendering on Java; foreign-language resolution and XML-only syntax out-of-scope; no external/JDK binary loader or compiler-equivalence is inferred'}


def required_acceptance_features():
    from audit import required_features
    reviewed = {row[0] for row in RETAINED}
    # A new call-tree sub-contract must be reviewed, not omitted by the gate.
    return reviewed | {f for f in required_features() if f.startswith('call-tree:')
                       and f != FEATURE and f not in ORACLE_FEATURES}


def unmapped_commands():
    return [] if cli_surface() == REVIEWED_SURFACE else ['changed Java call-tree API']


def validate_specification(checklist):
    engine.validate_specification(checklist, policy=sys.modules[__name__])
    if unmapped_commands():
        raise AcceptancePending('Java call-tree command/options changed without checklist review')


def assertion_keys(samples, criterion):
    return sorted(samples)


def validate_population(samples, criterion):
    if population_shape(samples, criterion['feature']) != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained Java call-tree assertion population: ' + criterion['feature'])


def criterion_for_proof(row, criterion):
    if row['subject'] != criterion['subject']:
        raise AcceptancePending('unreviewed retained Java call-tree subject: ' + row['feature'])
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
    # MCP direct proofs precede their two-level dependencies; both remain live.
    return state.execute("""SELECT k.* FROM checks k JOIN parent_acceptance_members m ON m.id=k.id
        WHERE m.parent IN (?,?) AND k.status='pending' ORDER BY k.feature,k.subject LIMIT 1""",
                         (FEATURE, ORACLE_PARENT)).fetchone()


def target_population(state):
    # Independent source structure proves the complete anchor population, even
    # when an accidentally deleted anchor/check would otherwise shrink it.
    expected = hashlib.sha256()
    count, files = 0, 0
    for row in state.execute('SELECT path,CASE WHEN length(CAST(entries_json AS BLOB))<=? THEN entries_json END AS entries_json FROM source_structures ORDER BY path',
                             (engine.MAX_PROOF_BYTES,)):
        try:
            entries = json.loads(row['entries_json'])
            anchors = sorted({(e['name'], row['path'], e['line'], e['column'], e['kind'])
                              for e in entries if e['kind'] in callers.CALLABLE_KINDS})
        except (ValueError, TypeError, KeyError):
            raise AcceptancePending('invalid/oversized Java callable source inventory') from None
        for anchor in anchors:
            expected.update((canonical_json(anchor) + '\n').encode())
            count += 1
        files += 1
    actual = hashlib.sha256()
    actual_count = 0
    for row in state.execute('SELECT name,path,line,column,kind FROM call_hierarchy_anchors ORDER BY path,name,line,column,kind'):
        actual.update((canonical_json(tuple(row)) + '\n').encode())
        actual_count += 1
    inventory_files = state.execute("SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0]
    java_files = state.execute("SELECT value FROM metadata WHERE key='java_files'").fetchone()
    if java_files is None or not files or files != int(java_files[0]) or files > inventory_files \
            or expected.digest() != actual.digest() or count != actual_count:
        raise AcceptancePending('Java call-tree target callable inventory is missing/incomplete')
    return count


def readiness(state, bindings):
    result = engine.readiness(state, bindings, policy=sys.modules[__name__])
    anchors = target_population(state)
    for feature in ORACLE_FEATURES:
        for row in state.execute('SELECT DISTINCT name FROM call_hierarchy_anchors ORDER BY name'):
            subject = row[0]
            identity = stable_id({'feature': feature, 'subject': subject})
            if not state.execute('SELECT 1 FROM parent_acceptance_members WHERE parent=? AND id=? AND feature=? AND subject=?',
                                 (ORACLE_PARENT, identity, feature, subject)).fetchone():
                raise AcceptancePending('missing target Java call-tree acceptance identity')
    if state.execute("""SELECT 1 FROM checks k WHERE k.feature IN (?,?)
            AND NOT EXISTS(SELECT 1 FROM parent_acceptance_members m WHERE m.parent=? AND m.id=k.id) LIMIT 1""",
                     (*sorted(ORACLE_FEATURES), ORACLE_PARENT)).fetchone():
        raise AcceptancePending('target Java call-tree check ledger is incomplete')
    count = 0
    digest = hashlib.sha256()
    for row in state.execute("""SELECT m.id,m.feature,m.subject,k.status,k.verdict,k.completed_at,k.error,
            k.feature AS actual_feature,k.subject AS actual_subject,
            CASE WHEN length(CAST(k.expected_json AS BLOB))<=? THEN k.expected_json END AS expected_json,
            CASE WHEN length(CAST(k.actual_json AS BLOB))<=? THEN k.actual_json END AS actual_json,
            CASE WHEN length(k.diff_json)<=1024 THEN k.diff_json END AS diff_json,
            c.status AS coverage_status FROM parent_acceptance_members m LEFT JOIN checks k ON m.id=k.id
            LEFT JOIN coverage c ON c.feature=m.feature WHERE m.parent=? ORDER BY m.feature,m.subject""",
             (engine.MAX_PROOF_BYTES, engine.MAX_PROOF_BYTES, ORACLE_PARENT)):
        if row['feature'] not in ORACLE_FEATURES or (row['actual_feature'], row['actual_subject']) != (row['feature'], row['subject']) \
                or (row['status'], row['verdict'], row['coverage_status']) != ('complete', 'pass', 'implemented') \
                or row['completed_at'] is None or row['error'] is not None:
            raise AcceptancePending('retained target Java call-tree proof requires an executed pass')
        try:
            expected, actual, diff = (json.loads(row[key]) for key in ('expected_json', 'actual_json', 'diff_json'))
            if not isinstance(expected, dict) or not str(expected.get('source', '')).startswith('live MCP') \
                    or not isinstance(expected.get('items'), list):
                raise ValueError()
            want = {'items': expected['items']}
            if row['feature'] == callers.FEATURE:
                declarations = state.execute('SELECT count(*) FROM call_hierarchy_anchors WHERE name=?', (row['subject'],)).fetchone()[0]
                queries = state.execute('SELECT count(*) FROM oracle_pages WHERE check_id=?', (row['id'],)).fetchone()[0]
                if not declarations or expected.get('declarations') != declarations or not queries or expected.get('queries') != queries:
                    raise ValueError()
            else:
                if not isinstance(expected.get('branches'), list) or not isinstance(expected.get('dependencies'), list) \
                        or row['subject'] not in expected['dependencies']:
                    raise ValueError()
                want['branches'] = expected['branches']
            if canonical_json(want) != canonical_json(actual) or diff != {'missing': [], 'unexpected': []}:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AcceptancePending('target Java call-tree metadata/items/branches or oracle dependencies are incomplete') from None
        digest.update((canonical_json([row['id'], stable_id(expected), stable_id(actual), row['completed_at']]) + '\n').encode())
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
    trees.plan(fixture.state)
    with fixture.state:
        fixture.state.execute("INSERT OR REPLACE INTO metadata VALUES ('java_files',?)", (str(len(files)),))
        text_mode = source.execute("SELECT value FROM metadata WHERE key='text_mode'").fetchone()
        fixture.state.execute("INSERT OR IGNORE INTO metadata VALUES ('text_mode',?)", (text_mode[0] if text_mode else 'batch',))
        for f, subject, *_ in RETAINED:
            fixture.state.execute("INSERT OR IGNORE INTO coverage VALUES (?,'implemented',?)", (f, REASON))
            fixture.state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                  (stable_id({'feature': f, 'subject': subject}), f, subject))
            for row in source.execute('SELECT id,feature,subject FROM checks WHERE feature=?', (f,)):
                fixture.state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', tuple(row))
        # Replanning today's anchors cannot erase a historical unsupported
        # Java oracle obligation whose name is no longer independently mapped.
        for feature in sorted(ORACLE_FEATURES):
            for row in source.execute('SELECT id,feature,subject FROM checks WHERE feature=?', (feature,)):
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
                raise AcceptancePending('required target callable has no recorded oracle check')
            oracle = StoredOracle(source, child['id']) if recorded else None
            fixture.client = oracle or saved
            fixture.evaluate(child)
            if oracle:
                oracle.assert_consumed()
    finally:
        fixture.client = saved
