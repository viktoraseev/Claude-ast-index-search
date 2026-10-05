"""Read-only Java analysis/management rendering; no MCP equivalence claim.

Query and schema retain their format-independent programmatic JSON contract.
DB introspection expectations are internal consistency evidence, while unused
declarations and root ownership come from the authored Java source below.
"""
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import tomllib

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

ANALYSIS = 'global:format:java-analysis'
MANAGEMENT = 'global:format:java-management-read-only'
FEATURES = {ANALYSIS, MANAGEMENT}
REASONS = {
    ANALYSIS: 'independent source/state: disposable javac-validated Java unused-symbol identities, '
              'export selection, limits, empty/missing index and text/JSON rendering; not MCP equivalence',
    MANAGEMENT: 'internal CLI/DB: disposable Java stats/schema/query values and metadata, '
                'format-independent programmatic query/schema JSON, version/db-path JSON/text, '
                'source-owned root listings and empty/missing index; not MCP equivalence',
}
MISSING = "Index not found. Run 'ast-index rebuild' first.\n"
SOURCE = '''package fixture.{owner};
class FormatProbe {{
 void UpperUnused() {{}}
 void used() {{}}
 void consume() {{ used(); }}
}}
'''


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-analysis-management-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASONS[feature]))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java text-search, file views, module diagrams, navigation, callers/call-tree, '
                       'literal/ranked search, intent fallback/exploration and read-only analysis/management '
                       'formats have separate executed contracts; lifecycle and management mutation '
                       'formats remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('management format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='management-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    runner.environment['AST_INDEX_DB_PATH'] = str(directory / 'index.sqlite')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    def command(format, *args):
        code, output = runner.command('--format', format, *args, acceptable=(0, 1))
        record(MANAGEMENT, 'ansi:' + str(runner.sequence), False, '\x1b' in output)
        return code, output

    def decode(output):
        try:
            return json.loads(output)
        except ValueError:
            return '<invalid-json>'

    for owner in ('project', 'attached'):
        root = directory / owner
        root.mkdir()
        (root / 'Probe.java').write_text(SOURCE.format(owner=owner))
        # Applicability must inventory all types, even ignored directories.
        (root / 'build').mkdir()
        (root / 'build/Inventory.kt').write_text('// inventory only\n')
        (root / 'descriptor.xml').write_text('<fixture/>\n')
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, root)
            inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
            for feature in FEATURES:
                record(feature, 'inventory:' + owner, {'.java': 1, '.kt': 1, '.xml': 1}, inventory)
            if inventory != {'.java': 1, '.kt': 1, '.xml': 1}:
                raise ToolError('management fixture full inventory incomplete')
        finally:
            state.close()
    (runner.root / '.git').mkdir()
    sources = [directory / owner / 'Probe.java' for owner in ('project', 'attached')]
    with (directory / 'javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(directory / 'javac'), *map(str, sources)],
                                stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('management fixture javac validation failed; see private log')
    version = tomllib.loads((boundary.parent / 'Cargo.toml').read_text())['package']['version']
    database = directory / 'index.sqlite'
    for phase in ('missing', 'populated', 'empty'):
        if phase == 'populated':
            runner.command('rebuild', '--force')
            runner.json('subtree', 'add', 'attached-label', '../attached')
            runner.command('rebuild', '--force')
        elif phase == 'empty':
            for source in sources:
                source.unlink()
            runner.command('rebuild', '--force')
        for format in ('text', 'json'):
            code, output = command(format, 'version')
            want = {'name': 'ast-index', 'version': version} if format == 'json' else f'ast-index v{version}\n'
            record(MANAGEMENT, phase + ':version:' + format, (0, want),
                   (code, decode(output) if format == 'json' else output))
            code, output = command(format, 'db-path')
            want = {'db_path': str(database)} if format == 'json' else str(database) + '\n'
            record(MANAGEMENT, phase + ':db-path:' + format, (0, want),
                   (code, decode(output) if format == 'json' else output))
        if phase == 'missing':
            for feature, args in [(ANALYSIS, ['unused-symbols']), (MANAGEMENT, ['stats']),
                                  (MANAGEMENT, ['list-roots']), (MANAGEMENT, ['subtree', 'list']),
                                  (MANAGEMENT, ['schema']), (MANAGEMENT, ['query', 'SELECT 1'])]:
                for format in ('text', 'json'):
                    code, output = command(format, *args)
                    # Programmatic query/schema already report missing DB as an error.
                    want = (1, '') if format == 'json' or args[0] in ('query', 'schema') else (0, MISSING)
                    record(feature, 'missing:' + ':'.join(args) + ':' + format, want, (code, output))
            record(MANAGEMENT, 'missing:no-database-created', False, database.exists())
            continue

        def identity(row):
            return runner.path(row['path']), row['name'], row['kind'], row['line']

        for exports in (False, True):
            candidates = []
            if phase == 'populated':
                for owner in ('project', 'attached'):
                    candidates.extend([(owner + '/Probe.java', 'FormatProbe', 'class', 2),
                                       (owner + '/Probe.java', 'UpperUnused', 'function', 3)])
                    if not exports:
                        candidates.append((owner + '/Probe.java', 'consume', 'function', 5))
                candidates.sort(key=lambda row: (row[3], row[0].split('/')[0]))
            for limit in (0, 1, 100):
                args = ['unused-symbols', '--limit', limit, *(['--export-only'] if exports else [])]
                code, output = command('json', *args)
                doc = decode(output)
                rows = [identity(row) for row in doc] if isinstance(doc, list) else doc
                key = f'{phase}:unused:{exports}:{limit}'
                record(ANALYSIS, key + ':json', (0, candidates[:limit]), (code, rows))
                code, output = command('text', *args)
                rows = [(runner.path(p), n, k, int(line)) for n, k, p, line in
                        re.findall(r'^  (.+) \[([^]]+)\]: (.+\.java):(\d+)$', output, re.M)]
                header = re.search(r'\((\d+)/(\d+) checked\):', output)
                record(ANALYSIS, key + ':text',
                       (0, candidates[:limit], [min(limit, len(candidates)),
                                              (4 if exports else 8) if phase == 'populated' else 0],
                        not candidates[:limit]),
                       (code, rows, list(map(int, header.groups())) if header else None,
                        'No unused symbols found.' in output))

        for args in (['list-roots'], ['subtree', 'list']):
            key = phase + ':' + ':'.join(args)
            want = [{'name': 'attached-label', 'canonical_path': str(directory / 'attached'),
                     'original_path': '../attached'}]
            code, output = command('json', *args)
            record(MANAGEMENT, key + ':json', (0, want), (code, decode(output)))
            code, output = command('text', *args)
            record(MANAGEMENT, key + ':text',
                   (0, f'Subtrees attached to this project:\n  {runner.root} (primary)\n'
                       f'  attached-label  ../attached ({directory / "attached"})\n'), (code, output))

        # These expectations intentionally use the isolated DB, not an MCP oracle.
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as db:
            tables = {'file_count': 'files', 'symbol_count': 'symbols', 'refs_count': 'refs',
                      'module_count': 'modules', 'xml_usages_count': 'xml_usages', 'resources_count': 'resources',
                      'storyboard_usages_count': 'storyboard_usages', 'ios_assets_count': 'ios_assets'}
            stats = {key: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for key, table in tables.items()}
            project = db.execute("SELECT value FROM metadata WHERE key='project_label'").fetchone()
            project = project[0] if project else None
            schema = {}
            for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name"):
                schema[table] = {'columns': [{'name': c[1], 'type': c[2], 'not_null': bool(c[3]), 'primary_key': bool(c[5])}
                                             for c in db.execute(f'PRAGMA table_info("{table}")')],
                                 'row_count': db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]}
        code, output = command('json', 'stats')
        doc = decode(output)
        record(MANAGEMENT, phase + ':stats:json',
               (0, stats, str(database), True, project),
               (code, doc.get('stats'), doc.get('db_path'),
                type(doc.get('db_size_bytes')) is int and doc['db_size_bytes'] == database.stat().st_size,
                doc.get('project')) if isinstance(doc, dict) else (code, doc))
        code, output = command('text', 'stats')
        want = ('Index Statistics:\n' + (f'  Project:    {project}\n' if project is not None else '') +
                ''.join(f'  {label + ":":<12}{stats[key]}\n'
                for label, key in [('Files', 'file_count'), ('Symbols', 'symbol_count'), ('Refs', 'refs_count'), ('Modules', 'module_count')]) +
                f'  DB size:    {database.stat().st_size / 1024 / 1024:.2f} MB\n  DB path:    {database}\n'
                f'\n  Extra roots:\n    {directory / "attached"}\n')
        record(MANAGEMENT, phase + ':stats:text', (0, want), (code, output))
        for format in ('json', 'text'):
            code, output = command(format, 'schema')
            record(MANAGEMENT, phase + ':schema:' + format, (0, schema), (code, decode(output)))
            for limit in (0, 1, 100):
                sql = 'SELECT path FROM files ORDER BY path,root_path'
                rows = [{'path': 'Probe.java'}] * 2 if phase == 'populated' else []
                code, output = command(format, 'query', sql, '--limit', limit)
                record(MANAGEMENT, f'{phase}:query:{format}:{limit}',
                       (0, {'columns': ['path'], 'count': len(rows[:limit]), 'rows': rows[:limit]}),
                       (code, decode(output)))
            sql = "SELECT NULL AS n, 7 AS i, 1.5 AS r, 'quote\"\tline\nλ' AS t, X'0102' AS b"
            code, output = command(format, 'query', sql)
            record(MANAGEMENT, phase + ':query-values:' + format,
                   (0, {'columns': ['n', 'i', 'r', 't', 'b'], 'count': 1,
                        'rows': [{'n': None, 'i': 7, 'r': 1.5, 't': 'quote"\tline\nλ', 'b': '<blob 2 bytes>'}]}),
                   (code, decode(output)))
            for sql in ('SELECT * FROM absent_fixture_table', 'DELETE FROM files'):
                code, output = command(format, 'query', sql)
                record(MANAGEMENT, phase + ':query-error:' + sql + ':' + format, (1, ''), (code, output))
    return expected, actual
