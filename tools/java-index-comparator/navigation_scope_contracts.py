"""Literal Java navigation selectors on authored, disposable multi-root sources.

This is independent source/state coverage. SQL rows are never the expectation,
and the shared scope matrix keeps its unrelated unresolved families pending.
"""
from collections import Counter
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner


FEATURES = {'global:scope:java-navigation'}
REASON = ('independent source/state: disposable Java literal combined file/module/'
          'cwd filters, root ownership, navigation/search/reference pages and text/JSON '
          'identities; not MCP equivalence')
GAP = ('Java file views and class/symbol/search/implementations/refs/usages/hierarchy '
       'combined path/root filters have separate executed contracts; caller/call-tree '
       'selector composition and module/map/analysis/graph/conventions/explore scope '
       'remain unresolved')
SOURCE = '''package fixture.{package};
class RootProbe extends ProbeBase {{
    void ping() {{}}
    void use() {{ ping(); }}
}}
class ProbeBase {{}}
// lexicalOnly
'''


def plan_scope(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-navigation-scope'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:scope-command-matrix' AND status='pending'",
                      (GAP,))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('navigation scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='navigation-scope-', dir=base)).resolve()
    runner = Runner(binary, directory)
    # Each collision is a distinct Java package; filenames may differ from
    # package-private class names without making the authored source invalid.
    relative = ['src/scope_/RootProbe_.java', 'src/scopeX/RootProbeX.java',
                'src/scope%/RootProbe%.java', 'src/scopeMany/RootProbeMany.java',
                'src/case/RootProbeCase.java', 'src/scope_/RootProbe\\.java']
    paths = []
    for owner in ('project', 'attached'):
        for number, path in enumerate(relative):
            dest = directory / owner / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(SOURCE.format(package=f'{owner}.p{number}'))
            paths.append(f'{owner}/{path}')
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force')
    expected, actual = {}, {}
    inventory = connect(directory / 'inventory.sqlite')
    try:
        inventory.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        for owner in ('project', 'attached'):
            mobile_contracts.inventory(inventory, directory / owner)
            expected['applicable:' + owner] = len(relative)
            actual['applicable:' + owner] = inventory.execute(
                "SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0]
    finally:
        inventory.close()

    def page(key, rows, candidates, pagination, limit):
        candidates, rows = Counter(candidates), Counter(rows)
        expected[key] = {'valid': True, 'returned': min(limit, candidates.total()),
                         'complete': True, 'pagination': {
                             'total': candidates.total(), 'returned': min(limit, candidates.total()),
                             'limit': limit, 'truncated': limit < candidates.total()}}
        actual[key] = {'valid': not bool(rows - candidates), 'returned': rows.total(),
                       'complete': limit < candidates.total() or rows == candidates,
                       'pagination': pagination}

    variants = [('class', ['class', 'RootProbe'], 2),
                ('class-pattern', ['class', '--pattern', '*Probe'], 2),
                ('class-fuzzy', ['class', 'RootPr', '--fuzzy'], 2),
                ('symbol', ['symbol', 'ping', '--type', 'function'], 3),
                ('symbol-pattern', ['symbol', '--pattern', 'p*', '--type', 'function'], 3),
                ('symbol-fuzzy', ['symbol', 'pin', '--fuzzy', '--type', 'function'], 3),
                ('implementations', ['implementations', 'ProbeBase'], 2),
                ('usages', ['usages', 'ping'], 4),
                ('usages-fallback', ['usages', 'lexicalOnly'], 7)]
    # Predicate semantics are authored literal contains/starts-with, including
    # case and SQL wildcard characters. Empty selectors are literal no-ops.
    selectors = [('combined', 'RootProbe_', 'src/scope_', None),
                 ('percent', '%', 'src/scope%', None),
                 ('backslash', '\\', 'src/scope_', None),
                 ('case', 'rootprobe_', 'src/scope_', None),
                 ('module-case', 'RootProbe', 'SRC/scope_', None),
                 ('disjoint', 'RootProbeX', 'src/scope_', None),
                 ('empty', '', '', None),
                 ('cwd', 'RootProbe', None, 'src/scope_')]
    scopes = [('all', [], {'project', 'attached'}),
              ('local', ['--local'], {'project'}),
              ('attached', ['--subtree', 'attached'], {'attached'}),
              ('absent', ['--subtree', 'absent'], set())]
    for scope_name, flags, owners in scopes:
        for label, file_filter, module_filter, cwd_prefix in selectors:
            filters = ['--in-file', file_filter]
            if module_filter is not None:
                filters += ['--module', module_filter]
            cwd = runner.root / cwd_prefix if cwd_prefix else runner.root
            # Nested invocation shares the primary index explicitly; its cwd
            # still contributes a directory selector relative to every root.
            env = {'AST_INDEX_ROOT': str(runner.root)}
            candidates = [p for p in paths if p.split('/', 1)[0] in owners
                          and file_filter in p.split('/', 1)[1]
                          and (module_filter is None or p.split('/', 1)[1].startswith(module_filter))
                          and (cwd_prefix is None or p.split('/', 1)[1].startswith(cwd_prefix + '/'))]
            for limit in (0, 1, 100):
                prefix = f'{scope_name}:{label}:{limit}'
                for name, args, line in variants:
                    output = runner.json(*flags, *args, *filters, '--limit', limit, cwd=cwd, environment=env)
                    rows = [(runner.path(r['path']), r['line']) for r in output['items']]
                    page(prefix + ':' + name, rows, [(p, line) for p in candidates],
                         output['pagination'], limit)
                output = runner.json(*flags, 'refs', 'ping', *filters, '--limit', limit, cwd=cwd, environment=env)
                for section, line in [('definitions', 3), ('usages', 4), ('imports', None)]:
                    rows = [(runner.path(r['path']), r['line']) for r in output[section]]
                    page(prefix + ':refs:' + section, rows, [(p, line) for p in candidates] if line else [],
                         output['pagination'][section], limit)
                output = runner.json(*flags, 'search', 'RootProbe', '--type', 'class', *filters,
                                     '--limit', limit, cwd=cwd, environment=env)
                for section in ('symbols', 'files', 'content_matches'):
                    rows = [(runner.path(r if isinstance(r, str) else r['path']),
                             2 if isinstance(r, str) else r.get('line', 2)) for r in output[section]]
                    page(prefix + ':search:' + section, rows, [(p, 2) for p in candidates],
                         output['pagination'][section], limit)
            # One full text page checks real rendered identities too. It does
            # not establish unrelated navigation formats or semantic dispatch.
            for name, args, line in variants:
                _, output = runner.command(*flags, *args, *filters, '--limit', 100,
                                           cwd=cwd, environment=env)
                rows = [(runner.path(p), int(n)) for p, n in re.findall(
                    r'^  (?:[^\n]+ \[[^]]+\]: )?(.+\.java):(\d+)$', output, re.MULTILINE)]
                expected[f'{scope_name}:{label}:text:{name}'] = sorted((p, line) for p in candidates)
                actual[f'{scope_name}:{label}:text:{name}'] = sorted(rows)
            _, output = runner.command(*flags, 'hierarchy', 'ProbeBase', *filters, '--limit', 100,
                                       cwd=cwd, environment=env)
            rows = [runner.path(p) for p in re.findall(r'^    .+ \[class\]: (.+\.java)$', output, re.MULTILINE)]
            expected[f'{scope_name}:{label}:hierarchy'] = sorted(candidates)
            actual[f'{scope_name}:{label}:hierarchy'] = sorted(rows)
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
