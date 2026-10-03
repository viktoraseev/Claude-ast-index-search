"""Root registration and Java scope contracts on disposable source.

Expected declarations and text sites come from the small source below, never
from native DB rows or a second native query. No MCP equivalent exists for
registering roots or selecting a native cache.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError


FEATURES = {'add-root', 'remove-root', 'subtree', 'global:local',
            'global:subtree', 'global:walk-up', 'global:explicit-root'}
REASON = ('independent source/state: disposable Java root registration, cache selection '
          'and SQL/text scope checks; not MCP equivalence')
SOURCE = '''package fixture.{package};
@Deprecated
class RootProbe extends ProbeBase {{
    // TODO root-contract
    @Inject Object service;
    void ping() {{}}
    void use() {{ ping(); }}
}}
class ProbeBase {{}}
'''


class Runner:
    def __init__(self, binary, directory):
        self.binary, self.directory = Path(binary).resolve(), directory
        self.root = directory / 'project'
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(('AST_INDEX_', 'KOTLIN_INDEX_'))}
        self.environment.update(AST_INDEX_CACHE_DIR=str(directory / 'cache'),
                                AST_INDEX_DISABLE_GC='1', AST_INDEX_THREADS='2', NO_COLOR='1')
        self.sequence = 0

    def command(self, *arguments, cwd=None, environment=None, acceptable=(0,)):
        cwd = Path(cwd or self.root).resolve()
        if not cwd.is_relative_to(self.directory):
            raise ToolError('root fixture command escaped disposable artifact directory')
        self.sequence += 1
        prefix = self.directory / f'{self.sequence:03d}'
        with prefix.with_suffix('.stdout.log').open('wb') as stdout, \
                prefix.with_suffix('.stderr.log').open('wb') as stderr:
            result = subprocess.run([str(self.binary), *map(str, arguments)], cwd=cwd,
                                    env={**self.environment, **(environment or {})},
                                    stdout=stdout, stderr=stderr, timeout=15)
        if result.returncode not in acceptable:
            raise ToolError('disposable root command failed; see private fixture logs')
        with prefix.with_suffix('.stdout.log').open('rb') as stream:
            output = stream.read(1024 * 1024 + 1)
        if len(output) > 1024 * 1024:
            raise ToolError('root fixture output exceeded its budget')
        return result.returncode, output.decode('utf-8')

    def json(self, *arguments, **kwargs):
        _, output = self.command('--format', 'json', *arguments, **kwargs)
        try:
            return json.loads(output)
        except ValueError as error:
            raise ToolError('root fixture expected JSON; see private command logs') from error

    def path(self, value):
        value = re.sub(r'^\[[^]]+\] ', '', value)
        path = Path(value)
        path = path if path.is_absolute() else self.root / path
        try:
            return path.relative_to(self.directory).as_posix()
        except ValueError as error:
            raise ToolError('root fixture result escaped disposable artifact directory') from error

    def roots(self):
        return sorted((row['name'], self.path(row['canonical_path']), row['original_path'])
                      for row in self.json('subtree', 'list'))

    def classes(self):
        rows = self.json('class', 'RootProbe', '--limit', '100')
        return sorted((self.path(row['path']), row['qualified_name'], row['line'])
                      for row in rows['items'])

    def scope(self, flags, paths):
        """Check source identities, per-category totals and scope before limits."""
        expected, actual = {}, {}
        # Colliding relative paths expose root ownership mistakes; crowded
        # primary rows expose filtering after LIMIT, even on an empty page.
        for command, arguments, line in (
                ('class', ['RootProbe'], 3),
                ('symbol', ['RootProbe', '--type', 'class'], 3),
                ('implementations', ['ProbeBase'], 3),
                ('usages', ['ping'], 7),
                ('callers', ['ping'], 7)):
            for limit in (1, 100):
                output = self.json(*flags, command, *arguments, '--limit', str(limit))
                items = [(self.path(row['path']), row['line']) for row in output['items']]
                candidates = [(path, line) for path in paths]
                key = f'{command}:{limit}'
                expected[key] = {'total': len(paths), 'returned': min(limit, len(paths)),
                                 'valid': True, 'complete': True}
                actual[key] = {'total': output['pagination']['total'], 'returned': len(items),
                               'valid': len(set(items)) == len(items) and set(items) <= set(candidates),
                               'complete': limit < len(paths) or sorted(items) == sorted(candidates)}
        for limit in (1, 100):
            output = self.json(*flags, 'refs', 'ping', '--limit', str(limit))
            for section, line in (('definitions', 6), ('usages', 7), ('imports', None)):
                items = [(self.path(row['path']), row['line']) for row in output[section]]
                candidates = [(path, line) for path in paths] if line else []
                key = f'refs:{section}:{limit}'
                expected[key] = {'total': len(candidates), 'returned': min(limit, len(candidates)),
                                 'valid': True, 'complete': True}
                actual[key] = {'total': output['pagination'][section]['total'], 'returned': len(items),
                               'valid': len(set(items)) == len(items) and set(items) <= set(candidates),
                               'complete': limit < len(candidates) or sorted(items) == sorted(candidates)}
            _, hierarchy = self.command(*flags, 'hierarchy', 'ProbeBase', '--limit', str(limit))
            items = [self.path(path) for path in re.findall(r'^    .+ \[class\]: (.+\.java)$',
                                                           hierarchy, re.MULTILINE)]
            expected[f'hierarchy:{limit}'] = {'returned': min(limit, len(paths)), 'valid': True}
            actual[f'hierarchy:{limit}'] = {'returned': len(items),
                                           'valid': len(set(items)) == len(items) and set(items) <= set(paths)}
        for limit in (0, 1, 100):
            output = self.json(*flags, 'file', 'Main.java', '--limit', str(limit))
            items = [self.path(path) for path in output]
            candidates = [path for path in paths if path.endswith('/Main.java')]
            key = f'file:{limit}'
            expected[key] = {'returned': min(limit, len(candidates)), 'valid': True, 'complete': True}
            actual[key] = {'returned': len(items),
                           'valid': len(set(items)) == len(items) and set(items) <= set(candidates),
                           'complete': limit < len(candidates) or sorted(items) == sorted(candidates)}
        search = self.json(*flags, 'search', 'RootProbe', '--type', 'class', '--limit', '100')
        expected['search'] = {'symbols': sorted((path, 3) for path in paths),
                              'content': sorted((path, 3) for path in paths),
                              'symbol-total': len(paths), 'content-total': len(paths)}
        actual['search'] = {'symbols': sorted((self.path(row['path']), row['line']) for row in search['symbols']),
                            'content': sorted((self.path(row['path']), row['line']) for row in search['content_matches']),
                            'symbol-total': search['pagination']['symbols']['total'],
                            'content-total': search['pagination']['content_matches']['total']}
        # Text traversal must select the same roots as SQL traversal.
        for command, arguments, line in (('todo', [], 4), ('annotations', ['Deprecated'], 2),
                                         ('deprecated', [], 2), ('inject', ['Object'], 5)):
            _, output = self.command(*flags, command, *arguments, '--limit', '100')
            sites = re.findall(r'^  (?:.*?: )?((?:\[[^]]+\] )?[^\n]*?\.java):(\d+)(?:\s.*)?$',
                               output, re.MULTILINE)
            expected[command] = sorted((path, line) for path in paths)
            actual[command] = sorted((self.path(path), int(number)) for path, number in sites)
        code, _ = self.command('--local', '--subtree', 'named', 'class', 'RootProbe',
                               acceptable=(2,))
        expected['conflicting-flags'] = 2
        actual['conflicting-flags'] = code
        return expected, actual


def explicit_root_samples(binary, directory):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    directory = Path(directory).resolve()
    if not directory.is_relative_to(boundary.resolve()):
        raise ToolError('explicit root fixtures must stay inside repository .artifacts')
    directory.mkdir(parents=True, exist_ok=True)
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    (runner.root / 'pom.xml').write_text('<project><artifactId>parent</artifactId></project>')
    (runner.root / 'ParentOnly.java').write_text('class ParentOnly {}\n')
    target = runner.root / 'java-only'
    target.mkdir()
    (target / 'TargetOnly.java').write_text('class TargetOnly {}\n')
    invoker = directory / 'invoker'
    invoker.mkdir()
    (invoker / '.git').mkdir()
    (invoker / 'InvokerOnly.java').write_text('class InvokerOnly {}\n')
    environment = {'AST_INDEX_ROOT': str(target),
                   'AST_INDEX_DB_PATH': str(directory / 'explicit.sqlite')}
    expected, actual = {}, {}
    for label, root, cwd in (('absolute', str(target), target),
                              ('relative', 'java-only', runner.root)):
        expected[label + ':markers'] = []
        output = runner.json('detect-stacks', cwd=cwd, environment={**environment, 'AST_INDEX_ROOT': root})
        actual[label + ':markers'] = output.get('stacks')
    runner.command('rebuild', '--force', cwd=invoker, environment=environment)
    for label, flags, env in (('explicit', [], {}), ('explicit-over-walk-up', ['--walk-up'],
                                                      {'AST_INDEX_WALK_UP': '1'})):
        expected[label + ':declarations'] = [('TargetOnly', 'TargetOnly.java', 1)]
        output = runner.json(*flags, 'class', '--pattern', '*Only', '--limit', '100', cwd=invoker,
                             environment={**environment, **env})
        actual[label + ':declarations'] = sorted((r['name'], r['path'], r['line']) for r in output['items'])
        expected[label + ':total'] = 1
        actual[label + ':total'] = output['pagination']['total']
    for label, root in (('missing', target / 'missing'), ('file', target / 'TargetOnly.java')):
        expected[label + ':rejected'] = 1
        actual[label + ':rejected'] = runner.command('detect-stacks', cwd=target,
            environment={**environment, 'AST_INDEX_ROOT': str(root)}, acceptable=(0, 1))[0]
    expected['version-independent-of-root'] = 0
    actual['version-independent-of-root'] = runner.command('version', cwd=target,
        environment={'AST_INDEX_ROOT': str(target / 'missing')})[0]
    return expected, actual


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('root fixture artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='roots-', dir=base)).resolve()
    runner = Runner(binary, directory)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
    expected['global:explicit-root'], actual['global:explicit-root'] = explicit_root_samples(
        binary, directory / 'explicit-root')
    sources = {'project/Main.java': 'primary', 'project/A0.java': 'first',
               'project/A1.java': 'second', 'named/Main.java': 'named', 'legacy/Main.java': 'legacy'}
    for path, package in sources.items():
        destination = directory / path
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(SOURCE.format(package=package))
    (runner.root / '.git').mkdir()
    runner.command('rebuild')
    runner.command('add-root', '../legacy')
    runner.command('add-root', directory / 'legacy')
    expected['add-root']['canonical-deduplication'] = [('legacy', 'legacy')]
    actual['add-root']['canonical-deduplication'] = [(name, path) for name, path, _ in runner.roots()]
    runner.command('add-root', '.', acceptable=(0,))
    expected['add-root']['overlap-refused'] = [('legacy', 'legacy')]
    actual['add-root']['overlap-refused'] = [(name, path) for name, path, _ in runner.roots()]
    (runner.root / 'inner').mkdir()
    runner.command('add-root', 'inner', '--force')
    expected['add-root']['forced-overlap'] = [('inner', 'project/inner'), ('legacy', 'legacy')]
    actual['add-root']['forced-overlap'] = [(name, path) for name, path, _ in runner.roots()]
    runner.command('remove-root', 'inner')
    expected['subtree']['forced-overlap'] = {'name': 'inner', 'canonical_path': 'project/inner', 'original_path': 'inner'}
    attached = runner.json('subtree', 'add', 'inner', 'inner', '--force')
    actual['subtree']['forced-overlap'] = {**attached, 'canonical_path': runner.path(attached['canonical_path'])}
    runner.json('subtree', 'remove', 'inner')
    expected['subtree']['attached'] = {'name': 'named', 'canonical_path': 'named', 'original_path': '../named'}
    attached = runner.json('subtree', 'add', 'named', '../named')
    actual['subtree']['attached'] = {**attached, 'canonical_path': runner.path(attached['canonical_path'])}
    runner.command('subtree', 'add', 'named', '../legacy')
    runner.command('subtree', 'add', 'alias', directory / 'named')
    runner.command('subtree', 'add', 'overlap', '.')
    expected['subtree']['duplicate-and-overlap-refused'] = [('legacy', 'legacy'), ('named', 'named')]
    actual['subtree']['duplicate-and-overlap-refused'] = [(name, path) for name, path, _ in runner.roots()]
    alias = directory / 'alias'
    alias.symlink_to(directory / 'named', target_is_directory=True)
    runner.command('add-root', alias)
    expected['add-root']['symlink-deduplication'] = [('legacy', 'legacy'), ('named', 'named')]
    actual['add-root']['symlink-deduplication'] = [(name, path) for name, path, _ in runner.roots()]
    alternate = directory / 'alternate' / 'legacy'
    alternate.mkdir(parents=True)
    runner.command('add-root', alternate)
    expected['add-root']['name-allocation'] = [('legacy', 'legacy'), ('legacy-2', 'alternate/legacy'), ('named', 'named')]
    actual['add-root']['name-allocation'] = [(name, path) for name, path, _ in runner.roots()]
    runner.command('remove-root', alternate)
    runner.command('rebuild')
    declarations = sorted((path, f'fixture.{package}.RootProbe', 3) for path, package in sources.items())
    for feature in ('add-root', 'subtree'):
        expected[feature]['rebuild-declarations'] = declarations
        actual[feature]['rebuild-declarations'] = runner.classes()
    runner.command('update')
    expected['subtree']['update-preserves-roots'] = declarations
    actual['subtree']['update-preserves-roots'] = runner.classes()
    wanted, observed = runner.scope([], sorted(sources))
    expected['subtree']['combined-scope'], actual['subtree']['combined-scope'] = wanted, observed
    primary = sorted(path for path in sources if path.startswith('project/'))
    for feature, flags, paths in (
            ('global:local', ['--local'], primary),
            ('global:subtree', ['--subtree', 'named'], ['named/Main.java']),
            ('global:subtree', ['--subtree', 'legacy'], ['legacy/Main.java']),
            ('global:subtree', ['--subtree', 'absent'], [])):
        label = flags[-1]
        wanted, observed = runner.scope(flags, paths)
        expected[feature][label], actual[feature][label] = wanted, observed
    # Native cache discovery is a separate contract from directory scoping.
    nested = runner.root / 'nested'
    nested.mkdir()
    (nested / '.git').mkdir()
    (nested / 'Nested.java').write_text('class Nested {}\n')
    parent_db = runner.command('db-path')[1].strip()
    expected['global:walk-up']['marker-stops-default'] = True
    actual['global:walk-up']['marker-stops-default'] = runner.command('db-path', cwd=nested)[1].strip() != parent_db
    for label, flags, environment in (('flag', ['--walk-up'], {}),
                                       ('environment', [], {'AST_INDEX_WALK_UP': '1'}),
                                       ('false-environment', [], {'AST_INDEX_WALK_UP': 'false'})):
        expected['global:walk-up'][label] = label != 'false-environment'
        actual['global:walk-up'][label] = runner.command(*flags, 'db-path', cwd=nested,
                                                       environment=environment)[1].strip() == parent_db
    expected['global:walk-up']['parent-query-directory-scope'] = []
    actual['global:walk-up']['parent-query-directory-scope'] = runner.json('--walk-up', 'class', 'RootProbe', cwd=nested)['items']
    runner.command('rebuild', cwd=nested)
    expected['global:walk-up']['nearest-existing-index'] = True
    actual['global:walk-up']['nearest-existing-index'] = runner.command('--walk-up', 'db-path', cwd=nested)[1].strip() != parent_db
    # Mutations without walk-up must remain in the current project.
    expected['global:walk-up']['nested-declarations'] = ['Nested']
    actual['global:walk-up']['nested-declarations'] = [row['name'] for row in runner.json('class', 'Nested', cwd=nested)['items']]
    runner.command('remove-root', '../legacy')
    runner.command('remove-root', '../legacy')
    runner.command('rebuild')
    remaining = sorted(row for row in declarations if not row[0].startswith('legacy/'))
    expected['remove-root'] = {'registered': [('named', 'named')], 'after-rebuild': remaining}
    actual['remove-root'] = {'registered': [(name, path) for name, path, _ in runner.roots()],
                             'after-rebuild': runner.classes()}
    expected['subtree']['detached'] = {'name': 'named', 'removed': True}
    actual['subtree']['detached'] = runner.json('subtree', 'remove', 'named')
    expected['subtree']['missing-detach'] = {'name': 'named', 'removed': False}
    actual['subtree']['missing-detach'] = runner.json('subtree', 'remove', 'named')
    runner.command('rebuild')
    expected['subtree']['after-detach-rebuild'] = sorted(row for row in declarations if row[0].startswith('project/'))
    actual['subtree']['after-detach-rebuild'] = runner.classes()
    return expected, actual
