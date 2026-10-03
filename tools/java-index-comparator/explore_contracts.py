"""Bounded Java exploration contracts; authored source, not MCP equivalence."""
from fractions import Fraction
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'explore:ranking-budgets'}
REASON = ('independent source/state: disposable Java lexical seed budgets, ranking '
          'scores, RWR blend, root identities and nearest exact test candidates; '
          'not MCP equivalence or Java semantic dispatch')


def plan_explore(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-exploration-budgets'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('exploration artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    expected, actual = {}, {}
    with tempfile.TemporaryDirectory(prefix='exploration-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)

        def write(path, content, root=None):
            destination = (root or runner.root) / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)

        def record(label, want, got):
            expected[label], actual[label] = want, got

        def identities(doc):
            return sorted((runner.path(row['path']), row['name'], row['line'])
                          for row in doc['symbols'])

        # All fixtures are synthetic. Repetition exceeds the production caps
        # without storing large public source trees or using the native DB as truth.
        for i in range(220):
            write(f'noise/N{i:03d}.java', 'class AuroraService {}\n')
            write(f'stellar_adapter/N{i:03d}.java', 'class Noise {}\n')
        write('selected/Result.java', 'class AuroraServiceFactory {}\n')
        path_dir = 'selected/long/path/stellar_adapter'
        write(f'{path_dir}/Target.java', '''class Target {
    void first() {}
    void second() {}
    void third() {}
}
''')
        # Fuzzy candidates must also be scoped before their 40-row cap.
        for i in range(45):
            write(f'fuzzy/N{i:02d}.java', 'class Zwidget {}\n')
        write('selected/Fuzzy.java', 'class LongZwidgetAnswer {}\n')
        # Exact basename and closest-package selection precede the test cap.
        write('selected/pkg/Beacon.java', 'class Beacon {}\n')
        for i in range(55):
            write(f'decoy/D{i:02d}/OtherBeaconTest.java', 'class OtherBeaconTest {}\n')
            write(f'tests/D{i:02d}/BeaconTest.java', 'class BeaconTest {}\n')
        write('tests/pkg/BeaconTest.java', 'class BeaconTest {}\n')
        write('selected/Flat.java', 'class Flat {}\n')
        for i in range(55):
            write(f'tests/F{i:02d}/FlatTest.java', 'class FlatTest {}\n')
        write('selected/OutlineBudget.java', 'class OutlineBudget {\n' +
              ''.join(f'    void method{i:02d}() {{}}\n' for i in range(45)) + '}\n')
        write('selected/BodyBudget.java', 'class BodyBudget {\n    void longBody() {\n' +
              '        int value = 0;\n' + '        value++;\n' * 65 + '    }\n}\n')
        write('selected/AuroraService.java', 'class AuroraService {}\n')
        write('selected/Utility.java', 'class Utility {\n    void service() {}\n}\n')
        write('selected/tests/AuroraService.java', 'class AuroraService {}\n')
        runner.command('rebuild', '--force')

        doc = runner.json('explore', 'aurora service', cwd=runner.root / 'selected')
        record('compound-and-ranked-scope', True,
               any(row['path'] == 'selected/Result.java' for row in doc['symbols']))
        # search fallback shares the same ranking engine and source identities.
        fallback = runner.json('search', 'aurora service', cwd=runner.root / 'selected')
        record('search-fallback', ('explore', identities(doc)),
               (fallback.get('fallback'), identities(fallback)))
        doc = runner.json('explore', 'stellar adapter', cwd=runner.root / 'selected')
        record('path-seeds-scope-and-per-file-cap',
               [(f'project/{path_dir}/Target.java', name, line)
                for name, line in [('Target', 1), ('Target.first', 2), ('Target.second', 3)]], identities(doc))
        doc = runner.json('explore', 'widget', cwd=runner.root / 'selected')
        record('fuzzy-scope-before-cap', [('project/selected/Fuzzy.java', 'LongZwidgetAnswer', 1)], identities(doc))
        doc = runner.json('explore', 'Beacon', '--max-files', '1')
        record('exact-and-nearest-tests-before-cap', ['tests/pkg/BeaconTest.java'], doc['tests'][0]['tests'])
        doc = runner.json('explore', 'Flat', '--max-files', '1')
        record('test-candidate-cap', [f'tests/F{i:02d}/FlatTest.java' for i in range(50)], doc['tests'][0]['tests'])
        doc = runner.json('explore', 'OutlineBudget', '--max-files', '1')
        record('outline-cap', (40, 6, 'method38'),
               (len(doc['files'][0].get('outline', [])), doc['files'][0].get('outline_hidden'),
                doc['files'][0]['outline'][-1]['name']))
        doc = runner.json('explore', 'longBody', '--max-files', '1')
        record('source-line-cap', 60, len(doc['files'][0].get('source', '').splitlines()))
        doc = runner.json('explore', 'aurora service', '--max-files', '1')
        record('symbol-and-file-caps', (15, 1), (len(doc['symbols']), len(doc['files'])))
        doc = runner.json('explore', 'aurora service', '--max-files', '0')
        record('zero-file-budget', (15, [], []), (len(doc['symbols']), doc['files'], doc['tests']))
        # Exact numeric examples lock the published lexical scoring behaviour:
        # two-term corroboration, whole name, primary file, trivial member and test penalty.
        doc = runner.json('explore', 'aurora service', cwd=runner.root / 'selected')
        scores = {row['path']: round(row['score'], 6) for row in doc['symbols']}
        record('lexical-scores', {'selected/AuroraService.java': 475.67,
                                 'selected/tests/AuroraService.java': 142.701,
                                 'selected/Utility.java': 32.05},
               {path: scores.get(path) for path in ('selected/AuroraService.java',
                                                  'selected/tests/AuroraService.java', 'selected/Utility.java')})
        record('lexical-order', 'selected/AuroraService.java', doc['files'][0]['path'])
        # A two-node authored graph admits an independent rational recurrence.
        # 25 steps with restart 1/4, seed mass initially 1; no native metrics oracle.
        seed = Fraction(1)
        for _ in range(25):
            seed = 1 - Fraction(3, 4) * seed
        rwr_scores = {'Probe.signal': 1000.0,
                      'Probe.invoke': round(float(400 * (1 - seed) / seed), 6)}
        # Fresh, tiny project for exact RWR and colliding attached-root identities.
        tiny = runner.directory / 'tiny'
        tiny.mkdir()
        write('Probe.java', 'class Probe {\n    int signal() { return 1; }\n    int invoke() {\n        return signal();\n    }\n}\n', tiny)
        runner.environment['AST_INDEX_ROOT'] = str(tiny)
        runner.command('rebuild', '--force', cwd=tiny)
        for built in (False, True):
            if built:
                runner.command('graph', 'build', cwd=tiny)
            doc = runner.json('explore', 'signal', '--rwr', cwd=tiny)
            record(f'rwr-blend:{built}', rwr_scores,
                   {row['name']: round(row['score'], 6) for row in doc['symbols']})
            record(f'rwr-role:{built}', [('Probe.invoke', 'caller')],
                   [(row['name'], row['link']) for row in doc['neighbours']])
        attached = runner.directory / 'attached'
        attached.mkdir()
        for root, package, value in ((tiny, 'primary', 1), (attached, 'attached', 2)):
            write('Twin.java', f'package {package};\nclass Twin {{\n    int beaconOne() {{ return {value}; }} int beaconTwo() {{ return {value + 2}; }}\n}}\n', root)
            write('TwinTest.java', f'package {package}; class TwinTest {{}}\n', root)
        runner.json('subtree', 'add', 'attached', attached, cwd=tiny)
        runner.command('rebuild', '--force', cwd=tiny)
        runner.root = tiny
        for flags, roots in (((), ('tiny', 'attached')), (('--local',), ('tiny',)),
                             (('--subtree', 'attached'), ('attached',))):
            for rwr in (False, True):
                doc = runner.json(*flags, 'explore', 'beacon', '--max-files', '2',
                                  *(('--rwr',) if rwr else ()), cwd=tiny)
                want = sorted((f'{root}/Twin.java', f'{"primary" if root == "tiny" else "attached"}.Twin.{name}', 3)
                              for root in roots for name in ('beaconOne', 'beaconTwo'))
                record(f'root-and-same-line-identities:{flags}:{rwr}', want, identities(doc))
                record(f'root-file-selection:{flags}:{rwr}', sorted(f'{root}/Twin.java' for root in roots),
                       sorted(runner.path(row['path']) for row in doc['files']))
                record(f'root-body-ownership:{flags}:{rwr}', True,
                       all(f'return {1 if runner.path(row["path"]).startswith("tiny/") else 2};' in row.get('source', '')
                           for row in doc['files']))
                record(f'root-test-ownership:{flags}:{rwr}',
                       sorted((f'{root}/Twin.java', (f'{root}/TwinTest.java',)) for root in roots),
                       sorted((runner.path(row['source']), tuple(runner.path(path) for path in row['tests']))
                              for row in doc['tests']))
        _, text = runner.command('explore', 'beacon', '--max-files', '2', cwd=tiny)
        record('text-root-rendering', True,
               '[attached] ' + str(attached / 'Twin.java') in text and str(tiny / 'Twin.java') in text)
        # Root filtering also precedes caps when foreign roots alone can fill
        # all 200 ranked and 40 per-term candidates.
        for i in range(220):
            write(f'Noise{i:03d}.java', 'class Pulse {}\n', tiny)
        write('Target.java', 'class Pulse {}\n', attached)
        runner.command('rebuild', '--force', cwd=tiny)
        for rwr in (False, True):
            doc = runner.json('--subtree', 'attached', 'explore', 'pulse',
                              *(('--rwr',) if rwr else ()), cwd=tiny)
            record(f'root-scope-before-seed-caps:{rwr}', [('attached/Target.java', 'Pulse', 1)], identities(doc))
        return ({next(iter(FEATURES)): expected}, {next(iter(FEATURES)): actual})
