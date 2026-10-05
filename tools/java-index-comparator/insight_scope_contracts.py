"""Project insights on authored Java sources; independent source/state, not MCP truth."""
from collections import Counter
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'global:scope:java-map', 'global:scope:java-conventions'}
REASON = ('independent source/state: disposable Java insight directory/root/module intersections, '
          'colliding root identities, aggregate counts, ordered limits and text/JSON rendering; '
          'not MCP equivalence; module_count remains whole-index metadata')
PENDING = {'global:scope:java-map-module-count':
           'Module counts in map still describe the whole index; attached-root module ownership '
           'and module counts under directory selectors remain unresolved'}
GAP = ('Java file views, navigation, caller/call-tree and map/conventions directory/root '
       'selectors have separate executed contracts; module/map module-count/analysis/graph/explore scope remains unresolved')


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-insight-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for feature, reason in PENDING.items():
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'pending', reason))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      (GAP,))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('insight scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='insight-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    rows = []
    for owner, suffix, framework in (('project', 'Service', 'JUnit'), ('attached', 'Repository', 'Mockito')):
        for stem, path in (('Alpha', 'scope_/presentation/Alpha.java'),
                           ('Beta', 'scope_/presentation/Beta.java'),
                           ('Gamma', 'scope_/presentation/Gamma.java'),
                           ('Domain', 'scope_/domain/Domain.java'),
                           ('Data', 'scope_/data/Data.java'),
                           ('Outside', 'scopeX/presentation/Outside.java'),
                           ('Percent', 'scope%/presentation/Percent.java'),
                           ('Case', 'CAPS/presentation/Case.java')):
            name = stem + suffix if stem in ('Alpha', 'Beta', 'Gamma') else stem
            parent = 'PrimaryBase' if owner == 'project' else 'AttachedBase'
            import_name = 'org.junit.Test' if framework == 'JUnit' else 'org.mockito.Mockito'
            source = (f'import {import_name};\n' if stem in ('Alpha', 'Beta', 'Gamma') else '')
            source += f'class {name} extends {parent} {{}}\n'
            file = directory / owner / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(source)
            rows.append({'owner': owner, 'path': path, 'name': name, 'parent': parent,
                         'suffix': suffix, 'framework': framework, 'hit': stem in ('Alpha', 'Beta', 'Gamma')})
        # Inventory witnesses all relevant types, without exercising foreign parsers.
        marker = directory / owner / 'build' / 'Inventory.kt'
        marker.parent.mkdir()
        marker.write_text('// inventory only\n')
        (directory / owner / 'descriptor.xml').write_text('<fixture/>\n')
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.command('subtree', 'add', 'attached-label', '../attached')
    runner.command('rebuild', '--force', '--max-files', '0')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for owner in ('project', 'attached'):
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' "
                                           "AND extension IN ('.java','.kt','.xml') GROUP BY extension"))
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, {'.java': 8, '.kt': 1, '.xml': 1}, inventory)
            if inventory != {'.java': 8, '.kt': 1, '.xml': 1}:
                raise ToolError('insight fixture full inventory incomplete')
        finally:
            state.close()

    scopes = [('all', [], {'project', 'attached'}, ''),
              ('local', ['--local'], {'project'}, ''),
              ('attached', ['--subtree', 'attached-label'], {'attached'}, ''),
              ('underscore', [], {'project', 'attached'}, 'scope_/'),
              ('local-underscore', ['--local'], {'project'}, 'scope_/'),
              ('attached-underscore', ['--subtree', 'attached-label'], {'attached'}, 'scope_/'),
              ('percent', [], {'project', 'attached'}, 'scope%/'),
              ('case', [], {'project', 'attached'}, 'CAPS/'),
              ('empty', [], {'project', 'attached'}, 'empty/')]
    (runner.root / 'empty').mkdir()
    for label, flags, owners, prefix in scopes:
        cwd = runner.root / prefix
        selected = [r for r in rows if r['owner'] in owners and r['path'].startswith(prefix)]
        for module in (None, '', 'scope_/', 'scope_/presentation/', 'scope%/', 'CAPS/', 'caps/', 'missing/'):
            scoped = [r for r in selected if module is None or r['path'].startswith(module)]
            groups = {}
            for row in scoped:
                group = f"{row['owner']}/" + str(Path(row['path']).parent) + '/'
                groups.setdefault(group, []).append(row)
            ordered = sorted(groups, key=lambda g: (-len(groups[g]), g))
            # Absolute root identity is shared by text and JSON; normalize only paths.
            for limit, per_dir in ((0, 0), (1, 1), (100, 100)):
                args = [*flags, 'map', '--limit', str(limit)]
                if module is not None:
                    args += ['--module', module, '--per-dir', str(per_dir)]
                want_groups = []
                for group in ordered[:limit]:
                    entry = {'path': group, 'file_count': len(groups[group])}
                    if module is None:
                        entry['kinds'] = {'class': len(groups[group])}
                    else:
                        entry['symbols'] = [{'name': r['name'], 'kind': 'class', 'parents': [r['parent']],
                            'file': Path(r['path']).name} for r in sorted(groups[group], key=lambda r: r['name'])[:per_dir]]
                    want_groups.append(entry)
                want = {'file_count': len(selected), 'module_count': 0, 'groups': want_groups}
                if module is None:
                    want.update(showing=len(want_groups), total_dirs=len(groups))
                got = runner.json(*args, cwd=cwd)
                got.pop('project', None)
                for group in got['groups']:
                    group['path'] = runner.path(group['path'].rstrip('/')) + '/'
                key = f'{label}:map:{module}:{limit}:{per_dir}'
                record('global:scope:java-map', key, want, got)
                _, text = runner.command(*args, cwd=cwd)
                header = re.search(r'(\d+) files \| (\d+) modules', text)
                if module is None:
                    matches = re.findall(r'^  (.+?)\s+(\d+) files(?: \| (.*))?$', text, re.M)
                    observed = [{'path': runner.path(p.rstrip('/')) + '/', 'file_count': int(n),
                                 'kinds': {'class': int(re.fullmatch(r'(\d+) cls', kinds)[1])}}
                                for p, n, kinds in matches]
                    wanted = want_groups
                else:
                    matches = re.findall(r'^(.+?) \((\d+) files\)$', text, re.M)
                    observed = [(runner.path(p.rstrip('/')) + '/', int(n)) for p, n in matches]
                    wanted = [(g['path'], g['file_count']) for g in want_groups if g['symbols']]
                record('global:scope:java-map', key + ':text',
                       {'header': [len(selected), 0], 'groups': wanted, 'ansi': False},
                       {'header': list(map(int, header.groups())) if header else None,
                        'groups': observed, 'ansi': '\x1b' in text})
                if module is not None:
                    symbols = [(name, kind, parents.split(', ') if parents else []) for name, kind, parents in
                               re.findall(r'^  (\w+) : (\w+)(?: > (.*))?$', text, re.M)]
                    record('global:scope:java-map', key + ':text-symbols',
                           [(r['name'], r['kind'], r.get('parents', [])) for group in want_groups for r in group['symbols']],
                           symbols)
        naming = Counter(r['suffix'] for r in selected if r['hit'])
        frameworks = Counter(r['framework'] for r in selected if r['hit'])
        segments = {part for r in selected for part in Path(r['path']).parts[:-1]}
        want = {'architecture': ['Clean Architecture'] if {'presentation','domain','data'} <= segments else [],
                'frameworks': {'Testing': [{'name': name, 'count': count} for name, count in
                              sorted(frameworks.items(), key=lambda pair: (-pair[1], pair[0]))]} if frameworks else {},
                'naming_patterns': [{'suffix': suffix, 'count': count} for suffix, count in
                                   sorted(naming.items(), key=lambda pair: (-pair[1], pair[0])) if count >= 3]}
        record('global:scope:java-conventions', label + ':json', want,
               runner.json(*flags, 'conventions', cwd=cwd))
        _, text = runner.command(*flags, 'conventions', cwd=cwd)
        want_text = 'Project Conventions:\n\n'
        if want['architecture']:
            want_text += 'Architecture: ' + ', '.join(want['architecture']) + '\n\n'
        if frameworks:
            want_text += 'Testing: ' + ', '.join(f"{hit['name']} ({hit['count']})" for hit in want['frameworks']['Testing']) + '\n\n'
        if want['naming_patterns']:
            want_text += 'Naming Patterns:\n' + ''.join(f"  {hit['suffix']:20} {hit['count']}\n" for hit in want['naming_patterns']) + '\n'
        record('global:scope:java-conventions', label + ':text', want_text, text)
    return expected, actual
