"""Java lexical scan failure composition; internal CLI, never MCP equivalence."""
from pathlib import Path
import json
import os
import tempfile

from common import ToolError, connect, file_sha256, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-scan-errors'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent authored Java source: lexical read/walk '
          'failure propagation, source/DB preservation, scope and empty-page controls; '
          'full file-type inventory; not MCP equivalence')
FORMATS = ('text', 'json')
POSITIONS = (False, True)
INVENTORY = {'Probe.java': '.java', 'Inventory.kt': '.kt', 'inventory.xml': '.xml'}
# Each case reaches a Java-applicable production reader. Invalid bytes occur
# on its matching anchor, rather than on an unrelated/non-Java line.
CASES = {
    'todo': (['todo'], '// TODO scan-probe\n'),
    'annotations': (['annotations', 'Deprecated'], '@Deprecated\nclass Probe {}\n'),
    'deprecated': (['deprecated'], '@Deprecated\nclass Probe {}\n'),
    'suppress': (['suppress'], '@SuppressWarnings("scan-probe")\nclass Probe {}\n'),
    'deeplinks': (['deeplinks'], '// DeepLink scan-probe\n'),
    'inject': (['inject', 'Object'], 'class Probe { @Inject Object service; }\n'),
    'provides': (['provides', 'Object'], 'class Probe { @Provides Object provide() { return null; } }\n'),
    'search': (['search', 'scanMarkerUnique'], '// scanMarkerUnique\nclass Probe {}\n'),
    'callers': (['callers', 'ping'], 'class Probe {\n void caller() { ping(); }\n}\n'),
    'call-tree': (['call-tree', 'ping'], 'class Probe {\n void caller() { ping(); }\n}\n'),
    # No indexed definition for this name: execute the documented lexical fallback.
    'usages': (['usages', 'scanProbe'], 'class Probe { Object field = scanProbe; }\n'),
}
BOUNDED_ZERO = {'todo', 'annotations', 'deprecated', 'suppress', 'deeplinks', 'provides', 'call-tree'}
PATH_READERS = {'search', 'usages', 'callers', 'call-tree'}
MODULE_READERS = {'search', 'usages'}


def phases(name):
    result = ['positive', 'invalid-match', 'unreadable', 'empty-page',
              'directory', 'attached-error', 'local-exclusion', 'unknown-subtree']
    if name in PATH_READERS:
        result += ['file-exclusion', 'file-inclusion', 'cwd-exclusion',
                   'unreadable-file-exclusion', 'unreadable-cwd-exclusion']
    if name in MODULE_READERS:
        result += ['module-exclusion', 'module-inclusion', 'unreadable-module-exclusion']
    return result


def acceptance_keys():
    keys = {'inventory', 'source-preservation', 'database-preservation', 'applicable-java'}
    for name in CASES:
        for fmt in FORMATS:
            for suffix in POSITIONS:
                for phase in phases(name):
                    keys.add(f'{name}:{phase}:{fmt}:{suffix}')
    return keys


def acceptance_complete(expected, actual):
    """Matching samples cannot waive a positive Java applicability obligation."""
    required = acceptance_keys()
    return (required <= expected.keys() and required <= actual.keys() and
            all(section.get('inventory') == INVENTORY and section.get('applicable-java') is True
                for section in (expected, actual)))


def plan_errors(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-scan-errors'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java lexical scan failure fixture')=0",
                      ('; Java lexical scan failure fixture executes matched UTF-8/read/walk '
                       'errors and scope/empty-page controls across all Java lexical readers; '
                       'file/module/cwd selection before failing reads; root-registration I/O, '
                       'watch/VCS failures, restore commit/recovery I/O and '
                       'refresh/update publication failures remain unresolved',))


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = (Path(__file__).resolve().parents[2] / '.artifacts').resolve()
    if not base.is_relative_to(boundary):
        raise ToolError('scan fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='scan-errors-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    source = runner.root / 'Probe.java'
    authored = b'class Probe {}\n'
    source.write_bytes(authored)
    (runner.root / 'Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'inventory.xml').write_text('<inventory/>\n')
    database = directory / 'index.sqlite'
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(database))
    runner.command('rebuild', '--force')
    attached = directory / 'attached'
    attached.mkdir()
    runner.command('subtree', 'add', 'attached', str(attached))
    selected_directory = runner.root / 'allowed'
    selected_directory.mkdir()
    # Close/checkpoint each CLI connection before fingerprinting the database.
    before = file_sha256(database)
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        want = INVENTORY
        if inventory != want:
            raise ToolError('scan fixture full inventory incomplete')
        record('inventory', want, inventory)
        record('applicable-java', True, any(ext == '.java' for ext in inventory.values()))
    finally:
        state.close()

    def invoke(name, phase, args, failure, nonempty=False, cwd=None):
        for fmt in FORMATS:
            for suffix in POSITIONS:
                flags = ['--format', fmt]
                code, output = runner.command(*(args + flags if suffix else flags + args),
                                               cwd=cwd, acceptable=(0, 1, 2))
                with (directory / f'{runner.sequence:03d}.stderr.log').open('rb') as stream:
                    error = stream.read(runner.output_budget + 1)
                if len(error) > runner.output_budget:
                    raise ToolError('scan failure diagnostic exceeded its budget')
                if failure:
                    want = {'exit': 1, 'stdout-empty': True, 'diagnostic': True}
                    got = {'exit': code, 'stdout-empty': not output, 'diagnostic': bool(error.strip())}
                else:
                    try:
                        doc = json.loads(output) if fmt == 'json' else None
                        valid = isinstance(doc, dict) if fmt == 'json' else bool(output.strip())
                    except ValueError:
                        valid = False
                    # Authored locations, not native DB rows, establish a
                    # positive source result in each command's format.
                    has_location = ('Probe.java' in output)
                    want = {'exit': 0, 'diagnostic': False, 'document': True, 'has-location': nonempty}
                    got = {'exit': code, 'diagnostic': bool(error.strip()),
                           'document': valid, 'has-location': has_location}
                record(f'{name}:{phase}:{fmt}:{suffix}', want, got)

    for name, (args, text) in CASES.items():
        content = text.encode()
        source.write_bytes(content)
        invoke(name, 'positive', args + ['--limit', '100'], False, True)
        # Preserve the anchor and change its matching line's encoding. Syntax
        # readers also reject it; ordinary grep readers used to swallow it.
        lines = content.splitlines(keepends=True)
        anchor = 1 if name in {'callers', 'call-tree'} else 0
        lines[anchor] = lines[anchor].rstrip(b'\n') + b' \xff\n'
        source.write_bytes(b''.join(lines))
        invoke(name, 'invalid-match', args + ['--limit', '100'], True)
        invoke(name, 'empty-page', args + ['--limit', '0'], name not in BOUNDED_ZERO)
        if name in PATH_READERS:
            invoke(name, 'file-exclusion', args + ['--in-file', 'Allowed.java'], False)
            invoke(name, 'file-inclusion', args + ['--in-file', 'Probe.java'], True)
            invoke(name, 'cwd-exclusion', args, False, cwd=selected_directory)
        if name in MODULE_READERS:
            invoke(name, 'module-exclusion', args + ['--module', 'allowed'], False)
            invoke(name, 'module-inclusion', args + ['--module', 'Probe.java'], True)
        source.write_bytes(content)
        source.chmod(0)
        try:
            if os.access(source, os.R_OK):
                raise ToolError('unreadable Java fixture cannot enforce read denial')
            invoke(name, 'unreadable', args + ['--limit', '100'], True)
            if name in PATH_READERS:
                invoke(name, 'unreadable-file-exclusion', args + ['--in-file', 'Allowed.java'], False)
                invoke(name, 'unreadable-cwd-exclusion', args, False, cwd=selected_directory)
            if name in MODULE_READERS:
                invoke(name, 'unreadable-module-exclusion', args + ['--module', 'allowed'], False)
        finally:
            source.chmod(0o600)
        # Directory suffixes are not source entities. Their children are.
        source.unlink()
        source.mkdir()
        (source / 'Probe.java').write_bytes(content)
        invoke(name, 'directory', args + ['--limit', '100'], False, True)
        (source / 'Probe.java').unlink()
        source.rmdir()
        source.write_bytes(authored)
        attached_source = attached / 'Probe.java'
        attached_source.write_bytes(b''.join(lines))
        invoke(name, 'attached-error', ['--subtree', 'attached', *args, '--limit', '100'], True)
        invoke(name, 'local-exclusion', ['--local', *args, '--limit', '100'], False)
        invoke(name, 'unknown-subtree', ['--subtree', 'absent', *args, '--limit', '100'], False)
        attached_source.unlink()
    source.write_bytes(authored)
    record('source-preservation', authored.decode(), source.read_text())
    record('database-preservation', before, file_sha256(database))
    return {FEATURE: expected}, {FEATURE: actual}
