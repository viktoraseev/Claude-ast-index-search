"""Authored Java navigation rendering and pages; independent source/state.

This family does not establish MCP equivalence or close the parent format
contract for search, exploration, management or lifecycle commands.
"""
from collections import Counter
import json
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner


FEATURES = {'global:format:java-navigation'}
REASON = ('independent source/state: disposable Java declaration/reference identities, '
          'hierarchy parents/children, JSON/text rendering, missing inputs, limits and '
          'root ownership; not MCP equivalence')
SOURCE = '''package fixture.{owner};
interface Parent {{}}
class Node implements Parent {{
    void ping() {{}}
    void use() {{ ping(); }}
}}
class Child extends Node {{}}
// lexicalOnly
'''
FILENAME = 'src/Node "λ".java'
IMPORT_FILE = 'src/Imports.java'
IMPORT_SOURCE = '''package fixture.{owner};
import java.util.List;
class Imports {{ List<String> values; }}
'''


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-navigation-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views, module diagrams and class/symbol/'
                       'implementations/hierarchy/refs/usages formats have separate executed '
                       'contracts; search, callers/call-tree, exploration, analysis, management '
                       'and lifecycle formats remain unresolved',))


def document(output):
    try:
        return json.loads(output)
    except ValueError:
        return None


def identity(row, runner, declaration):
    if not isinstance(row, dict):
        return ('invalid',)
    try:
        path = row['path']
        if not isinstance(path, str) or path.startswith('['):
            return ('invalid-path',)
        path = runner.path(path)
    except (KeyError, TypeError, ValueError, ToolError):
        return ('invalid-path',)
    if type(row.get('line')) is not int or row['line'] < 1:
        return ('invalid-line',)
    if declaration:
        return (path, row['line'], row.get('qualified_name'), row.get('kind'))
    return (path, row['line'])


def page(rows, candidates, pagination, limit):
    """Limited pages may select any valid candidates, preserving multiplicity."""
    observed, available = Counter(rows), Counter(candidates)
    if pagination is not None and not (
            isinstance(pagination, dict) and set(pagination) == {'total', 'returned', 'limit', 'truncated'}
            and all(type(pagination[k]) is int and pagination[k] >= 0 for k in ('total', 'returned', 'limit'))
            and type(pagination['truncated']) is bool):
        pagination = {'invalid': True}
    return {'identities': not bool(observed - available), 'returned': observed.total(),
            'complete': limit < available.total() or observed == available,
            'pagination': pagination}


def expected_page(candidates, limit):
    count = min(limit, len(candidates))
    return {'identities': True, 'returned': count, 'complete': True,
            'pagination': {'total': len(candidates), 'returned': count,
                           'limit': limit, 'truncated': count < len(candidates)}}


def text_rows(output, runner, declaration, indent='  '):
    rows = []
    for line in output.splitlines():
        pattern = (re.escape(indent) + r'(.+) \[(\w+)\]: (.+\.java):(\d+)' if declaration else
                   re.escape(indent) + r'(.+\.java):(\d+)')
        match = re.fullmatch(pattern, line)
        if match:
            rows.append((runner.path(match[3]), int(match[4]), match[1], match[2]) if declaration else
                        (runner.path(match[1]), int(match[2])))
    return rows


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('navigation format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='navigation-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    for owner in ('project', 'attached'):
        path = directory / owner / FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(SOURCE.format(owner=owner))
        (path.parent / 'Imports.java').write_text(IMPORT_SOURCE.format(owner=owner))
    (runner.root / '.git').mkdir()
    expected, actual = {}, {}
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        for owner in ('project', 'attached'):
            mobile_contracts.inventory(state, directory / owner)
            expected['inventory:' + owner] = 2
            actual['inventory:' + owner] = state.execute(
                "SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0]
            # Retain both full inventories, rather than an absence claim based
            # on a Java-only glob. Only authored Java sources are executed.
            state.execute('CREATE TABLE inventory_' + owner + ' AS SELECT * FROM file_inventory')
    finally:
        state.close()

    indexed_commands = [('class', 'Node'), ('symbol', 'ping'),
                        ('implementations', 'Parent'), ('hierarchy', 'Parent'), ('refs', 'ping')]
    for command, query in indexed_commands:
        code, output = runner.command('--format', 'json', command, query, acceptable=(0, 1))
        expected['missing-index:' + command] = {'code': 1, 'stdout': ''}
        actual['missing-index:' + command] = {'code': code, 'stdout': output}
        code, output = runner.command('--format', 'text', command, query)
        expected['missing-index-text:' + command] = {'code': 0, 'stdout': "Index not found. Run 'ast-index rebuild' first.\n"}
        actual['missing-index-text:' + command] = {'code': code, 'stdout': output}
    # Usages deliberately supports a source fallback before an index exists.
    value = runner.json('usages', 'lexicalOnly', '--limit', 100)
    expected['unindexed-usages'] = expected_page([('project/' + FILENAME, 8)], 100)
    actual['unindexed-usages'] = page([identity(row, runner, False) for row in value['items']],
                                    [('project/' + FILENAME, 8)], value.get('pagination'), 100)
    runner.command('rebuild', '--force')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force')

    scopes = [('local', ['--local'], ['project']),
              ('attached', ['--subtree', 'attached'], ['attached']),
              ('combined', [], ['project', 'attached'])]
    for scope, flags, owners in scopes:
        def declarations(name, kind, line):
            return [(f'{owner}/{FILENAME}', line, f'fixture.{owner}.{name}', kind) for owner in owners]
        for command, query, candidates, declaration in [
                ('class', 'Node', declarations('Node', 'class', 3), True),
                ('symbol', 'ping', declarations('Node.ping', 'function', 4), True),
                ('implementations', 'Parent', declarations('Node', 'class', 3), True),
                ('usages', 'ping', [(f'{o}/{FILENAME}', 5) for o in owners], False),
                ('usages', 'lexicalOnly', [(f'{o}/{FILENAME}', 8) for o in owners], False)]:
            for absent in (False, True):
                wanted = [] if absent else candidates
                needle = 'AbsentContractMatch' if absent else query
                for limit in (0, 1, 100):
                    key = f'{scope}:{command}:{query}:{absent}:{limit}'
                    for format in ('json', 'text'):
                        _, output = runner.command(*flags, command, needle, '--limit', limit, '--format', format)
                        expected[key + ':' + format + ':ansi'] = False
                        actual[key + ':' + format + ':ansi'] = '\x1b' in output
                        if format == 'json':
                            value = document(output)
                            valid = (isinstance(value, dict) and type(value.get('schema_version')) is int
                                     and value.get('schema_version') == 2 and isinstance(value.get('items'), list))
                            expected[key + ':json:valid'] = True
                            actual[key + ':json:valid'] = valid
                            rows = [identity(row, runner, declaration) for row in value['items']] if valid else []
                            pagination = value.get('pagination') if valid else None
                        else:
                            rows = text_rows(output, runner, declaration)
                            header = re.search(r'\(showing (\d+) of (\d+)\):', output)
                            expected[key + ':text:valid'] = True
                            actual[key + ':text:valid'] = bool(header)
                            pagination = ({'total': int(header[2]), 'returned': int(header[1]), 'limit': limit,
                                           'truncated': int(header[2]) > int(header[1])} if header else None)
                        expected[key + ':' + format] = expected_page(wanted, limit)
                        actual[key + ':' + format] = page(rows, wanted, pagination, limit)

        for needle in ('ping', 'List', 'AbsentContractMatch'):
            absent = needle == 'AbsentContractMatch'
            for limit in (0, 1, 100):
                wanted = {'definitions': [] if absent else declarations('Node.ping', 'function', 4),
                          'imports': [], 'usages': [] if absent else [(f'{o}/{FILENAME}', 5) for o in owners]}
                if needle == 'List':
                    wanted = {'definitions': [], 'imports': [(f'{o}/{IMPORT_FILE}', 2) for o in owners],
                              'usages': [(f'{o}/{IMPORT_FILE}', 3) for o in owners]}
                for format in ('json', 'text'):
                    _, output = runner.command(*flags, 'refs', needle, '--limit', limit, '--format', format)
                    key = f'{scope}:refs:{needle}:{limit}:{format}'
                    value = document(output) if format == 'json' else None
                    expected[key + ':valid'] = True
                    actual[key + ':valid'] = (isinstance(value, dict) and type(value.get('schema_version')) is int and value.get('schema_version') == 2
                                              if format == 'json' else output.startswith(f"Cross-references for '{needle}':\n"))
                    for section, candidates in wanted.items():
                        if format == 'json':
                            rows = value.get(section, []) if isinstance(value, dict) else []
                            identities = [identity(row, runner, section == 'definitions') for row in rows]
                            pagination = value.get('pagination', {}).get(section) if isinstance(value, dict) else None
                        else:
                            match = re.search(r'\n  ' + section.title() + r' \(showing (\d+) of (\d+)\):\n(.*?)(?=\n  (?:Definitions|Imports|Usages) |\Z)', output, re.S)
                            identities = text_rows(match[3], runner, section == 'definitions', '    ') if match else []
                            # Empty text sections are omitted, including a
                            # zero limit; full metadata is asserted in JSON.
                            pagination = None if not match else {
                                'total': int(match[2]), 'returned': int(match[1]), 'limit': limit,
                                'truncated': int(match[2]) > int(match[1])}
                        expected[key + ':' + section] = expected_page(candidates, limit)
                        if format == 'text' and (not candidates or limit == 0):
                            expected[key + ':' + section]['pagination'] = None
                        actual[key + ':' + section] = page(identities, candidates, pagination, limit)

        for query in (['Parent', 'Node', 'AbsentContractMatch'] if len(owners) == 1 else ['Parent', 'AbsentContractMatch']):
            absent = query == 'AbsentContractMatch'
            candidates = [] if absent else declarations('Node' if query == 'Parent' else 'Child', 'class', 3 if query == 'Parent' else 7)
            targets = [] if absent else declarations(query, 'interface' if query == 'Parent' else 'class', 2 if query == 'Parent' else 3)
            parents = [{'name': 'Parent', 'kind': 'implements'}] if query == 'Node' else []
            for limit in (0, 1, 100):
                for format in ('json', 'text'):
                    _, output = runner.command('--format', format, *flags, 'hierarchy', query, '--limit', limit)
                    key = f'{scope}:hierarchy:{query}:{limit}:{format}'
                    expected[key + ':ansi'] = False
                    actual[key + ':ansi'] = '\x1b' in output
                    if format == 'json':
                        value = document(output)
                        valid = (isinstance(value, dict) and type(value.get('schema_version')) is int
                                 and value.get('schema_version') == 2 and value.get('query') == query)
                        expected[key + ':valid'] = True
                        actual[key + ':valid'] = valid
                        expected[key + ':target'] = True
                        actual[key + ':target'] = (value.get('target') is None if absent else identity(value.get('target'), runner, True) in targets) if valid else False
                        expected[key + ':parents'] = parents
                        actual[key + ':parents'] = value.get('parents') if valid else None
                        expected[key + ':skipped'] = 'not_found' if absent else None
                        actual[key + ':skipped'] = value.get('skipped') if valid else 'invalid'
                        rows = [identity(row, runner, True) for row in value.get('children', [])] if valid else []
                        expected[key + ':children'] = expected_page(candidates, limit)
                        actual[key + ':children'] = page(rows, candidates, value.get('pagination') if valid else None, limit)
                    else:
                        rows = [(runner.path(m[3]), m[1], m[2]) for m in re.finditer(r'^    (.+) \[(\w+)\]: (.+\.java)$', output, re.M)]
                        expected[key + ':children'] = expected_page(candidates, limit)
                        expected[key + ':children']['pagination'] = None
                        actual[key + ':children'] = page(rows, [(p, n, k) for p, _, n, k in candidates], None, limit)
                        expected[key + ':parents'] = [(p['name'], p['kind']) for p in parents]
                        actual[key + ':parents'] = re.findall(r'^    (.+) \((\w+)\)$', output, re.M)
                        expected[key + ':heading'] = True
                        actual[key + ':heading'] = output.startswith(f"Class '{query}' not found.\n" if absent else f"Hierarchy for '{query}':\n")
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
