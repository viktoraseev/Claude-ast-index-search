"""Execute the complete finite Java selector checklist, never infer it from notes."""
import sys

from common import stable_id
import parent_acceptance as engine
import scope_acceptance_spec as spec

LABEL = 'scope'
FEATURE = 'global:scope-command-matrix'
SUBJECT = 'java-scope-acceptance-v1'
BINDINGS = engine.BINDINGS
AcceptancePending = engine.AcceptancePending
REASON = ('internal CLI acceptance composition: executed complete finite Java root/'
          'directory/file/module selector checklist and retained source ownership guards; '
          'independent source/state proofs, not MCP equivalence; graph/exploration/'
          'unused-deps/Java-resource semantic parents remain separate obligations')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS parent_acceptance_notes(
 parent TEXT NOT NULL,note_sha256 TEXT NOT NULL,note TEXT NOT NULL,
 PRIMARY KEY(parent,note_sha256)
);
'''


def specification():
    return {'parent': FEATURE, 'criteria': spec.criteria(),
            'scope': 'Java/shared-on-Java; XML-only syntax and foreign-only features out-of-scope',
            'cli_surface_sha256': spec.REVIEWED_SURFACE_SHA256,
            'commands': spec.COMMAND_GROUPS,
            'contract_boundaries': [
                'src/main.rs Cli/Commands/SubtreeAction/GraphSymbolArgs/GraphAction',
                'README.md named subtrees and current-directory selectors',
                'README.md attached-root declaring-symbol ownership',
                'USER_GUIDE.md named subtrees, directory scope and confidence-labelled graph edges',
                'No new external/JDK binary-loader, compiler-equivalence or dynamic dispatch API is implied',
            ]}


def required_acceptance_features():
    return spec.required_acceptance_features()


def unmapped_commands():
    return spec.unmapped_commands()


def validate_specification(checklist):
    engine.validate_specification(checklist, policy=sys.modules[__name__])
    if spec.cli_surface() != checklist.get('cli_surface_sha256'):
        raise AcceptancePending('advertised Java scope commands/options changed without checklist review')
    if any(not item.get('population_sha256') for item in checklist['criteria']):
        raise AcceptancePending('scope criterion lacks complete nested assertion population')


def assertion_keys(samples, criterion):
    return spec.assertion_keys(samples, criterion['feature'])


def validate_population(samples, criterion):
    try:
        digest = spec.population_shape(samples, criterion['feature'])
    except ValueError as error:
        raise AcceptancePending(str(error)) from error
    if digest != criterion['population_sha256']:
        raise AcceptancePending('incomplete retained nested scope assertion population: ' + criterion['feature'])


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
    return engine.readiness(state, bindings, policy=sys.modules[__name__])


def exercise(fixture):
    return engine.exercise(fixture, policy=sys.modules[__name__])


def replay_children(fixture, source):
    retain_note(fixture.state, source)
    return engine.replay_children(fixture, source, policy=sys.modules[__name__])
