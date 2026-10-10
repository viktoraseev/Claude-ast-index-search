"""Executed Java resource acceptance; target presence can never become absence."""
import json
import sys

from common import ToolError, stable_id
import parent_acceptance as engine
import resource_acceptance_spec as spec
import android_contracts

LABEL = 'Java resource'
FEATURE = 'android:syntax-resolution'
SUBJECT = 'java-resource-acceptance-v1'
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('internal CLI acceptance composition: finite executed Java R binding, '
          'definition, namespace metadata, dependency and root ownership criteria; '
          'independent source/javac/CLI, not MCP equivalence; target absence proven '
          'separately by full inventory and executed empty commands; dynamic build '
          'values remain unresolved metadata and XML-only syntax/foreign parsers '
          'are explicitly out-of-scope')


def specification():
    return {'parent': FEATURE, 'criteria': spec.criteria(),
            'cli_surface_sha256': spec.REVIEWED_SURFACE_SHA256,
            'scope': 'full Java/shared resource API; XML-only syntax and foreign-only features out-of-scope',
            'target': 'full file inventory and framework markers; absence needs executed command proof; applicable target ownership remains pending',
            'boundaries': ['src/main.rs ResourceUsages/XmlUsages and resource-related UnusedDeps options',
                           'README.md resource-usages/xml-usages/unused-deps',
                           'src/indexer.rs Android resource directories and values-to-Java R identities',
                           'src/indexer/java_resources.rs bounded source-proven namespace metadata; no build execution',
                           'Unknown dynamic namespace values stay unresolved; no compiler equivalence or external binary loader is inferred']}


def required_acceptance_features():
    from audit import required_features, JAVA_EXCLUDED_FEATURES
    return {f for f in required_features() - JAVA_EXCLUDED_FEATURES
            if f == 'resource-usages' or f.startswith('resource-usages:') and f != 'resource-usages:target'} | {
                'xml-usages', 'unused-deps:java-android-ownership', 'global:scope:java-resources'}


def unmapped_commands():
    return [] if spec.cli_surface() == spec.REVIEWED_SURFACE_SHA256 else ['changed Java resource API']


def validate_specification(checklist):
    engine.validate_specification(checklist, policy=sys.modules[__name__])
    if spec.cli_surface() != checklist.get('cli_surface_sha256'):
        raise AcceptancePending('Java resource command/options changed without acceptance review')
    if any(not item.get('population_sha256') for item in checklist['criteria']):
        raise AcceptancePending('Java resource criterion lacks nested assertion population')


def criterion_for_proof(row, criterion):
    if row['subject'] == criterion['subject']:
        return criterion
    if row['feature'] in android_contracts.FEATURES and row['subject'] == 'target-absence':
        return {**criterion, 'samples_count': 1,
                'sample_keys_sha256': stable_id(['absence']),
                'population_sha256': spec.population_shape({'absence': True}, row['feature'])}
    raise AcceptancePending('retained Java resource subject has no reviewed executable criterion: ' + row['feature'])


def assertion_keys(samples, criterion):
    return sorted(samples)


def validate_population(samples, criterion):
    if spec.population_shape(samples, criterion['feature']) != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained Java resource assertion population: ' + criterion['feature'])


def retain_note(state, source):
    row = source.execute('SELECT reason FROM coverage WHERE feature=?', (FEATURE,)).fetchone()
    if row is not None:
        with state:
            state.execute('INSERT OR IGNORE INTO parent_acceptance_notes VALUES (?,?,?)',
                          (FEATURE, stable_id(row[0]), row[0]))


def plan(state, *, java_only):
    if java_only:
        retain_note(state, state)
    return engine.plan(state, java_only=java_only, policy=sys.modules[__name__])


def pending_children(state):
    return engine.pending_children(state, policy=sys.modules[__name__])


def readiness(state, bindings):
    result = engine.readiness(state, bindings, policy=sys.modules[__name__])
    for feature in sorted(android_contracts.FEATURES):
        row = state.execute('SELECT status FROM coverage WHERE feature=?', (feature + ':target',)).fetchone()
        if row is None or row[0] != 'inapplicable':
            raise AcceptancePending('applicable/unproved target Java resource ownership needs executed target evidence: ' + feature)
        proof = state.execute("SELECT expected_json,actual_json FROM checks WHERE feature=? AND subject='target-absence' AND status='complete' AND verdict='pass'",
                              (feature,)).fetchone()
        try:
            valid = proof is not None and json.loads(proof[0]).get('samples') == {'absence': True} \
                    and json.loads(proof[1]) == {'absence': True}
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise AcceptancePending('target Java resource absence has no executed proof: ' + feature)
    try:
        android_contracts.verify_absence_evidence(state)
    except ToolError as error:
        raise AcceptancePending('target Java resource absence inventory is invalid') from error
    return result


def exercise(fixture):
    return engine.exercise(fixture, policy=sys.modules[__name__])


def replay_children(fixture, source):
    import mobile_contracts
    retain_note(fixture.state, source)
    # Re-read the exact target inventory; never copy an absence verdict or
    # source marker proof from an older edition into the new acceptance.
    mobile_contracts.inventory(fixture.state, fixture.root)
    android_contracts.plan_android(fixture.state, fixture.root)
    return engine.replay_children(fixture, source, policy=sys.modules[__name__])
