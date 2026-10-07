"""Java watch root/configuration contracts; authored truth, no MCP equivalent."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:format:java-watch-scope'
FEATURES = {FEATURE}
REASON = ('internal CLI and independent source/state: Java attached-root notifications, '
          'live root registration/availability and configuration reconciliation; not MCP equivalence')
CASES = ('startup', 'attached-edit', 'attached-create', 'attached-rename', 'attached-delete',
         'directory-import', 'directory-export', 'root-add', 'new-root-edit', 'root-remove',
         'root-unavailable', 'root-return', 'returned-root-edit', 'include-create',
         'include-edit', 'exclude-edit', 'yaml-precedence', 'yaml-delete', 'yml-delete',
         'config-root-add', 'config-root-edit', 'config-root-retained', 'ignore-create',
         'legacy-remove', 'legacy-add', 'include-empty', 'ignore-edit',
         'no-ignore-enable', 'no-ignore-disable', 'ignore-delete', 'idle', 'format', 'released')
INVENTORY = {'.java': 3, '.kt': 1, '.xml': 1, '.yaml': 1}


def acceptance_keys():
    return {'inventory', 'applicable-java', *(f'{fmt}:{case}' for fmt in ('json', 'text') for case in CASES)}


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= rows.keys() and rows.get('inventory') == INVENTORY
               and rows.get('applicable-java') is True for rows in (expected, actual))


def plan_scope(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-watch-scope-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))
        note = ('; separate executed Java watch scope checklist covers attached-root notifications, '
                'live add/remove registrations, unavailable/recreated roots, yaml/yml selection, '
                'include/exclude/ignore/no_ignore and additive config-root attachment; persistent '
                'publication recovery and other recorded parent obligations remain pending; not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='global:format' "
                      "AND status='pending' AND instr(reason,?)=0", (note, note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('watch scope fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='watch-scope-', dir=base)).resolve()
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got
        next_file = directory / 'results.next.json'
        next_file.write_text(json.dumps({'expected': expected, 'actual': actual}))
        next_file.replace(directory / 'results.json')

    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    for fmt in ('json', 'text'):
        location = directory / fmt
        location.mkdir()
        runner = Runner(binary, location)
        runner.root.mkdir()
        runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(location / 'index.sqlite'))
        extra = location / 'build'  # Root basename must not be treated as an excluded directory.
        write(runner.root / 'left/Probe.java', 'class WatchScopeLeft {}\n')
        write(runner.root / 'right/Probe.java', 'class WatchScopeRight {}\n')
        write(extra / 'Probe.java', 'class WatchScopeAttached {}\n')
        write(runner.root / 'Inventory.kt', '// inventory only\n')
        write(runner.root / 'inventory.xml', '<fixture/>\n')
        yaml, yml = runner.root / '.ast-index.yaml', runner.root / '.ast-index.yml'
        write(yaml, 'roots: [../build]\n')
        if fmt == 'json':
            state = connect(location / 'inventory.sqlite')
            try:
                state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
                mobile_contracts.inventory(state, location)
                counts = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' "
                                            "AND extension IN ('.java','.kt','.xml','.yaml') GROUP BY extension"))
                if counts != INVENTORY:
                    raise ToolError('watch scope full inventory incomplete')
            finally:
                state.close()
            record('inventory', INVENTORY, counts)
            record('applicable-java', True, counts.get('.java', 0) > 0)
        runner.command('rebuild', '--force')
        # Register the already configured path too: one owner, never duplicates.
        runner.command('add-root', extra)
        out, err = location / 'watch.stdout.log', location / 'watch.stderr.log'
        names = [('WatchScopeLeft', 'project/left/Probe.java'), ('WatchScopeRight', 'project/right/Probe.java'),
                 ('WatchScopeAttached', 'build/Probe.java')]

        def identities():
            return sorted((row['name'], runner.path(row['path']))
                          for row in runner.json('class', 'WatchScope*', '--limit', 100)['items'])

        with out.open('wb') as stdout, err.open('wb') as stderr:
            child = subprocess.Popen([str(runner.binary), '--format', fmt, 'watch'], cwd=runner.root,
                                     env=runner.environment, stdout=stdout, stderr=stderr)

            def until(predicate, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if max(out.stat().st_size, err.stat().st_size) > runner.output_budget:
                        raise ToolError('watch scope stream exceeded budget')
                    if predicate():
                        return True
                    if child.poll() is not None:
                        return False
                    time.sleep(.05)
                return False

            def check(case):
                wanted = sorted(names)
                matched = until(lambda: identities() == wanted)
                record(f'{fmt}:{case}', {'matched': True, 'items': wanted},
                       {'matched': matched, 'items': identities()})

            def replace_name(before, after, path):
                names[:] = [item for item in names if item[0] != before]
                names.append((after, path))

            try:
                if not until(lambda: 'watching' in out.read_text().lower()):
                    raise ToolError('native watch scope fixture did not become ready')
                check('startup')
                write(extra / 'Probe.java', 'class WatchScopeEdited {}\n')
                replace_name('WatchScopeAttached', 'WatchScopeEdited', 'build/Probe.java'); check('attached-edit')
                write(extra / 'Added.java', 'class WatchScopeAdded {}\n')
                names.append(('WatchScopeAdded', 'build/Added.java')); check('attached-create')
                (extra / 'Added.java').rename(extra / 'Moved.java')
                replace_name('WatchScopeAdded', 'WatchScopeAdded', 'build/Moved.java'); check('attached-rename')
                (extra / 'Moved.java').unlink()
                names.remove(('WatchScopeAdded', 'build/Moved.java')); check('attached-delete')
                write(location / 'incoming/Imported.java', 'class WatchScopeImported {}\n')
                (location / 'incoming').rename(extra / 'nested')
                names.append(('WatchScopeImported', 'build/nested/Imported.java')); check('directory-import')
                (extra / 'nested').rename(location / 'exported')
                names.remove(('WatchScopeImported', 'build/nested/Imported.java')); check('directory-export')
                new_root = location / 'registered'
                write(new_root / 'Probe.java', 'class WatchScopeRegistered {}\n')
                runner.command('subtree', 'add', 'fixture', new_root)
                names.append(('WatchScopeRegistered', 'registered/Probe.java')); check('root-add')
                write(new_root / 'Probe.java', 'class WatchScopeNewEdit {}\n')
                replace_name('WatchScopeRegistered', 'WatchScopeNewEdit', 'registered/Probe.java'); check('new-root-edit')
                runner.command('subtree', 'remove', 'fixture')
                names.remove(('WatchScopeNewEdit', 'registered/Probe.java')); check('root-remove')
                shutil.rmtree(extra)
                names.remove(('WatchScopeEdited', 'build/Probe.java')); check('root-unavailable')
                write(extra / 'Probe.java', 'class WatchScopeReturned {}\n')
                names.append(('WatchScopeReturned', 'build/Probe.java')); check('root-return')
                write(extra / 'Probe.java', 'class WatchScopeReturnedEdit {}\n')
                replace_name('WatchScopeReturned', 'WatchScopeReturnedEdit', 'build/Probe.java'); check('returned-root-edit')
                write(yaml, 'include: [left]\n')
                names.remove(('WatchScopeRight', 'project/right/Probe.java')); check('include-create')
                write(yaml, 'include: [right]\n')
                names.remove(('WatchScopeLeft', 'project/left/Probe.java'))
                names.append(('WatchScopeRight', 'project/right/Probe.java')); check('include-edit')
                write(yaml, 'exclude: [right]\n')
                names.remove(('WatchScopeRight', 'project/right/Probe.java'))
                names.append(('WatchScopeLeft', 'project/left/Probe.java')); check('exclude-edit')
                write(yml, 'include: [right]\n')
                # The selected yaml still wins, even if a lower-priority file changed.
                time.sleep(.8); check('yaml-precedence')
                yaml.unlink()
                names.remove(('WatchScopeLeft', 'project/left/Probe.java'))
                names.append(('WatchScopeRight', 'project/right/Probe.java')); check('yaml-delete')
                yml.unlink()
                names.append(('WatchScopeLeft', 'project/left/Probe.java')); check('yml-delete')
                configured = location / 'configured'
                write(configured / 'Probe.java', 'class WatchScopeConfig {}\n')
                write(yaml, 'roots: [../configured, ../build]\n')
                names.append(('WatchScopeConfig', 'configured/Probe.java')); check('config-root-add')
                write(configured / 'Probe.java', 'class WatchScopeConfigEdit {}\n')
                replace_name('WatchScopeConfig', 'WatchScopeConfigEdit', 'configured/Probe.java'); check('config-root-edit')
                yaml.unlink()
                time.sleep(.8); check('config-root-retained')
                runner.command('remove-root', configured)
                names.remove(('WatchScopeConfigEdit', 'configured/Probe.java')); check('legacy-remove')
                runner.command('add-root', configured)
                names.append(('WatchScopeConfigEdit', 'configured/Probe.java')); check('legacy-add')
                write(yaml, 'include: []\nexclude: []\n')
                time.sleep(.8); check('include-empty')
                # Match rebuild semantics: config roots become registered roots;
                # removing the config does not implicitly detach a registration.
                (runner.root / '.git').mkdir()
                write(runner.root / '.gitignore', '/left/\n')
                names.remove(('WatchScopeLeft', 'project/left/Probe.java')); check('ignore-create')
                write(runner.root / '.gitignore', '/right/\n')
                names.append(('WatchScopeLeft', 'project/left/Probe.java'))
                names.remove(('WatchScopeRight', 'project/right/Probe.java')); check('ignore-edit')
                write(yaml, 'no_ignore: true\n')
                names.append(('WatchScopeRight', 'project/right/Probe.java')); check('no-ignore-enable')
                write(yaml, 'no_ignore: false\n')
                names.remove(('WatchScopeRight', 'project/right/Probe.java')); check('no-ignore-disable')
                (runner.root / '.gitignore').unlink()
                names.append(('WatchScopeRight', 'project/right/Probe.java')); check('ignore-delete')
                size = out.stat().st_size
                time.sleep(1.1)
                record(f'{fmt}:idle', True, size == out.stat().st_size and child.poll() is None)
                output = out.read_text()
                if fmt == 'json':
                    docs = [json.loads(line) for line in output.splitlines()]
                    valid = (docs[0] == {'command': 'watch', 'status': 'watching', 'root': str(runner.root)}
                             and all(d.get('command') == 'watch' and d.get('status') == 'updated' for d in docs[1:]))
                else:
                    valid = output.startswith('Watching for changes in ') and '\x1b' not in output
                record(f'{fmt}:format', True, valid)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=3)
        code, output = runner.command('watch-status', '--quiet', acceptable=(0, 1))
        record(f'{fmt}:released', True, code == 1 and output == '')
    return {FEATURE: expected}, {FEATURE: actual}
