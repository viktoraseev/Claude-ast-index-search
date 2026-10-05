"""Java exploration and intent fallback rendering from authored source, not MCP."""
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

EXPLORE = 'global:format:java-exploration'
FALLBACK = 'global:format:java-intent-fallback'
FEATURES = {EXPLORE, FALLBACK}
REASON = ('independent source/state: disposable Java exploration and ranked/unranked '
          'intent fallback identities, kind filters, budgets, source/outline truncation, '
          'empty/missing index, test ownership and colliding-root JSON/text rendering; '
          'not MCP equivalence or compiler-wide dispatch')
SOURCE = '''package fixture.{owner};
class SignalProbe {{
 int signal() {{
  return {value};
 }}
 int consume() {{
  return signal();
 }}
}}
'''


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-exploration-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views, module diagrams, navigation, callers/call-tree, '
                       'literal/ranked search and intent fallback/exploration formats have separate '
                       'executed contracts; analysis, management and lifecycle formats remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('exploration format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='exploration-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for owner, value in [('project', 7), ('attached', 107)]:
        root = directory / owner
        source = root / 'src/SignalProbe.java'
        source.parent.mkdir(parents=True)
        source.write_text(SOURCE.format(owner=owner, value=value))
        (source.parent / 'SignalProbeTest.java').write_text(f'package fixture.{owner};\nclass SignalProbeTest {{}}\n')
        # These tiny non-Java files prove that applicability uses a full inventory.
        (root / 'Inventory.kt').write_text('// inventory only\n')
        (root / 'descriptor.xml').write_text('<fixture/>\n')
        (root / 'empty').mkdir()
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, root)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            want = {'.java': 2, '.kt': 1, '.xml': 1}
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, want, inventory)
            if inventory != want:
                raise ToolError('exploration fixture full inventory incomplete')
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    for feature, arguments in [(EXPLORE, ['explore', 'signal']),
                               (FALLBACK, ['search', 'how signal works'])]:
        for format in ('json', 'text'):
            code, output = runner.command('--format', format, *arguments, acceptable=(0, 1))
            record(feature, 'missing-index:' + format,
                   (1, '') if format == 'json' else (0, "Index not found. Run 'ast-index rebuild' first.\n"),
                   (code, output))
    runner.command('rebuild', '--force')
    runner.command('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force')

    def identity(row):
        return runner.path(row['path']), row['name'], row['kind'], row['line']

    scopes = [('all', [], ['project', 'attached'], ''), ('local', ['--local'], ['project'], ''),
              ('attached', ['--subtree', 'attached-label'], ['attached'], ''),
              ('missing', ['--subtree', 'absent'], [], ''), ('empty', [], [], 'empty')]
    for built in (False, True):
        if built:
            runner.command('graph', 'build')
        for feature in sorted(FEATURES):
            for label, flags, owners, cwd in scopes:
                modes = [(False, None), (True, None)] if feature == EXPLORE else [
                    (False, None), (True, None), (False, 'function'), (True, 'class'), (False, 'absent-kind')]
                for ranked, kind in modes:
                    for limit in (0, 1, 100):
                        if feature == EXPLORE:
                            args = ['explore', 'signal', '--max-files', limit, *(['--rwr'] if ranked else [])]
                        else:
                            args = ['search', 'how signal works', '--limit', limit,
                                    *(['--rank', 'central'] if ranked else []),
                                    *(['--type', kind] if kind else [])]
                        doc = runner.json(*flags, *args, cwd=runner.root / cwd)
                        _, text = runner.command(*flags, *args, cwd=runner.root / cwd)
                        candidates, neighbours = [], []
                        for owner in owners:
                            path = owner + '/src/SignalProbe.java'
                            candidates.extend([(path, f'fixture.{owner}.SignalProbe', 'class', 2),
                                               (path, f'fixture.{owner}.SignalProbe.signal', 'function', 3),
                                               (owner + '/src/SignalProbeTest.java', f'fixture.{owner}.SignalProbeTest', 'class', 2)])
                            if feature == EXPLORE and ranked:
                                neighbour = (path, f'fixture.{owner}.SignalProbe.consume', 'function', 6)
                                candidates.append(neighbour)
                                neighbours.append(neighbour)
                        if kind:
                            candidates = [r for r in candidates if r[2] == kind]
                        got = [identity(r) for r in doc['symbols']]
                        text_rows = [(runner.path(p), n, k, int(line)) for n, k, p, line in
                                     re.findall(r'^  (.+?) \[([^]]+)\]  (.+\.java):(\d+)  score=', text, re.M)]
                        symbol_cap = min(15, limit) if feature == FALLBACK else 15
                        file_cap = min(6, limit) if feature == FALLBACK else limit
                        paths = {r[0] for r in got}
                        files = [runner.path(r['path']) for r in doc['files']]
                        tests = [(runner.path(r['source']), [runner.path(p) for p in r['tests']]) for r in doc['tests']]
                        text_tests = [(runner.path(p), [] if targets == 'no test file found by convention'
                                       else [runner.path(target) for target in targets.split(', ')]) for p, targets in
                                      re.findall(r'^  (.+\.java) ← (.+)$', text, re.M)]
                        text_neighbours = [(runner.path(p), name, kind, int(line)) for role, name, kind, p, line in
                                           re.findall(r'^  \[(caller|subclass)\] (.+?) \[([^]]+)\]  (.+\.java):(\d+)$', text, re.M)]
                        file_text = [runner.path(p) for p in re.findall(r'^#### (.+\.java) — ', text, re.M)]
                        content_valid = True
                        for row in doc['files']:
                            path = runner.path(row['path'])
                            if row['symbol'].endswith('.signal'):
                                value = 107 if path.startswith('attached/') else 7
                                want = f'    3\t int signal() {{\n    4\t  return {value};\n    5\t }}\n'
                                content_valid &= row.get('source') == want and want in text
                                content_valid &= (row.get('truncated'), row.get('end_line'), row.get('displayed_end_line')) == (False, 5, 5)
                            elif row['symbol'].endswith('.consume'):
                                want = '    6\t int consume() {\n    7\t  return signal();\n    8\t }\n'
                                content_valid &= row.get('source') == want and want in text
                                content_valid &= (row.get('truncated'), row.get('end_line'), row.get('displayed_end_line')) == (False, 8, 8)
                            else:
                                outline = [('SignalProbe', 'class', 2, 9), ('signal', 'function', 3, 5),
                                           ('consume', 'function', 6, 8)] if path.endswith('/SignalProbe.java') else [
                                               ('SignalProbeTest', 'class', 2, 2)]
                                content_valid &= [(r['name'], r['kind'], r['line'], r['end_line'])
                                                  for r in row.get('outline', [])] == outline
                                content_valid &= row.get('outline_hidden') == 0
                                for outline_name, outline_kind, outline_line, outline_end in outline:
                                    position = (f':{outline_line}-{outline_end}' if outline_end > outline_line
                                                else f':{outline_line}')
                                    marker = '→' if outline_line == row['line'] else ' '
                                    content_valid &= f'  {marker} {position} {outline_name} [{outline_kind}]\n' in text
                        key = f'{built}:{label}:{ranked}:{kind}:{limit}'
                        want_tests = [(p, [p.replace('SignalProbe.java', 'SignalProbeTest.java')]
                                       if p.endswith('/SignalProbe.java') else []) for p in files]
                        record(feature, key,
                               {'symbols': min(symbol_cap, len(candidates)), 'identities': True,
                                'complete': True, 'text': True, 'files': min(file_cap, len(paths)),
                                'file-identities': True, 'source': True, 'tests': True,
                                'neighbours': sorted(neighbours), 'text-neighbours': sorted(neighbours),
                                'roles': ['caller'] * len(neighbours), 'ansi': False,
                                'query': 'how signal works' if feature == FALLBACK else 'signal',
                                'language': 'java' if candidates else None,
                                'reason': ('no literal matches for a multi-word query; results are ranked by relevance')
                                          if feature == FALLBACK else None,
                                'fallback': 'explore' if feature == FALLBACK else None,
                                'label': feature == FALLBACK},
                               {'symbols': len(got), 'identities': len(set(got)) == len(got) and set(got) <= set(candidates),
                                'complete': len(candidates) > symbol_cap or sorted(got) == sorted(candidates),
                                'text': got == text_rows, 'files': len(files),
                                'file-identities': len(set(files)) == len(files) and set(files) <= paths and files == file_text,
                                'source': content_valid, 'tests': tests == want_tests and text_tests == want_tests,
                                'neighbours': sorted(identity(r) for r in doc['neighbours']),
                                'text-neighbours': sorted(text_neighbours),
                                'roles': sorted(r['link'] for r in doc['neighbours']), 'ansi': '\x1b' in text,
                                'query': doc.get('query'), 'language': doc.get('dominant_language'), 'reason': doc.get('reason'),
                                'fallback': doc.get('fallback'),
                                'label': 'Showing relevance-ranked results' in text})
    for query in ('', 'a b', 'how does the', 'AbsentContractMatch'):
        doc = runner.json('explore', query)
        record(EXPLORE, 'empty:' + query,
               {'query': query, 'dominant_language': None, 'symbols': [], 'neighbours': [], 'files': [], 'tests': []}, doc)
        _, text = runner.command('explore', query)
        record(EXPLORE, 'empty-text:' + query,
               'explore: query has no usable terms (need identifiers >= 3 chars)\n' if query in ('', 'a b')
               else f"explore: no symbols matched '{query}'\n", text)
    # Search comma syntax stays literal even when no terms match.
    for query in ('AbsentOne,AbsentTwo', 'a b', ''):
        doc = runner.json('search', query)
        record(FALLBACK, 'literal:' + query, False, 'fallback' in doc)
    # Filters precede ranked, per-term, fuzzy and per-file seed budgets. A
    # post-sampling filter can return an empty answer despite an eligible hit.
    (runner.root / 'src/Crowded.java').write_text('class Crowded {\n' +
        ''.join(f' void crowdedSignal{i}() {{}}\n' for i in range(220)) +
        ' class CrowdedSignalNeedle {}\n}\n')
    path_budget = runner.root / 'src/path_budget/Host.java'
    path_budget.parent.mkdir()
    path_budget.write_text('class Host {\n class First {}\n class Second {}\n class Third {}\n'
                           ' void sole() {}\n}\n')
    (runner.root / 'src/Fuzzy.java').write_text(
        ''.join(f'class Fuzzy{i} {{}}\n' for i in range(45)) +
        'class Holder { void prefixFuzzyTail() {} }\n')
    (runner.root / 'src/NeedleService.java').write_text('class NeedleService {}\n')
    runner.command('rebuild', '--force')
    guards = [('how crowdedSignal works', 'class', 'Crowded.CrowdedSignalNeedle',
               'project/src/Crowded.java', 222),
              ('how path budget works', 'function', 'Host.sole', 'project/src/path_budget/Host.java', 5),
              ('how fuzzy works', 'function', 'Holder.prefixFuzzyTail', 'project/src/Fuzzy.java', 46),
              ('how needle service works', 'class', 'NeedleService', 'project/src/NeedleService.java', 1)]
    for query, kind, name, path, line in guards:
        for ranked in (False, True):
            args = ['--local', 'search', query, '--type', kind, '--limit', 1,
                    *(['--rank', 'central'] if ranked else [])]
            doc = runner.json(*args)
            record(FALLBACK, f'filter-before-budget:{query}:{ranked}', [(path, name, kind, line)],
                   [identity(r) for r in doc['symbols']])
    # Bounded contexts must disclose truncation in both renderers.
    long = runner.root / 'src/LongProbe.java'
    long.write_text('class LongProbe {\n int lengthy() {\n' + '  // padding\n' * 65 + '  return 1;\n }\n}\n')
    wide = runner.root / 'src/WideProbe.java'
    wide.write_text('class WideProbe {\n' + ''.join(f' void member{i}() {{}}\n' for i in range(43)) + '}\n')
    runner.command('rebuild', '--force')
    for feature in sorted(FEATURES):
        for query, mode in [('lengthy', 'body'), ('WideProbe', 'outline')]:
            args = ['explore', query, '--max-files', 1] if feature == EXPLORE else ['search', 'how ' + query + ' works']
            doc = runner.json(*args)
            _, text = runner.command(*args)
            row = doc['files'][0]
            if mode == 'body':
                want = {'source': ''.join(f'{line:>5}\t{content}\n' for line, content in
                                         enumerate(long.read_text().splitlines()[1:61], 2)),
                        'truncated': True, 'end_line': 69, 'displayed_end_line': 61, 'text-marker': True}
                got = {k: row.get(k) for k in ('source', 'truncated', 'end_line', 'displayed_end_line')}
                got['text-marker'] = '(source truncated at line 61; ends at line 69)' in text
            else:
                want = {'outline': [('WideProbe', 'class', 1, 45)] +
                        [(f'member{i}', 'function', i + 2, i + 2) for i in range(39)],
                        'hidden': 4, 'text-marker': True}
                got = {'outline': [(r['name'], r['kind'], r['line'], r['end_line']) for r in row.get('outline', [])],
                       'hidden': row.get('outline_hidden'), 'text-marker': '… 4 more' in text}
            record(feature, 'bounded:' + mode, want, got)
    sources = [directory / owner / 'src' / name for owner in ('project', 'attached')
               for name in ('SignalProbe.java', 'SignalProbeTest.java')]
    sources += [runner.root / 'src' / name for name in
                ('Crowded.java', 'Fuzzy.java', 'NeedleService.java', 'LongProbe.java', 'WideProbe.java')]
    sources.append(path_budget)
    with (directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'javac'), *map(str, sources)],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('exploration fixture javac validation failed; see private log')
    return expected, actual
