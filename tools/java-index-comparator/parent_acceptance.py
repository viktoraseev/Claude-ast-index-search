"""Execute finite parent acceptance over current, independently executed children.

The durable identity ledger is append-only. It includes pending and failed
children, never just passing rows. Proofs are streamed one child at a time;
parent results contain bounded counts/digests, not repeated ID or payload lists.
"""
import hashlib
import json

from common import ToolError, canonical_json, stable_id
from format_acceptance_spec import criteria

FEATURE = 'global:format'
FEATURES = {FEATURE}
SUBJECT = 'java-format-acceptance-v1'
REASON = ('internal CLI acceptance composition: finite Java command formats and '
          'error/recovery envelopes from executed source/state fixtures; not MCP equivalence')
BINDINGS = ('project_root', 'snapshot_sha256', 'inventory_sha256', 'binary_sha256',
            'fixture_sha256', 'text_mode', 'audit_scope')
MAX_PROOF_BYTES = 8 * 1024 * 1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS parent_acceptance_editions(
 parent TEXT PRIMARY KEY, bindings_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parent_acceptance_members(
 parent TEXT NOT NULL, id TEXT NOT NULL, feature TEXT NOT NULL, subject TEXT NOT NULL,
 PRIMARY KEY(parent,id)
);
CREATE TABLE IF NOT EXISTS parent_acceptance_families(
 parent TEXT NOT NULL,feature TEXT NOT NULL,PRIMARY KEY(parent,feature)
);
CREATE INDEX IF NOT EXISTS parent_acceptance_features ON parent_acceptance_members(feature);
DROP TRIGGER IF EXISTS parent_acceptance_new_children;
CREATE TRIGGER parent_acceptance_new_children AFTER INSERT ON checks
WHEN EXISTS(SELECT 1 FROM parent_acceptance_families WHERE feature=NEW.feature)
BEGIN
 INSERT OR IGNORE INTO parent_acceptance_members(parent,id,feature,subject)
 SELECT parent,NEW.id,NEW.feature,NEW.subject FROM parent_acceptance_families
 WHERE feature=NEW.feature;
END;
'''


class AcceptancePending(ToolError):
    """A concrete acceptance obligation has no current executed proof."""


def specification():
    return {'parent': FEATURE, 'criteria': criteria(),
            'scope': 'Java/shared-on-Java; XML-only syntax and other languages out-of-scope'}


def required_format_features():
    from audit import JAVA_EXCLUDED_FEATURES, required_features
    return {feature for feature in required_features() - JAVA_EXCLUDED_FEATURES
            if feature.startswith('global:format:')} | {
                'graph:metrics-rendering', 'graph:traversal-rendering', 'module-route:rendering'}


def validate_specification(spec):
    items = spec.get('criteria') if isinstance(spec, dict) else None
    if not isinstance(items, list) or not items:
        raise AcceptancePending('empty format acceptance checklist')
    features = []
    for item in items:
        if not isinstance(item, dict) or any(not item.get(key) for key in (
                'feature', 'subject', 'fixture', 'production', 'contract', 'sample_keys_sha256')) \
                or type(item.get('samples_count')) is not int or item['samples_count'] < 1:
            raise AcceptancePending('unmapped format acceptance criterion')
        features.append(item['feature'])
    if len(set(features)) != len(features) or set(features) != required_format_features():
        raise AcceptancePending('format checklist omits or duplicates required API criteria')


def plan(state, *, java_only):
    if not java_only:
        return
    spec = specification()
    validate_specification(spec)
    metadata = dict(state.execute('SELECT key,value FROM metadata'))
    bindings = {key: metadata.get(key) for key in BINDINGS}
    with state:
        # Never rewrite an edition to make historical proof look current.
        state.execute('INSERT OR IGNORE INTO parent_acceptance_editions VALUES (?,?,?)',
                      (FEATURE, canonical_json(bindings), stable_id(spec)))
        for criterion in spec['criteria']:
            feature, subject = criterion['feature'], criterion['subject']
            identity = stable_id({'feature': feature, 'subject': subject})
            state.execute('INSERT OR IGNORE INTO parent_acceptance_families VALUES (?,?)', (FEATURE, feature))
            state.execute('INSERT OR IGNORE INTO parent_acceptance_members VALUES (?,?,?,?)',
                          (FEATURE, identity, feature, subject))
            state.execute('''INSERT OR IGNORE INTO parent_acceptance_members
                SELECT ?,id,feature,subject FROM checks WHERE feature=?''', (FEATURE, feature))
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))
        state.execute("UPDATE coverage SET status='pending' WHERE feature=?", (FEATURE,))
        # Replanning must revalidate composition even when a child was deleted,
        # added or changed after a previously successful acceptance.
        state.execute("UPDATE checks SET status='pending',verdict=NULL WHERE feature=?", (FEATURE,))


def pending_children(state):
    return state.execute('''SELECT k.* FROM checks k JOIN parent_acceptance_members m ON m.id=k.id
        WHERE m.parent=? AND k.status='pending'
        ORDER BY k.feature,k.subject LIMIT 1''', (FEATURE,)).fetchone()


def readiness(state, bindings):
    """Read-only, bounded acceptance; identities and assertion sets are mandatory."""
    from format_acceptance_spec import unmapped_commands
    if unmapped_commands():
        raise AcceptancePending('new advertised Java commands have no format checklist')
    spec = specification()
    validate_specification(spec)
    for table in ('parent_acceptance_editions', 'parent_acceptance_members', 'parent_acceptance_families'):
        if state.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise AcceptancePending('missing format acceptance schema: ' + table)
    edition = state.execute('SELECT * FROM parent_acceptance_editions WHERE parent=?', (FEATURE,)).fetchone()
    if edition is None:
        raise AcceptancePending('missing format acceptance edition')
    metadata = dict(state.execute('SELECT key,value FROM metadata'))
    try:
        recorded = json.loads(edition['bindings_json'])
    except (ValueError, TypeError):
        raise AcceptancePending('invalid format acceptance edition') from None
    for key in BINDINGS:
        if not bindings.get(key) or bindings[key] != metadata.get(key) or bindings[key] != recorded.get(key):
            raise AcceptancePending('stale or missing format acceptance binding: ' + key)
    if bindings['audit_scope'] != 'java' or edition['spec_sha256'] != stable_id(spec):
        raise AcceptancePending('format acceptance specification/scope changed')
    required = {item['feature']: item for item in spec['criteria']}
    for criterion in required.values():
        identity = stable_id({'feature': criterion['feature'], 'subject': criterion['subject']})
        member = state.execute('SELECT feature,subject FROM parent_acceptance_members WHERE parent=? AND id=?',
                               (FEATURE, identity)).fetchone()
        if member is None or tuple(member) != (criterion['feature'], criterion['subject']):
            raise AcceptancePending('missing required format criterion: ' + criterion['feature'])
    if state.execute('''SELECT 1 FROM checks k WHERE k.feature IN
            (SELECT feature FROM parent_acceptance_members WHERE parent=?)
            AND NOT EXISTS(SELECT 1 FROM parent_acceptance_members m WHERE m.parent=? AND m.id=k.id) LIMIT 1''',
                     (FEATURE, FEATURE)).fetchone():
        raise AcceptancePending('format child inventory is incomplete')
    count, assertions = 0, 0
    digest = hashlib.sha256()
    # SQL length guards avoid loading arbitrary malformed/huge JSON rows.
    for row in state.execute('''SELECT m.id,m.feature,m.subject,k.feature AS actual_feature,
            k.subject AS actual_subject,k.status,k.verdict,k.completed_at,k.error,
            CASE WHEN length(CAST(k.expected_json AS BLOB))<=? THEN k.expected_json END AS expected_json,
            CASE WHEN length(CAST(k.actual_json AS BLOB))<=? THEN k.actual_json END AS actual_json,
            CASE WHEN length(k.diff_json)<=1024 THEN k.diff_json END AS diff_json,
            c.status AS coverage_status
        FROM parent_acceptance_members m LEFT JOIN checks k ON k.id=m.id
        LEFT JOIN coverage c ON c.feature=m.feature WHERE m.parent=? ORDER BY m.id''',
            (MAX_PROOF_BYTES, MAX_PROOF_BYTES, FEATURE)):
        feature = row['feature']
        if feature not in required:
            raise AcceptancePending('unmapped retained format criterion')
        if (row['actual_feature'], row['actual_subject']) != (feature, row['subject']):
            raise AcceptancePending('missing or changed format check identity: ' + feature)
        if (row['status'], row['verdict'], row['coverage_status']) != ('complete', 'pass', 'implemented') \
                or row['completed_at'] is None or row['error'] is not None:
            raise AcceptancePending('format criterion requires an executed pass: ' + feature)
        try:
            expected, actual, diff = (json.loads(row[key]) for key in ('expected_json', 'actual_json', 'diff_json'))
        except (TypeError, ValueError):
            raise AcceptancePending('missing, oversized or invalid format proof: ' + feature) from None
        samples = expected.get('samples') if isinstance(expected, dict) else None
        criterion = required[feature]
        if not isinstance(samples, dict) or not samples or not isinstance(expected.get('source'), str) \
                or len(samples) != criterion['samples_count'] \
                or stable_id(sorted(samples)) != criterion['sample_keys_sha256']:
            raise AcceptancePending('incomplete retained format assertion population: ' + feature)
        if canonical_json(actual) != canonical_json(samples) or diff != {'missing': [], 'unexpected': []}:
            raise AcceptancePending('format proof does not establish expected behaviour: ' + feature)
        digest.update(canonical_json([row['id'], feature, row['subject'], row['completed_at'],
                                      stable_id(expected), stable_id(actual)]).encode())
        digest.update(b'\n')
        count += 1
        assertions += len(samples)
    return {'criteria': len(required), 'executed_checks': count, 'assertions': assertions,
            'proof_sha256': digest.hexdigest(), 'spec_sha256': stable_id(spec),
            'bindings_sha256': stable_id(bindings), 'source': REASON}


def exercise(fixture):
    from common import adapter_digest, file_sha256
    metadata = dict(fixture.state.execute('SELECT key,value FROM metadata'))
    bindings = {key: metadata.get(key) for key in BINDINGS}
    bindings.update(binary_sha256=file_sha256(fixture.binary), fixture_sha256=adapter_digest(),
                    project_root=str(fixture.root.resolve()))
    result = readiness(fixture.state, bindings)
    # This composition is evidence about executed source/CLI contracts, never
    # new MCP equivalence and never just successful serialization of a manifest.
    return result, result, {('proved', stable_id(result))}, {('proved', stable_id(result))}


def replay_children(fixture, source):
    """Re-execute required fixtures when a parent belongs to a repair batch.

    Replay's verification database deliberately starts without historical
    child verdicts. Never copy passes into the new binary/adapter edition.
    Retain source ledger identities, including unsupported/deleted children.
    """
    state = fixture.state
    with state:
        text_mode = source.execute("SELECT value FROM metadata WHERE key='text_mode'").fetchone()
        state.execute("INSERT OR IGNORE INTO metadata VALUES ('text_mode',?)",
                      (text_mode[0] if text_mode else 'batch',))
        for criterion in criteria():
            feature, subject = criterion['feature'], criterion['subject']
            state.execute("INSERT OR IGNORE INTO coverage VALUES (?,'implemented',?)", (feature, REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
            # Include all historical identities, without trusting their verdicts.
            for row in source.execute('SELECT id,feature,subject FROM checks WHERE feature=?', (feature,)):
                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)', tuple(row))
    plan(state, java_only=True)
    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parent_acceptance_members'").fetchone():
        with state:
            state.executemany('INSERT OR IGNORE INTO parent_acceptance_members VALUES (?,?,?,?)',
                              source.execute('SELECT parent,id,feature,subject FROM parent_acceptance_members WHERE parent=?',
                                             (FEATURE,)))
    while child := pending_children(state):
        fixture.evaluate(child)
