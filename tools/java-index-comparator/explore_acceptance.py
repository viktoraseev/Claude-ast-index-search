"""Execute exploration acceptance without converting unresolved Java criteria to passes."""
import sys

from common import stable_id
import parent_acceptance as engine
import explore_acceptance_spec as checklist
from scope_acceptance_spec import population_shape

LABEL = 'Java exploration'
FEATURE = 'explore:semantic-resolution'
SUBJECT = 'java-exploration-acceptance-v1'
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('executed Java exploration source/javac/CLI acceptance: finite query, ranking, '
          'RWR receiver/declaration/result ownership, root/directory selectors and rendering '
          'checklist; no equivalent MCP exploration API, not MCP or compiler equivalence; '
          'XML-only syntax and foreign resolution explicitly out-of-scope')


def specification():
    return {'parent': FEATURE, 'cli_surface_sha256': checklist.REVIEWED_SURFACE,
            'criteria': checklist.criteria(),
            'scope': 'all advertised Java explore options and shared global selectors; '
                     'retained uncertain inference/result/overload behaviours require executed proof'}


def required_acceptance_features():
    from audit import required_features
    return {feature for feature in required_features()
            if (feature == 'explore' or feature.startswith('explore:')) and feature != FEATURE} | {
                'global:format:java-exploration', 'global:format:java-intent-fallback',
                'global:scope:java-explore'}


def unmapped_commands():
    return [] if checklist.cli_surface() == checklist.REVIEWED_SURFACE else ['changed Java explore API']


def validate_specification(spec):
    engine.validate_specification(spec, policy=sys.modules[__name__])
    if unmapped_commands():
        raise AcceptancePending('Java exploration commands/options changed without checklist review')


def assertion_keys(samples, criterion):
    return sorted(samples)


def validate_population(samples, criterion):
    if population_shape(samples, criterion['feature']) != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained Java exploration assertion branches: ' + criterion['feature'])


def criterion_for_proof(row, criterion):
    if row['subject'] != criterion['subject']:
        raise AcceptancePending('unreviewed retained Java exploration subject: ' + row['feature'])
    return criterion


def retain_note(state, source):
    row = source.execute('SELECT reason FROM coverage WHERE feature=?', (FEATURE,)).fetchone()
    if row:
        with state:
            state.execute('INSERT OR IGNORE INTO parent_acceptance_notes VALUES (?,?,?)',
                          (FEATURE, stable_id(row[0]), row[0]))


def plan(state, *, java_only):
    if java_only:
        retain_note(state, state)
        engine.plan(state, java_only=True, policy=sys.modules[__name__])


def pending_children(state):
    return engine.pending_children(state, policy=sys.modules[__name__])


def readiness(state, bindings):
    return engine.readiness(state, bindings, policy=sys.modules[__name__])


def exercise(fixture):
    return engine.exercise(fixture, policy=sys.modules[__name__])


def replay_children(fixture, source):
    retain_note(fixture.state, source)
    return engine.replay_children(fixture, source, policy=sys.modules[__name__])
