"""Java watch notifications: executed CLI/source contracts, not MCP equivalence."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-watch-events'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: disposable Java notification '
          'backend/channel termination, directory/file reconciliation, relative event '
          'filters and Java module descriptors; complete inventory; not MCP equivalence')
DESCRIPTORS = ('build.gradle', 'build.gradle.kts', 'pom.xml', 'ya.make')
INVENTORY = {'src/Probe.java': '.java', 'Inventory.kt': '.kt', 'inventory.xml': '.xml',
             **{f'markers/{name}': Path(name).suffix for name in DESCRIPTORS}}
FORMATS = ('json', 'text')
CASES = ('disconnect', 'backend-error', 'empty-batch', 'create', 'edit', 'delete',
         'file-rename', 'directory-rename', 'directory-import', 'directory-export',
         'directory-delete', 'dotted-directory-delete', 'coalesced', 'irrelevant',
         'excluded', 'scoped', 'root-name', 'descriptor-create', 'descriptor-edit',
         'descriptor-delete', 'module-directory-rename',
         *(f'descriptor-{operation}@{name}' for operation in ('create', 'edit', 'delete')
           for name in DESCRIPTORS[1:]))
GUARDS = ('missing-db', 'outside-control', 'outside-root', 'wrong-identity',
          'parent-path', 'absolute-path', 'unknown-mode', 'path-budget', 'byte-budget')
NATIVE = ('directory-rename', 'directory-import', 'directory-export', 'directory-delete')


def acceptance_keys():
    return {'inventory', 'applicable-java', 'source-preservation',
            *(f'{case}:{fmt}:{suffix}' for case in CASES for fmt in FORMATS for suffix in (False, True)),
            *(f'guard:{case}' for case in GUARDS), *(f'native:{case}' for case in NATIVE)}


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= section.keys() and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def plan_events(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-watch-events-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,'Java watch event fixture')=0",
                      ('; Java watch event fixture executes notification backend/channel failures, '
                       'Java file and directory create/rename/import/export/delete/coalescing, '
                       'relative filters, static include/exclude and module descriptor refresh; '
                       'attached-root notification registration, dynamic configuration/root changes, '
                       'persistent publication recovery and other parent criteria remain pending',))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('watch event fixtures must stay inside repository .artifacts')
    directory = Path(tempfile.mkdtemp(prefix='watch-events-', dir=base)).resolve()
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got
        checkpoint = directory / 'results.next.json'
        checkpoint.write_text(json.dumps({'expected': expected, 'actual': actual}))
        checkpoint.replace(directory / 'results.json')

    def setup(label, root_name='project'):
        location = directory / label
        location.mkdir()
        runner = Runner(binary, location)
        runner.root = location / root_name
        runner.root.mkdir()
        runner.database = location / 'index.sqlite'
        runner.control = location / 'events.json'
        runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(runner.database))
        write(runner.root / 'src/Probe.java', 'class WatchProbeBase {}\n')
        write(runner.root / 'Inventory.kt', '// inventory only\n')
        write(runner.root / 'inventory.xml', '<fixture/>\n')
        return runner

    def write(path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def identities(runner):
        return sorted([row['name'], row['path']] for row in runner.json('class', 'WatchProbe*', '--limit', 100)['items'])

    def snapshot(runner):
        with closing(sqlite3.connect(f'file:{runner.database}?mode=ro', uri=True)) as state:
            return tuple(state.iterdump())

    def notifications(runner, mode='events', paths=()):
        runner.control.write_text(json.dumps({'database': str(runner.database), 'root': str(runner.root),
                                             'mode': mode, 'paths': list(paths)}))

    def descriptor(name, module, dependency=False):
        if name == 'pom.xml':
            deps = ('<dependencies><dependency><groupId>fixture</groupId>'
                    '<artifactId>dep</artifactId></dependency></dependencies>' if dependency else '')
            return f'<project><groupId>fixture</groupId><artifactId>{module}</artifactId>{deps}</project>\n'
        if name == 'ya.make':
            return 'JAVA_LIBRARY()\n' + ('PEERDIR(dep)\n' if dependency else '') + 'END()\n'
        return 'dependencies {\n' + ('implementation(project(":dep"))\n' if dependency else '') + '}\n'

    sample = setup('inventory')
    for name in DESCRIPTORS:
        write(sample.root / 'markers' / name, descriptor(name, 'markers'))
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, sample.root)
        files = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if files != INVENTORY:
            raise ToolError('watch event full inventory incomplete')
    finally:
        state.close()
    record('inventory', INVENTORY, files)
    record('applicable-java', True, '.java' in files.values())

    # Expected declarations are authored here; DB rows are used only for
    # transaction preservation, never to infer the expected Java identities.
    for case in CASES:
        for fmt in FORMATS:
            for suffix in (False, True):
                kind, _, descriptor_name = case.partition('@')
                descriptor_name = descriptor_name or 'build.gradle'
                runner = setup(f'{case}-{fmt}-{suffix}', 'build' if case == 'root-name' else 'project')
                if case == 'dotted-directory-delete':
                    (runner.root / 'src').rename(runner.root / 'src.dotted')
                if case == 'scoped':
                    write(runner.root / '.ast-index.yaml', 'include:\n  - src\nexclude:\n  - src/blocked\n')
                if kind in ('descriptor-edit', 'descriptor-delete', 'module-directory-rename'):
                    write(runner.root / 'src' / descriptor_name, descriptor(descriptor_name, 'src'))
                if kind == 'descriptor-edit':
                    write(runner.root / 'dep' / descriptor_name, descriptor(descriptor_name, 'dep'))
                    write(runner.root / 'dep/Helper.java', 'class Helper {}\n')
                runner.command('rebuild', '--force')
                # Persist initial module fingerprint before descriptor mutations.
                runner.command('update')
                before = snapshot(runner)
                names = [['WatchProbeBase', 'src/Probe.java']]
                paths, updates = ['src/Probe.java'], 1
                mode = case if case in ('disconnect', 'backend-error') else 'events'
                if case in ('disconnect', 'backend-error', 'empty-batch'):
                    paths, updates = [], 0
                elif case in ('create', 'coalesced', 'scoped'):
                    write(runner.root / 'src/Added.java', 'class WatchProbeAdded {}\n')
                    names.append(['WatchProbeAdded', 'src/Added.java'])
                    paths = ['src']
                    if case == 'coalesced': paths = ['src', 'src/Added.java', 'src/Added.java']
                    if case == 'scoped':
                        write(runner.root / 'outside/Hidden.java', 'class WatchProbeHidden {}\n')
                        write(runner.root / 'src/blocked/Hidden.java', 'class WatchProbeBlocked {}\n')
                        paths += ['outside', 'src/blocked']
                elif case in ('edit', 'root-name'):
                    write(runner.root / 'src/Probe.java', 'class WatchProbeEdited {}\n')
                    names = [['WatchProbeEdited', 'src/Probe.java']]
                elif case == 'delete':
                    (runner.root / 'src/Probe.java').unlink(); names = []
                elif case == 'file-rename':
                    (runner.root / 'src/Probe.java').rename(runner.root / 'src/Renamed.java')
                    names = [['WatchProbeBase', 'src/Renamed.java']]
                    paths = ['src/Probe.java', 'src/Renamed.java']
                elif case in ('directory-rename', 'module-directory-rename'):
                    (runner.root / 'src').rename(runner.root / 'moved')
                    names = [['WatchProbeBase', 'moved/Probe.java']]
                    paths = ['src', 'moved']
                elif case == 'directory-import':
                    write(runner.directory / 'incoming/Added.java', 'class WatchProbeAdded {}\n')
                    (runner.directory / 'incoming').rename(runner.root / 'incoming')
                    paths = ['incoming']; names.append(['WatchProbeAdded', 'incoming/Added.java'])
                elif case == 'directory-export':
                    (runner.root / 'src').rename(runner.directory / 'outgoing')
                    paths, names = ['src'], []
                elif case in ('directory-delete', 'dotted-directory-delete'):
                    folder = 'src.dotted' if case == 'dotted-directory-delete' else 'src'
                    shutil.rmtree(runner.root / folder); paths, names = [folder], []
                elif case in ('irrelevant', 'excluded'):
                    path = 'notes.txt' if case == 'irrelevant' else 'build/Hidden.java'
                    write(runner.root / path, 'class WatchProbeHidden {}\n')
                    paths, updates = [path], 0
                elif case.startswith('descriptor-'):
                    build = runner.root / 'src' / descriptor_name
                    if kind == 'descriptor-delete': build.unlink()
                    else: write(build, descriptor(descriptor_name, 'src', kind == 'descriptor-edit'))
                    paths = ['src/' + descriptor_name]
                notifications(runner, mode, paths)
                flags = ['--format', fmt]
                args = ['watch'] + flags if suffix else flags + ['watch']
                code, output = runner.command(*args, environment={'AST_INDEX_TEST_WATCH_EVENTS_FILE': str(runner.control)}, acceptable=(0, 1))
                error = (runner.directory / f'{runner.sequence:03d}.stderr.log').read_text()
                if fmt == 'json':
                    docs = [json.loads(line) for line in output.splitlines()]
                    valid = bool(docs) and docs[0] == {'command': 'watch', 'status': 'watching', 'root': str(runner.root)}
                    batches = sum(d.get('status') == 'updated' for d in docs)
                    valid &= all(d.get('command') == 'watch' for d in docs)
                else:
                    valid = output.startswith('Watching for changes in ') and '\x1b' not in output
                    batches = error.count('Detected ')
                # All deterministic providers finish by disconnecting. This
                # must be a failed provider, even after a successful batch.
                want = {'exit': 1, 'diagnostic': True, 'valid': True, 'batches': updates, 'items': sorted(names)}
                got = {'exit': code, 'diagnostic': ('Watch error:' in error if case == 'backend-error' else 'Channel error:' in error),
                       'valid': bool(valid), 'batches': batches, 'items': identities(runner)}
                if updates == 0:
                    want['preserved'] = True; got['preserved'] = before == snapshot(runner)
                if case in ('disconnect', 'backend-error'):
                    status, silent = runner.command('watch-status', '--quiet', acceptable=(0, 1))
                    want['released'] = True; got['released'] = status == 1 and silent == ''
                    write(runner.root / 'src/Probe.java', 'class WatchProbeRetried {}\n')
                    notifications(runner, paths=['src/Probe.java'])
                    retry_code, retry_output = runner.command(*args,
                        environment={'AST_INDEX_TEST_WATCH_EVENTS_FILE': str(runner.control)}, acceptable=(0, 1))
                    want['retry'] = True
                    got['retry'] = (retry_code == 1 and 'already-running' not in retry_output
                                    and identities(runner) == [['WatchProbeRetried', 'src/Probe.java']])
                if case.startswith('descriptor-') or case == 'module-directory-rename':
                    modules = runner.json('module', '')
                    want['modules'] = [] if kind == 'descriptor-delete' else [['moved' if case == 'module-directory-rename' else 'src'] * 2]
                    if kind == 'descriptor-edit': want['modules'].insert(0, ['dep', 'dep'])
                    got['modules'] = sorted([row['name'], row['path']] for row in modules['items'])
                    if kind == 'descriptor-edit':
                        want['dependencies'] = ['dep']
                        got['dependencies'] = sorted(row['name'] for row in runner.json('deps', 'src')['items'])
                record(f'{case}:{fmt}:{suffix}', want, got)

    guard = setup('guards'); guard.command('rebuild', '--force')
    for case in GUARDS:
        notifications(guard, paths=['src'])
        body = json.loads(guard.control.read_text())
        env = {'AST_INDEX_TEST_WATCH_EVENTS_FILE': str(guard.control)}
        if case == 'missing-db': env['AST_INDEX_DB_PATH'] = ''
        if case == 'outside-control': env['AST_INDEX_TEST_WATCH_EVENTS_FILE'] = str(directory / 'wrong.json')
        if case == 'outside-root':
            body['root'] = str(sample.root)
            env['AST_INDEX_ROOT'] = str(sample.root)
        if case == 'wrong-identity': body['database'] = str(sample.database)
        if case == 'parent-path': body['paths'] = ['../Probe.java']
        if case == 'absolute-path': body['paths'] = [str(guard.root / 'src')]
        if case == 'unknown-mode': body['mode'] = 'unknown'
        if case == 'path-budget': body['paths'] = ['src'] * 129
        guard.control.write_text(json.dumps(body) if case != 'byte-budget' else ' ' * 65537)
        code, output = guard.command('--format', 'json', 'watch', environment=env, acceptable=(0, 1))
        record(f'guard:{case}', {'exit': 1, 'empty': True}, {'exit': code, 'empty': output == ''})

    # Real filesystem notifications complement deterministic event delivery.
    # Poll indexed declarations only; never trigger update/rebuild to hide a
    # watcher that missed a notification.
    runner = setup('native'); runner.command('rebuild', '--force')
    out, err = runner.directory / 'watch.stdout.log', runner.directory / 'watch.stderr.log'
    with out.open('wb') as stdout, err.open('wb') as stderr:
        child = subprocess.Popen([str(runner.binary), '--format', 'json', 'watch'], cwd=runner.root,
                                 env=runner.environment, stdout=stdout, stderr=stderr)
        def until(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if max(out.stat().st_size, err.stat().st_size) > runner.output_budget:
                    raise ToolError('watch event stream exceeded budget')
                if predicate(): return True
                if child.poll() is not None: return False
                time.sleep(.05)
            return False
        try:
            if not until(lambda: 'watching' in out.read_text()):
                raise ToolError('native watch event fixture did not become ready')
            (runner.root / 'src').rename(runner.root / 'moved')
            record('native:directory-rename', True, until(lambda: identities(runner) == [['WatchProbeBase', 'moved/Probe.java']]))
            write(runner.directory / 'incoming/Added.java', 'class WatchProbeAdded {}\n')
            (runner.directory / 'incoming').rename(runner.root / 'incoming')
            record('native:directory-import', True, until(lambda: identities(runner) == [['WatchProbeAdded', 'incoming/Added.java'], ['WatchProbeBase', 'moved/Probe.java']]))
            (runner.root / 'moved').rename(runner.directory / 'exported')
            record('native:directory-export', True, until(lambda: identities(runner) == [['WatchProbeAdded', 'incoming/Added.java']]))
            shutil.rmtree(runner.root / 'incoming')
            record('native:directory-delete', True, until(lambda: identities(runner) == []))
        finally:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
    record('source-preservation', 'class WatchProbeBase {}\n', (sample.root / 'src/Probe.java').read_text())
    return ({FEATURE: expected}, {FEATURE: actual})
