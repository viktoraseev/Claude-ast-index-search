"""Java project insight rendering and delegated search flags, independent of MCP."""
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile

from common import ToolError, connect, stable_id
from delegate_contracts import STUB
import mobile_contracts
from root_contracts import Runner


INSIGHTS = 'global:format:java-project-insights'
DELEGATE = 'global:format:java-agrep'
FEATURES = {INSIGHTS, DELEGATE}
REASONS = {
    INSIGHTS: ('independent source/state: disposable Java map/conventions declarations, '
               'counts, ordered pages, JSON/text, missing/empty/corrupt index and missing source; '
               'not MCP equivalence'),
    DELEGATE: ('internal CLI: disposable Java agrep global-format/--json composition, '
               'delegate arguments, stdout, fallback and exit status; '
               'external parser and MCP equivalence not claimed'),
}
GAP = ('Java per-command format families, including map/conventions missing/error states '
       'and agrep global-format/--json composition, have separate executed contracts; '
       'ambiguous Java graph rendering and global-selector failure composition across '
       'remaining Java-applicable commands remain unresolved')
SOURCES = {
    'presentation/Alpha.java': 'import org.junit.Test; class AlphaService implements Marker {} interface Marker {} enum Tone { LIGHT }\n',
    'presentation/Beta.java': 'import org.junit.Test; class BetaService {}\n',
    'presentation/Gamma.java': 'import org.junit.Test; class GammaService {}\n',
    'domain/Domain.java': 'class Domain {}\n',
    'data/Data.java': 'class Data {}\n',
    'build/Inventory.kt': '// inventory only\n',
    'descriptor.xml': '<fixture/>\n',
}
DECLARATIONS = {
    'presentation/': [
        {'name': 'AlphaService', 'kind': 'class', 'parents': ['Marker'], 'file': 'Alpha.java'},
        {'name': 'BetaService', 'kind': 'class', 'file': 'Beta.java'},
        {'name': 'GammaService', 'kind': 'class', 'file': 'Gamma.java'},
        {'name': 'Marker', 'kind': 'interface', 'file': 'Alpha.java'},
        {'name': 'Tone', 'kind': 'enum', 'file': 'Alpha.java'},
    ],
    'data/': [{'name': 'Data', 'kind': 'class', 'file': 'Data.java'}],
    'domain/': [{'name': 'Domain', 'kind': 'class', 'file': 'Domain.java'}],
}
MISSING = "Index not found. Run 'ast-index rebuild' first.\n"


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-project-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASONS[feature]))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'", (GAP,))


def inventory(runner, sources):
    state = connect(runner.directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        observed = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if observed != {path: Path(path).suffix.lower() for path in sources}:
            raise ToolError('project format fixture inventory incomplete')
        return dict(sorted(Counter(observed.values()).items()))
    finally:
        state.close()


def map_document(mode, limit, per_dir, empty=False, absent=False):
    selected = [] if empty or absent else list(DECLARATIONS)[:limit]
    groups = []
    for path in selected:
        group = {'path': path, 'file_count': 3 if path == 'presentation/' else 1}
        if mode == 'summary':
            group['kinds'] = dict(Counter(row['kind'] for row in DECLARATIONS[path]))
        else:
            group['symbols'] = DECLARATIONS[path][:per_dir]
        groups.append(group)
    document = {'file_count': 0 if empty else 5, 'module_count': 0, 'groups': groups}
    if mode == 'summary':
        document.update(showing=len(groups), total_dirs=0 if empty else 3)
    return document


def map_text(document, mode):
    header = f"{document['file_count']} files | 0 modules"
    if mode == 'summary':
        header += f" | top {document['showing']} of {document['total_dirs']} dirs"
    output = header + '\n\n'
    for group in document['groups']:
        if mode == 'summary':
            kinds = ', '.join(f"{group['kinds'][kind]} {label}" for kind, label in
                              [('class', 'cls'), ('interface', 'iface'), ('enum', 'enum')]
                              if kind in group['kinds'])
            output += f"  {group['path']:60} {group['file_count']:>5} files | {kinds}\n"
        elif group['symbols']:
            output += f"{group['path']} ({group['file_count']} files)\n"
            for row in group['symbols']:
                parents = ' > ' + ', '.join(row['parents']) if row.get('parents') else ''
                output += f"  {row['name']} : {row['kind']}{parents}\n"
            output += '\n'
    if mode == 'summary' and document['total_dirs'] > document['showing']:
        output += f"\n  ... and {document['total_dirs'] - document['showing']} more dirs. Use --limit or --module <path> to drill down.\n"
    return output


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('project format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='project-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    for path, source in SOURCES.items():
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    for feature in FEATURES:
        record(feature, 'inventory', {'.java': 5, '.kt': 1, '.xml': 1}, inventory(runner, SOURCES))

    def run(format, args, suffix=False):
        flags = ['--format', format]
        code, output = runner.command(*(args + flags if suffix else flags + args), acceptable=(0, 1, 2))
        stderr = (directory / f'{runner.sequence:03d}.stderr.log').read_text()
        return code, output, stderr

    def failure(label, args, diagnostic):
        for format in ('json', 'text'):
            code, output, stderr = run(format, args)
            record(INSIGHTS, f'{label}:{args[0]}:{format}',
                   {'exit': 1, 'stdout': '', 'diagnostic': True},
                   {'exit': code, 'stdout': output, 'diagnostic': bool(stderr.strip()) and diagnostic in stderr})

    for args in (['map'], ['map', '--module', ''], ['conventions']):
        for format in ('json', 'text'):
            for suffix in (False, True):
                code, output, stderr = run(format, args, suffix)
                record(INSIGHTS, f'missing:{args}:{format}:{suffix}',
                       {'exit': 1 if format == 'json' else 0, 'stdout': '' if format == 'json' else MISSING,
                        'diagnostic': format == 'json'},
                       {'exit': code, 'stdout': output, 'diagnostic': MISSING.strip() in stderr})

    def indexed(empty=False):
        for mode in ('summary', 'detail', 'absent'):
            for limit, per_dir in ((0, 0), (1, 1), (100, 100)):
                args = ['map', '--limit', str(limit)]
                if mode != 'summary':
                    args += ['--module', '' if mode == 'detail' else 'missing/', '--per-dir', str(per_dir)]
                want = map_document(mode, limit, per_dir, empty, mode == 'absent')
                for format in ('json', 'text'):
                    code, output, stderr = run(format, args, suffix=limit == 1)
                    valid_label = True
                    if format == 'json':
                        try:
                            got = json.loads(output)
                            valid_label = got.pop('project', None) == 'Unknown'
                        except (ValueError, AttributeError):
                            got, valid_label = None, False
                    else:
                        valid_label = output.startswith('Project: Unknown | ')
                        got = output.removeprefix('Project: Unknown | ')
                    record(INSIGHTS, f'{empty}:map:{mode}:{limit}:{per_dir}:{format}',
                           {'exit': 0, 'value': want if format == 'json' else map_text(want, mode),
                            'label': True, 'stderr': '', 'ansi': False},
                           {'exit': code, 'value': got, 'label': valid_label, 'stderr': stderr, 'ansi': '\x1b' in output})
        conventions = {'architecture': [] if empty else ['Clean Architecture'],
                       'frameworks': {} if empty else {'Testing': [{'name': 'JUnit', 'count': 3}]},
                       'naming_patterns': [] if empty else [{'suffix': 'Service', 'count': 3}]}
        text = 'Project Conventions:\n\n'
        if not empty:
            text += 'Architecture: Clean Architecture\n\nTesting: JUnit (3)\n\nNaming Patterns:\n  ' + f"{'Service':20} 3\n\n"
        for format in ('json', 'text'):
            code, output, stderr = run(format, ['conventions'], suffix=True)
            try:
                got = json.loads(output) if format == 'json' else output
            except ValueError:
                got = None
            record(INSIGHTS, f'{empty}:conventions:{format}',
                   {'exit': 0, 'value': conventions if format == 'json' else text, 'stderr': '', 'ansi': False},
                   {'exit': code, 'value': got, 'stderr': stderr, 'ansi': '\x1b' in output})

    runner.command('rebuild', '--force')
    indexed()
    # A source-based command must not present stale DB-only evidence as a
    # successful conventions result when its Java source disappears.
    (runner.root / 'presentation/Alpha.java').unlink()
    failure('missing-source', ['conventions'], 'No such file')
    for path in SOURCES:
        destination = runner.root / path
        if destination.exists():
            destination.unlink()
    runner.command('rebuild', '--force')
    indexed(empty=True)
    db_path = Path(runner.json('db-path')['db_path'])
    if not db_path.is_relative_to(directory):
        raise ToolError('project format index escaped artifact boundary')
    db_path.write_bytes(b'public invalid sqlite fixture')
    for args in (['map'], ['conventions']):
        # The availability API treats an unrecognizable cache as unavailable.
        # Preserve the text rebuild hint; JSON must still fail on stderr.
        for format in ('json', 'text'):
            code, output, stderr = run(format, args)
            record(INSIGHTS, f'corrupt-index:{args[0]}:{format}',
                   {'exit': 1 if format == 'json' else 0, 'stdout': '' if format == 'json' else MISSING,
                    'diagnostic': format == 'json'},
                   {'exit': code, 'stdout': output, 'diagnostic': MISSING.strip() in stderr})

    # Use the existing synthetic provider, with no real external program on PATH.
    (runner.root / 'Probe.java').write_text('class Probe { void probe() {} }\n')
    scripts = directory / 'bin'
    scripts.mkdir()
    # Output depends on the delegated flag as well as the exit status.
    script = STUB.format(python=sys.executable).replace(
        "print(os.environ['AUDIT_DELEGATE_OUTPUT'])", '''
if '--json=compact' in sys.argv:
    print('[]' if os.environ['AUDIT_DELEGATE_EXIT'] == '1' else '[{"name":"probe"}]')
else:
    print('no matches' if os.environ['AUDIT_DELEGATE_EXIT'] == '1' else 'probe match')
''')
    for name in ('sg', 'ast-grep'):
        path = scripts / name
        path.write_text(script)
        path.chmod(0o755)
    log = directory / 'external.jsonl'
    runner.environment.update(PATH=str(scripts), AUDIT_DELEGATE_LOG=str(log))
    pattern = 'probe($$$)'
    for format in ('text', 'json'):
        for explicit in (False, True):
            for suffix in (False, True):
                json_mode = explicit or format == 'json'
                args = ['agrep', pattern, '--lang', 'java'] + (['--json'] if explicit else [])
                for label, sg, alternate, external_exit, provider, exit_code in (
                        ('matches', 0, 0, 0, 'sg', 0), ('empty', 0, 0, 1, 'sg', 0),
                        ('error', 0, 0, 7, 'sg', 1), ('fallback', 7, 0, 0, 'ast-grep', 0),
                        ('unavailable', 127, 127, 0, None, 1)):
                    runner.environment.update(AUDIT_SG_VERSION=str(sg), AUDIT_AST_GREP_VERSION=str(alternate),
                        AUDIT_DELEGATE_EXIT=str(external_exit), AUDIT_DELEGATE_OUTPUT='')
                    before = len(log.read_text().splitlines()) if log.exists() else 0
                    code, output, stderr = run(format, args, suffix)
                    records = [json.loads(line) for line in log.read_text().splitlines()[before:]]
                    runs = [row for row in records if row[2:3] == ['run']]
                    want_args = ['run', '--pattern', pattern, '--lang', 'java'] + (['--json=compact'] if json_mode else [])
                    want_output = ('[]' if external_exit == 1 else '[{"name":"probe"}]') if json_mode else ('no matches' if external_exit == 1 else 'probe match')
                    record(DELEGATE, f'{format}:{explicit}:{suffix}:{label}',
                           {'exit': exit_code, 'runs': [[provider, str(runner.root), *want_args]] if provider else [],
                            'stdout': want_output + '\n' if provider else '', 'diagnostic': exit_code != 0},
                           {'exit': code, 'runs': runs, 'stdout': output, 'diagnostic': bool(stderr.strip())})
    return expected, actual
