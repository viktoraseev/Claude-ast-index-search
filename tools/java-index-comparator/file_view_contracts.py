"""Authored Java file views and rendering; independent source/state, not MCP."""
import json
from pathlib import Path
import re
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner


FEATURES = {'global:format:java-file-views'}
REASON = ('independent source/state: disposable Java file selection, syntax imports, '
          'outline and public API identities, snippets, empty/missing inputs and '
          'JSON/text rendering; not MCP equivalence or attached-root API coverage')
SOURCE = '''package fixture;
  import java.util.
      List;
import static java.util.Collections./* separator */emptyList;
import java.util.*; import static java.lang.Math.*;
// import example.Fake;
public class View {
    public View() {}
    public List<String> expose() { return emptyList(); }
    private void hidden() {}
    public String text = "import example.StringOnly; λ";
}
'''
IMPORTS = ['java.util.List', 'static java.util.Collections.emptyList',
           'java.util.*', 'static java.lang.Math.*']
OUTLINE = [('View', 'class', 7, 12), ('View', 'function', 8, 8),
           ('expose', 'function', 9, 9), ('hidden', 'function', 10, 10),
           ('text', 'property', 11, 11)]
API_LINES = [7, 8, 9, 11]


def plan_views(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-file-views'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search and file/outline/imports/api formats have separate executed '
                       'contracts; other Java command formats and invalid diagram-format selection '
                       'remain unresolved',))


def document(output):
    try:
        return json.loads(output)
    except ValueError:
        # Keep a format failure comparable without printing command payloads.
        return {'invalid_json': True}


def outline(output, format):
    if format == 'json':
        value = document(output)
        if not isinstance(value, dict) or not isinstance(value.get('symbols'), list):
            return {'invalid_outline': True}
        rows = [(row.get('name'), row.get('kind'), row.get('line'), row.get('end_line'))
                for row in value['symbols']]
        return {'file': value.get('file'), 'schema_version': value.get('schema_version'),
                'skipped': value.get('skipped'), 'rows': rows}
    rows = []
    for line in output.splitlines():
        if match := re.fullmatch(r'  :(\d+)(?:-(\d+))? (.+) \[(\w+)\]', line):
            rows.append((match[3], match[4], int(match[1]), int(match[2] or match[1])))
    return rows


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('file view fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='file-views-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    folder = runner.root / 'nested' / 'views'
    folder.mkdir(parents=True)
    filename = 'nested/views/View "λ".java'
    (runner.root / filename).write_text(SOURCE)
    (folder / 'Empty.java').write_text('// No declarations or imports.\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        count = state.execute("SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0]
        if count != 2:
            raise ToolError('applicable Java file view inventory incomplete')
    finally:
        state.close()
    expected, actual = {}, {}
    # JSON must never disguise an absent index as an empty successful search.
    code, output = runner.command('--format', 'json', 'file', '.java', acceptable=(0, 1))
    expected['file:missing-index'] = {'code': 1, 'stdout': ''}
    actual['file:missing-index'] = {'code': code, 'stdout': output}
    runner.command('rebuild', '--force')
    for format in ('json', 'text'):
        def run(*args):
            _, output = runner.command(*args, '--format', format)
            expected['ansi:' + ':'.join(map(str, args)) + ':' + format] = False
            actual['ansi:' + ':'.join(map(str, args)) + ':' + format] = '\x1b' in output
            return output
        for pattern, files in [('.java', ['nested/views/Empty.java', filename]),
                               ('View "λ".java', [filename]), ('Missing.java', [])]:
            for limit in (0, 1, 100):
                output = run('file', pattern, '--limit', limit)
                key = f'file:{pattern}:{limit}:{format}'
                selected = document(output) if format == 'json' else [
                    line[2:] for line in output.splitlines() if line.startswith('  ') and line.strip() != 'No files found.']
                valid = isinstance(selected, list) and all(isinstance(p, str) for p in selected)
                expected[key] = {'count': min(limit, len(files)), 'valid': True, 'complete': True}
                actual[key] = {'count': len(selected) if valid else None,
                               'valid': valid and len(set(selected)) == len(selected) and set(selected) <= set(files),
                               'complete': valid and (limit < len(files) or sorted(selected) == sorted(files))}
        for file, names, rows in [(filename, IMPORTS, OUTLINE),
                                  ('nested/views/Empty.java', [], []), ('Missing.java', [], [])]:
            missing = file == 'Missing.java'
            output = run('imports', file)
            key = f'imports:{file}:{format}'
            expected[key] = ({'file': file, 'imports': names, 'count': len(names),
                              **({'skipped': 'not_found'} if missing else {})} if format == 'json' else
                             ('File not found: Missing.java\n' if missing else
                              'Imports in ' + file + ':\n' + (''.join('  ' + name + ';\n' for name in names) +
                              f'\n  Total: {len(names)} imports\n' if names else '  No imports found.\n')))
            actual[key] = document(output) if format == 'json' else output
            for full in (False, True):
                output = run('outline', file, *(['--full'] if full else []))
                key = f'outline:{file}:{full}:{format}'
                expected[key] = ({'file': file, 'schema_version': 1,
                                  'skipped': 'not_found' if missing else None, 'rows': rows}
                                 if format == 'json' else rows)
                actual[key] = outline(output, format)
        for module, lines in [('nested/views', API_LINES), ('nested.views', API_LINES),
                               ('nested/views/Empty.java', []), ('missing', [])]:
            for limit in (0, 1, 100):
                items = [{'path': filename, 'line': n, 'content': SOURCE.splitlines()[n - 1].strip()[:100]}
                         for n in lines[:limit]]
                output = run('api', module, '--limit', limit)
                key = f'api:{module}:{limit}:{format}'
                missing = module == 'missing'
                expected[key] = ({'module': module, 'items': items, 'count': len(items),
                                  **({'skipped': 'not_found'} if missing else {})} if format == 'json' else
                                 ('Module not found: missing\n' if missing else
                                  f"Public API of '{module}' ({len(items)}):\n" +
                                  (''.join(f"  {row['path']}:{row['line']}\n    {row['content']}\n" for row in items)
                                   if items else '  No public API found.\n')))
                actual[key] = document(output) if format == 'json' else output
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
