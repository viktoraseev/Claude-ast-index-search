"""Git history and fallback ranking on disposable Java source, never MCP truth.

Expected history comes from an authored change ledger. Native DB rows are not
used as an oracle. The plain/ranked comparison checks an internal CLI promise;
it additionally checks the independent source candidate population.
"""
import difflib
import json
import math
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'changed', 'hotspots', 'search:rank-history'}
REASON = ('independent source/state: disposable Java/Git committed diffs, authored history '
          'ledger, percentile/filter/page checks and internal CLI ranking fallback; not MCP equivalence')
SORTS = ('score', 'commits', 'churn', 'relative-churn', 'fixes', 'authors', 'recent')
PENDING_REASON = ('Graph-dependent ranking formulas, lineage/substance and bounded candidate '
                  'pool ranking remain unresolved; search:rank-history covers history scoring, '
                  'missing-signal fallback, test exclusion and page prefixes on disposable Java source')


def plan_vcs(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-git'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("INSERT OR REPLACE INTO coverage VALUES (?,'pending',?)",
                      ('search:rank-presets', PENDING_REASON))


def source(package, name, value=0):
    return f'package fixture.{package};\nclass {name} {{\n    int value = {value};\n}}\n'


def percentile(values, value):
    return 100 * (sum(v < value for v in values) + sum(v == value for v in values) / 2) / len(values)


def round_positive(value):
    return math.floor(value + 0.5)


class History:
    """Small authored file histories, including lineage across an exact rename."""
    def __init__(self, runner):
        self.runner = runner
        # Inherited repository/index/config overrides must never redirect a
        # disposable Git mutation to the caller's working tree or hooks.
        self.runner.environment = {k: v for k, v in runner.environment.items() if not k.startswith('GIT_')}
        self.contents, self.rows = {}, {}
        self.commits = 0
        self.paths = set()

    def git(self, *args):
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                 '-c', 'core.autocrlf=false', *map(str, args)],
                                cwd=self.runner.root, env=self.runner.environment,
                                capture_output=True, timeout=15)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ToolError('disposable Git command failed; no project payload emitted')
        return result.stdout.decode().strip()

    def commit(self, changes, *, fix=False, author='One', rename=None):
        self.commits += 1
        timestamp = 1577836800 + 86400 * self.commits
        self.runner.environment.update(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=author.lower() + '@example.invalid',
                                       GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=author.lower() + '@example.invalid',
                                       GIT_AUTHOR_DATE=f'{timestamp} +0000', GIT_COMMITTER_DATE=f'{timestamp} +0000')
        if rename:
            old, new = rename
            self.git('mv', old, new)
            self.contents[new] = self.contents.pop(old)
            row = self.rows.pop(old)
            row['commits'] += 1
            row['fix_commits'] += int(fix)
            row['authors_set'].add(author)
            row['last_commit_at'] = timestamp
            self.rows[new] = row
            self.paths.add(new)
        for path, content in changes.items():
            previous = self.contents.get(path, '')
            before, after = previous.splitlines(), (content or '').splitlines()
            added = deleted = 0
            for op, i, j, k, l in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                if op != 'equal':
                    deleted += j - i
                    added += l - k
            row = self.rows.setdefault(path, {'commits': 0, 'fix_commits': 0, 'lines_added': 0,
                                              'lines_deleted': 0, 'authors_set': set(),
                                              'first_commit_at': timestamp, 'last_commit_at': timestamp})
            row.update(commits=row['commits'] + 1, fix_commits=row['fix_commits'] + int(fix),
                       lines_added=row['lines_added'] + added, lines_deleted=row['lines_deleted'] + deleted,
                       last_commit_at=timestamp)
            row['authors_set'].add(author)
            self.paths.add(path)
            target = self.runner.root / path
            if content is None:
                target.unlink()
                self.contents.pop(path)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                self.contents[path] = content
        self.git('add', '--all', '--', '.')
        self.git('commit', '-qm', 'fix regression' if fix else 'add fixture source')
        return self.git('rev-parse', 'HEAD')

    def expected(self):
        rows = []
        for path, content in self.contents.items():
            row = self.rows[path]
            rows.append({**{k: v for k, v in row.items() if k != 'authors_set'}, 'path': path,
                         'authors': len(row['authors_set']), 'current_lines': len(content.splitlines()),
                         'churn': row['lines_added'] + row['lines_deleted'],
                         'fix_ratio': row['fix_commits'] / row['commits']})
        for row in rows:
            for metric in ('commits', 'churn', 'fix_ratio', 'authors'):
                row[metric + '_exact_pct'] = percentile([r[metric] for r in rows], row[metric])
                row[metric + '_pct'] = round_positive(row[metric + '_exact_pct'])
            row['score_exact'] = sum(row[k + '_exact_pct'] for k in ('commits', 'churn', 'fix_ratio')) / 3
            row['score'] = round_positive(row['score_exact'])
            row['fix_ratio'] = round(row['fix_ratio'], 2)
            row['relative_churn'] = (round(row['churn'] / row['current_lines'], 2)
                                     if row['current_lines'] >= 10 else None)
        return rows


def observation(row):
    keys = ('path', 'commits', 'fix_commits', 'lines_added', 'lines_deleted', 'churn', 'authors',
            'current_lines', 'first_commit_at', 'last_commit_at', 'fix_ratio', 'relative_churn',
            'commits_pct', 'churn_pct', 'fix_ratio_pct', 'authors_pct', 'score')
    return {k: row.get(k) for k in keys}


def order(rows, sort):
    def key(row):
        if sort == 'fixes':
            n, f, z = row['commits'], row['fix_commits'], 1.96
            p = f / n
            bound = (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)
            return (-(n >= 4), -bound, -f, -row['churn'], row['path'])
        metric = {'score': 'score_exact', 'commits': 'commits', 'churn': 'churn',
                  'relative-churn': 'relative_churn', 'authors': 'authors', 'recent': 'last_commit_at'}[sort]
        return (-(row[metric] or 0), -row['churn'], row['path'])
    return sorted(rows, key=key)


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('VCS artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='vcs-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    history = History(runner)
    history.git('init', '-q', '--template=', '--initial-branch=main')
    initial = {
        'a/ProbeHelper.java': source('a', 'ProbeHelper'),
        'b/Probe.java': source('b', 'Probe'),
        'vendor/Probe.java': source('vendor', 'Probe'),
        'tests/ProbeTest.java': source('tests', 'ProbeTest'),
        'a/Legacy.java': source('a', 'Legacy'),
        'a/Deleted.java': source('a', 'Deleted'),
    }
    base_sha = history.commit(initial)
    history.git('update-ref', 'refs/remotes/origin/main', base_sha)
    runner.command('rebuild', '--force')
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    # This is an internal CLI ordering invariant, supplemented by the source
    # population. Four Java candidates, two exact-name declarations in separate
    # packages, a test and a partial project match exercise fallback ordering.
    for excluded in (False, True):
        flags = ['--exclude-tests'] if excluded else []
        plain = runner.json('search', 'Probe', '--type', 'class', '--limit', 100)
        identities = lambda output, section: [(r, None) if isinstance(r, str) else
                                              (r['path'], r.get('qualified_name')) for r in output[section]]
        want_symbols = [r for r in identities(plain, 'symbols') if not excluded or not r[0].startswith('tests/')]
        want_files = [r for r in identities(plain, 'files') if not excluded or not r[0].startswith('tests/')]
        record('search:rank-history', f'population:{excluded}',
               sorted((p, 'fixture.' + package + '.' + name) for p, package, name in
                      [('a/ProbeHelper.java', 'a', 'ProbeHelper'), ('b/Probe.java', 'b', 'Probe'),
                       ('vendor/Probe.java', 'vendor', 'Probe'), ('tests/ProbeTest.java', 'tests', 'ProbeTest')]
                      if not excluded or package != 'tests'), sorted(want_symbols))
        for preset in ('hotspots', 'proven', 'risky', 'central'):
            for limit in (0, 1, 100):
                output = runner.json('search', 'Probe', '--type', 'class', '--rank', preset,
                                     '--limit', limit, *flags)
                key = f'missing:{preset}:{excluded}:{limit}'
                missing = ['graph'] if preset == 'central' else ['history'] if preset == 'hotspots' else ['graph', 'history']
                record('search:rank-history', key, {'applied': False, 'missing': missing,
                       'symbols': want_symbols[:limit], 'files': want_files[:limit],
                       'symbol_total': len(want_symbols), 'file_total': len(want_files), 'unscored': True},
                       {'applied': output['rank']['applied'], 'missing': [r['signal'] for r in output['rank']['missing']],
                        'symbols': identities(output, 'symbols'), 'files': identities(output, 'files'),
                        'symbol_total': output['pagination']['symbols']['total'],
                        'file_total': output['pagination']['files']['total'],
                        'unscored': all(r['rank'] is None for s in ('files', 'symbols') for r in output[s])})

    head = history.commit({'a/ProbeHelper.java': source('a', 'ProbeHelper', 1), 'a/Deleted.java': None},
                          fix=True, author='Two', rename=('a/Legacy.java', 'a/Renamed.java'))
    head = history.commit({'a/ProbeHelper.java': source('a', 'ProbeHelper', 2)}, fix=True)
    runner.command('rebuild', '--force')
    changes = [{'status': 'D', 'path': 'a/Deleted.java'},
               {'status': 'M', 'path': 'a/ProbeHelper.java'},
               {'status': 'R', 'path': 'a/Renamed.java', 'old_path': 'a/Legacy.java'}]
    # Dirty/untracked source must not be substituted for committed HEAD diff.
    (runner.root / 'b/Dirty.java').write_text(source('b', 'Dirty'))
    (runner.root / 'b/Probe.java').write_text(source('b', 'Probe', 99))
    for scope in (None, 'a', 'b'):
        for explicit in (False, True):
            flags = ['--base', base_sha] if explicit else []
            output = runner.json('changed', *flags, cwd=runner.root / scope if scope else runner.root)
            selected = changes if scope in (None, 'a') else []
            record('changed', f'diff:{scope}:{explicit}',
                   {'schema_version': 1, 'vcs': 'git', 'base': base_sha if explicit else 'origin/main',
                    'head': 'HEAD', 'scope': scope, 'changes': selected}, output)
    # Restore dirty data by writing fixture content, without mutating the target.
    (runner.root / 'b/Dirty.java').unlink()
    (runner.root / 'b/Probe.java').write_text(history.contents['b/Probe.java'])
    _, text = runner.command('changed', '--base', base_sha)
    record('changed', 'text', f'Changed files against {base_sha} (3):\n  D  a/Deleted.java\n'
           '  M  a/ProbeHelper.java\n  R  a/Legacy.java -> a/Renamed.java\n', text)
    for args in (['changed', '--base', 'missing-fixture-ref'], ['changed', '--base', base_sha, '--timeout-ms', 0]):
        code, _ = runner.command(*args, acceptable=(1,))
        record('changed', ':'.join(map(str, args)), 1, code)

    def check_history(label, collect_args):
        output = runner.json('hotspots', *collect_args, '--limit', 100, '--window', 1)
        rows = history.expected()
        record('hotspots', label + ':ledger', sorted(map(observation, rows), key=lambda r: r['path']),
               sorted(map(observation, output['items']), key=lambda r: r['path']))
        # Exact renames have one folded lineage; deleted lineages still count.
        record('hotspots', label + ':summary', [head, history.commits, len(rows), len(history.rows)],
               [output['head'], output['commits_analyzed'], output['files_with_history'], output['paths_in_history']])
        for sort in SORTS:
            for limit, prefix, minimum, excluded in ((0, '', 1, False), (1, '', 1, False),
                                                     (100, 'a/', 2, False), (100, '', 1, True)):
                selected = order([r for r in rows if r['path'].startswith(prefix) and r['commits'] >= minimum
                                  and (not excluded or not r['path'].startswith('tests/'))], sort)
                flags = ['--exclude-tests'] if excluded else []
                output = runner.json('hotspots', '--sort', sort, '--limit', limit, '--path', prefix,
                                     '--min-commits', minimum, *flags)
                key = f'{label}:{sort}:{limit}:{prefix}:{minimum}:{excluded}'
                record('hotspots', key, {'items': [observation(r) for r in selected[:limit]],
                       'total': len(selected), 'truncated': len(selected) > limit, 'population': len(rows)},
                       {'items': [observation(r) for r in output['items']], 'total': output['pagination']['total'],
                        'truncated': output['pagination']['truncated'], 'population': output['files_with_history']})
        return rows

    rows = check_history('full', ['--collect', '--full'])
    check_history('no-change', ['--collect'])
    # Hotspot scoring is independent of source navigation and graph ranking.
    by_path = {r['path']: r for r in rows}
    for excluded in (False, True):
        flags = ['--exclude-tests'] if excluded else []
        pages = []
        for limit in (0, 1, 100):
            output = runner.json('search', 'Probe', '--type', 'class', '--rank', 'hotspots', '--limit', limit, *flags)
            pages.append(output)
            for section in ('files', 'symbols'):
                want_scores, got_scores = {}, {}
                for item in output[section]:
                    path, dossier = item['path'], item['rank']
                    want_scores[path] = round(by_path[path]['score_exact'] / 100, 3)
                    got_scores[path] = dossier['score']
                record('search:rank-history', f'scores:{excluded}:{limit}:{section}', want_scores, got_scores)
            record('search:rank-history', f'applied:{excluded}:{limit}', [True, 3 if excluded else 4, 3 if excluded else 4],
                   [output['rank']['applied'], output['pagination']['files']['total'], output['pagination']['symbols']['total']])
        for section in ('files', 'symbols'):
            full = pages[-1][section]
            record('search:rank-history', f'prefix:{excluded}:{section}', [r['path'] for r in full[:1]],
                   [r['path'] for r in pages[1][section]])
            record('search:rank-history', f'population-ready:{excluded}:{section}',
                   sorted(p for p in initial if 'Probe' in p and (not excluded or not p.startswith('tests/'))),
                   sorted(r['path'] for r in full))
    head = history.commit({'c/ProbeNew.java': source('c', 'ProbeNew')})
    runner.command('update')
    check_history('incremental', ['--collect'])
    check_history('recollected', ['--full'])
    for args in (['hotspots', '--sort', 'invalid'], ['hotspots', '--collect', '--timeout-ms', 0]):
        code, _ = runner.command(*args, acceptable=(1,))
        record('hotspots', ':'.join(map(str, args)), 1, code)
    return expected, actual


def exclusion_budget(binary, base):
    """Generate private Java source just above the old 50,000-row scan cap."""
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('VCS artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='vcs-budget-', dir=base)).resolve()
    runner = Runner(binary, directory)
    (runner.root / 'tests').mkdir(parents=True)
    for chunk in range(6):
        with (runner.root / f'tests/Crowd{chunk}.java').open('w') as output:
            for i in range(chunk * 9000, min((chunk + 1) * 9000, 50010)):
                output.write(f'class Crowd{i:05d} {{ void Probe() {{}} }}\n')
    (runner.root / 'Live.java').write_text('class Live { void ProbeLive() {} }\n')
    runner.command('rebuild', '--force')
    expected, actual = {}, {}
    # First establish that every generated Java declaration was indexed;
    # otherwise a skipped oversized source could manufacture a false pass.
    population = runner.json('search', 'Probe', '--type', 'function', '--limit', 0)
    expected['population'], actual['population'] = 50011, population['pagination']['symbols']['total']
    for scope, total in (([], 1), (['--module', 'tests/'], 0), (['--in-file', 'Live.java'], 1)):
        for preset in ('hotspots', 'central'):
            for fuzzy in (False, True):
                flags = ['--fuzzy'] if fuzzy else []
                output = runner.json('search', 'Probe', '--type', 'function', '--rank', preset,
                                     '--exclude-tests', '--limit', 1, *scope, *flags)
                key = json.dumps([scope, preset, fuzzy])
                expected[key] = {'total': total, 'paths': ['Live.java'] if total else [], 'truncated': False}
                actual[key] = {'total': output['pagination']['symbols']['total'],
                               'paths': [r['path'] for r in output['symbols']],
                               'truncated': output['pagination']['symbols']['truncated']}
    return expected, actual
