"""Java management mutation formats, exercised only in disposable artifacts.

External installation CLIs are authored stubs. Source-owned root identities and
filesystem effects are checked alongside rendering; this is not MCP evidence.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from install_contracts import STUB
from root_contracts import Runner

ROOTS = 'global:format:java-root-mutations'
INSTALL = 'global:format:java-installation'
FEATURES = {ROOTS, INSTALL}
REASON = ('independent source/state and internal CLI: disposable Java root mutation and '
          'installation JSON/text, conflict/missing/failure states, child output isolation, '
          'dry-run plans and authored filesystem/external-call effects; not MCP equivalence')
MISSING = "Index not found. Run 'ast-index rebuild' first.\n"
EVENTS = ['post-checkout', 'post-merge', 'post-rewrite']


def plan_formats(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            subject = 'disposable-java-management-mutation-formats'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        # Keep the aggregate pending until the remaining command formats are
        # independently covered. A family contract is not a blanket pass.
        state.execute("UPDATE coverage SET reason=? WHERE feature='global:format' AND status='pending'",
                      ('Java read-only, lifecycle, root mutation and installation formats have '
                       'separate executed contracts; missing/error states across map/conventions '
                       'and delegated agrep global-format/--json composition remain unresolved',))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('mutation format fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='mutation-formats-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    (runner.root / 'Probe.java').write_text('class Probe {}\n')
    (runner.root / 'build').mkdir()
    (runner.root / 'build/Inventory.kt').write_text('// inventory only\n')
    (runner.root / 'descriptor.xml').write_text('<fixture/>\n')
    attached = directory / 'attached'
    attached.mkdir()
    (attached / 'Peer.java').write_text('class Peer {}\n')
    other = directory / 'other'
    other.mkdir()
    runner.environment.update(AST_INDEX_ROOT=str(runner.root),
                              AST_INDEX_DB_PATH=str(directory / 'index.sqlite'))
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def record(feature, key, want, got):
        expected[feature][key], actual[feature][key] = want, got

    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        inventory = dict(state.execute("SELECT extension,count(*) FROM file_inventory WHERE kind='file' GROUP BY extension"))
        for feature in FEATURES:
            record(feature, 'inventory', {'.java': 1, '.kt': 1, '.xml': 1}, inventory)
        if inventory != {'.java': 1, '.kt': 1, '.xml': 1}:
            raise ToolError('mutation format fixture full inventory incomplete')
    finally:
        state.close()

    def invoke(feature, label, format, args, want, text_fragment=None, code=0, environment=None):
        exit_code, output = runner.command('--format', format, *args,
                                          acceptable=(0, 1), environment=environment)
        if format == 'json' and code == 0:
            try:
                got = json.loads(output)
            except ValueError:
                got = '<invalid-json>'
        elif format == 'text' and text_fragment is not None:
            got, want = text_fragment in output, True
        else:
            got = output
        record(feature, label + ':' + format, (code, want), (exit_code, got))
        record(feature, 'ansi:' + str(runner.sequence), False, '\x1b' in output)
        return got

    def roots(label, names):
        rows = runner.json('subtree', 'list')
        record(ROOTS, label, sorted(names), sorted((row['name'], row['canonical_path']) for row in rows))

    for format in ('json', 'text'):
        for args in (['add-root', attached], ['remove-root', attached],
                     ['subtree', 'add', 'peer', attached], ['subtree', 'remove', 'peer']):
            label = '-'.join(args[:2]) if args[0] == 'subtree' else args[0]
            invoke(ROOTS, 'missing:' + label, format, args,
                   '' if format == 'json' else MISSING, code=1 if format == 'json' else 0)
        record(ROOTS, 'missing:state:' + format, False, (directory / 'index.sqlite').exists())
    runner.command('rebuild')
    for format in ('json', 'text'):
        path = str(attached)
        invoke(ROOTS, 'legacy-add', format, ['add-root', '../attached'],
               {'command': 'add-root', 'status': 'complete', 'path': path}, 'Added source root:')
        roots('legacy-add:state:' + format, [('attached', path)])
        invoke(ROOTS, 'legacy-dedup', format, ['add-root', attached],
               {'command': 'add-root', 'status': 'complete', 'path': path}, 'Added source root:')
        roots('legacy-dedup:state:' + format, [('attached', path)])
        for args, command in ((['add-root', '.'], 'add-root'),
                              (['subtree', 'add', 'self', '.'], 'subtree-add'),
                              (['add-root', '..'], 'add-root'),
                              (['subtree', 'add', 'parent', '..'], 'subtree-add')):
            overlap_path = runner.root if args[-1] == '.' else directory
            invoke(ROOTS, 'overlap:' + command + (':inside' if args[-1] == '.' else ':parent'), format, args,
                   {'command': command, 'status': 'overlap-refused', 'path': str(overlap_path)}, 'Warning:')
        roots('overlap:state:' + format, [('attached', path)])
        invoke(ROOTS, 'legacy-remove', format, ['remove-root', '../attached'],
               {'command': 'remove-root', 'removed': True, 'path': path}, 'Removed source root:')
        invoke(ROOTS, 'legacy-absent', format, ['remove-root', attached],
               {'command': 'remove-root', 'removed': False, 'path': path}, 'Root not found:')
        roots('legacy-remove:state:' + format, [])
        invoke(ROOTS, 'legacy-force-add', format, ['add-root', '.', '--force'],
               {'command': 'add-root', 'status': 'complete', 'path': str(runner.root)}, 'Added source root:')
        roots('legacy-force:state:' + format, [('project', str(runner.root))])
        invoke(ROOTS, 'legacy-force-remove', format, ['remove-root', '.'],
               {'command': 'remove-root', 'removed': True, 'path': str(runner.root)}, 'Removed source root:')
        invoke(ROOTS, 'named-add', format, ['subtree', 'add', 'peer', '../attached'],
               {'name': 'peer', 'canonical_path': path, 'original_path': '../attached'}, 'Attached subtree peer')
        invoke(ROOTS, 'name-conflict', format, ['subtree', 'add', 'peer', other],
               {'command': 'subtree-add', 'status': 'name-conflict', 'name': 'peer', 'canonical_path': path},
               "Subtree name 'peer' already attached")
        invoke(ROOTS, 'path-conflict', format, ['subtree', 'add', 'alias', attached],
               {'command': 'subtree-add', 'status': 'path-conflict', 'name': 'peer', 'canonical_path': path},
               "already attached as subtree 'peer'")
        roots('conflicts:state:' + format, [('peer', path)])
        invoke(ROOTS, 'named-remove', format, ['subtree', 'remove', 'peer'],
               {'removed': True, 'name': 'peer'}, 'Detached subtree peer')
        invoke(ROOTS, 'named-absent', format, ['subtree', 'remove', 'peer'],
               {'removed': False, 'name': 'peer'}, "Subtree 'peer' not found.")
        # Explicit force still publishes the same structured success contract.
        invoke(ROOTS, 'force-add', format, ['subtree', 'add', 'self', '.', '--force'],
               {'name': 'self', 'canonical_path': str(runner.root), 'original_path': '.'}, 'Attached subtree self')
        roots('force:state:' + format, [('self', str(runner.root))])
        invoke(ROOTS, 'force-remove', format, ['subtree', 'remove', 'self'],
               {'removed': True, 'name': 'self'}, 'Detached subtree self')
        roots('empty:state:' + format, [])

    scripts = directory / 'external cli'
    scripts.mkdir()
    # Child stdout is deliberately noisy; JSON must stay one parseable value.
    stub = STUB.replace("name = os.path.basename(sys.argv[0])",
                        "name = os.path.basename(sys.argv[0])\nprint('synthetic child progress')")
    for name in ('claude', 'codex', 'ast-index-mcp'):
        script = scripts / name
        script.write_text(stub.format(python=sys.executable))
        script.chmod(0o755)
    log = directory / 'external.jsonl'
    runner.environment.update(PATH=str(scripts), AUDIT_INSTALL_LOG=str(log), AUDIT_INSTALL_FAILURE='')

    def calls():
        if not log.exists():
            return []
        if log.stat().st_size > runner.output_budget:
            raise ToolError('mutation fixture external log exceeded its budget')
        return [json.loads(line) for line in log.read_text().splitlines()]

    marketplace = ['claude', 'plugin', 'marketplace', 'add', 'defendend/Claude-ast-index-search']
    plugin = ['claude', 'plugin', 'install', 'ast-index']
    helper = runner.binary.parent / 'ast-index-mcp'
    if not helper.is_file():
        helper = scripts / 'ast-index-mcp'
    args = ['mcp', 'add', '--env', f'AST_INDEX_ROOT={runner.root}',
            '--env', f'AST_INDEX_BIN={runner.binary}', 'ast-index', str(helper)]
    fallback = ('[mcp_servers.ast-index]\ncommand = ' + json.dumps(str(helper)) +
                '\nenv = { AST_INDEX_ROOT = ' + json.dumps(str(runner.root)) +
                ', AST_INDEX_BIN = ' + json.dumps(str(runner.binary)) + ' }\n')
    for format in ('json', 'text'):
        for failure in ('', 'marketplace', 'plugin'):
            before = len(calls())
            code = 1 if failure == 'plugin' else 0
            invoke(INSTALL, 'claude:' + (failure or 'success'), format, ['install-claude-plugin'],
                   '' if code else {'command': 'install-claude-plugin', 'status': 'complete',
                                    'marketplace_added': failure != 'marketplace'},
                   'Plugin installed successfully.' if not code else '', code=code,
                   environment={'AUDIT_INSTALL_FAILURE': failure})
            record(INSTALL, 'claude:effects:' + (failure or 'success') + ':' + format,
                   [marketplace, plugin], calls()[before:])
        for mode, failure, code in (('dry-run', '', 0), ('complete', '', 0), ('failure', 'codex', 1)):
            before = len(calls())
            invoke(INSTALL, 'codex:' + mode, format,
                   ['install-codex-mcp'] + (['--dry-run'] if mode == 'dry-run' else []),
                   '' if code else {'command': 'install-codex-mcp', 'status': mode,
                                    'program': 'codex', 'args': args, 'fallback_config': fallback},
                   'Would run:' if mode == 'dry-run' else ('registered.' if not code else ''), code=code,
                   environment={'AUDIT_INSTALL_FAILURE': failure})
            record(INSTALL, 'codex:effects:' + mode + ':' + format,
                   [] if mode == 'dry-run' else [['codex', *args]], calls()[before:])

        hooks = runner.root / '.git/hooks'
        hooks.mkdir(exist_ok=True)
        for event in EVENTS:
            (hooks / event).unlink(missing_ok=True)
        custom = b'#!/bin/sh\n# authored custom hook\nexit 0\n'
        (hooks / EVENTS[0]).write_bytes(custom)
        plan = invoke(INSTALL, 'hooks:dry-run', format, ['install-git-hooks', '--dry-run'],
                      {}, 'Would write the following hooks')
        if format == 'json':
            # The script is executable product data; verify its promises without
            # copying the whole implementation into the expected value.
            script = plan.get('script', '') if isinstance(plan, dict) else ''
            summary = dict(plan) if isinstance(plan, dict) else {}
            summary['script'] = ('ast-index watch-status --quiet' in script and
                                 'ast-index update --background --debounce-ms 500' in script)
            expected[INSTALL]['hooks:dry-run:json'] = (0, {
                'command': 'install-git-hooks', 'status': 'dry-run', 'hooks_dir': str(hooks),
                'hooks': EVENTS, 'script': True})
            actual[INSTALL]['hooks:dry-run:json'] = (actual[INSTALL]['hooks:dry-run:json'][0], summary)
        record(INSTALL, 'hooks:dry-run:state:' + format, {EVENTS[0]: custom.decode()},
               {p.name: p.read_text() for p in hooks.iterdir()})
        for label, flags, written, installed, preserved in (
                ('preserve', [], EVENTS[1:], [], EVENTS[:1]),
                ('force', ['--force'], EVENTS, [], []),
                ('idempotent', [], [], EVENTS, [])):
            invoke(INSTALL, 'hooks:' + label, format, ['install-git-hooks', *flags],
                   {'command': 'install-git-hooks', 'status': 'complete', 'hooks_dir': str(hooks),
                    'written': written, 'already_installed': installed, 'preserved': preserved},
                   f'Installed {len(written)} hook(s)')
            record(INSTALL, 'hooks:files:' + label + ':' + format, EVENTS,
                   sorted(p.name for p in hooks.iterdir()))
            record(INSTALL, 'hooks:executable:' + label + ':' + format, True,
                   all(os.access(hooks / event, os.X_OK) for event in EVENTS if event not in preserved))
            if preserved:
                record(INSTALL, 'hooks:custom:' + format, custom.decode(), (hooks / EVENTS[0]).read_text())
            else:
                record(INSTALL, 'hooks:body:' + label + ':' + format, True,
                       all(b'ast-index update --background --debounce-ms 500' in (hooks / event).read_bytes()
                           for event in EVENTS))
        before = len(calls())
        invoke(INSTALL, 'claude:missing-cli', format, ['install-claude-plugin'], '',
               '' if format == 'text' else None, code=1, environment={'PATH': str(directory / 'empty-path')})
        record(INSTALL, 'claude:missing-cli:effects:' + format, [], calls()[before:])
        invoke(INSTALL, 'codex:missing-cli', format, ['install-codex-mcp'], '',
               '' if format == 'text' else None, code=1, environment={'PATH': str(directory / 'empty-path')})

        common = directory / ('common-' + format) / '.git'
        metadata = common / 'worktrees/linked'
        metadata.mkdir(parents=True)
        (metadata / 'commondir').write_text('../..\n')
        linked = directory / ('linked-' + format)
        linked.mkdir()
        (linked / '.git').write_text('gitdir: ../common-' + format + '/.git/worktrees/linked\n')
        (linked / 'Linked.java').write_text('class Linked {}\n')
        code, output = runner.command('--format', format, 'install-git-hooks', cwd=linked,
                                      environment={'AST_INDEX_ROOT': str(linked)}, acceptable=(0, 1))
        if format == 'json':
            try:
                got = json.loads(output)
            except ValueError:
                got = '<invalid-json>'
            want = {'command': 'install-git-hooks', 'status': 'complete', 'hooks_dir': str(common / 'hooks'),
                    'written': EVENTS, 'already_installed': [], 'preserved': []}
        else:
            want, got = True, f'Installed 3 hook(s) into {common / "hooks"}.' in output
        record(INSTALL, 'hooks:linked:' + format, (0, want), (code, got))
        record(INSTALL, 'hooks:linked:state:' + format, (True, False),
               (all((common / 'hooks' / event).is_file() for event in EVENTS), (metadata / 'hooks').exists()))
        no_git = directory / ('no-git-' + format)
        no_git.mkdir()
        (no_git / 'NoGit.java').write_text('class NoGit {}\n')
        code, output = runner.command('--format', format, 'install-git-hooks', cwd=no_git,
                                      environment={'AST_INDEX_ROOT': str(no_git)}, acceptable=(0, 1))
        record(INSTALL, 'hooks:missing-git:' + format, (1, ''), (code, output))
        record(INSTALL, 'hooks:missing-git:state:' + format, ['NoGit.java'],
               sorted(p.name for p in no_git.iterdir()))
    return expected, actual
