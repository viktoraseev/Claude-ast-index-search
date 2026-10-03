"""Native installation contracts with controlled external CLIs, never real settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

from common import ToolError
from root_contracts import Runner


FEATURES = {'install-claude-plugin', 'install-codex-mcp', 'install-git-hooks'}
REASON = ('independent source/state: disposable Java installation, external command arguments '
          'and failure propagation; no real settings or hooks modified; not MCP equivalence')
STUB = '''#!{python}
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ['AUDIT_INSTALL_LOG'], 'a') as out:
    out.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'claude':
    stage = 'marketplace' if 'marketplace' in sys.argv else 'plugin'
    sys.exit(7 if stage == os.environ.get('AUDIT_INSTALL_FAILURE') else 0)
if name == 'codex':
    sys.exit(7 if os.environ.get('AUDIT_INSTALL_FAILURE') == 'codex' else 0)
if name == 'ast-index':
    if sys.argv[1:2] == ['watch-status']:
        sys.exit(int(os.environ.get('AUDIT_INSTALL_WATCH_STATUS', '1')))
    sys.exit(int(os.environ.get('AUDIT_INSTALL_UPDATE_STATUS', '0')))
'''


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('installation fixture artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='install-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / 'Probe.java').write_text('public class Probe {}\n')
    (runner.root / '.git').mkdir()
    scripts = directory / 'external cli'
    scripts.mkdir()
    for name in ('claude', 'codex', 'ast-index', 'ast-index-mcp'):
        script = scripts / name
        script.write_text(STUB.format(python=sys.executable))
        script.chmod(0o755)
    log = directory / 'external.jsonl'
    runner.environment.update(PATH=str(scripts) + os.pathsep + runner.environment.get('PATH', ''),
                              AUDIT_INSTALL_LOG=str(log), AUDIT_INSTALL_FAILURE='')
    expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))

    def calls():
        if not log.exists():
            return []
        if log.stat().st_size > 1024 * 1024:
            raise ToolError('installation fixture external log exceeded its budget')
        return [json.loads(line) for line in log.read_text().splitlines()]

    def invoke(feature, label, failure=''):
        before = len(calls())
        code, output = runner.command(feature, environment={'AUDIT_INSTALL_FAILURE': failure},
                                      acceptable=(0, 1))
        actual[feature][label] = {'exit': code, 'commands': calls()[before:]}
        return output

    marketplace = ['claude', 'plugin', 'marketplace', 'add', 'defendend/Claude-ast-index-search']
    plugin = ['claude', 'plugin', 'install', 'ast-index']
    feature = 'install-claude-plugin'
    for label, failure, commands, code in (
            ('success', '', [marketplace, plugin], 0),
            ('marketplace-warning', 'marketplace', [marketplace, plugin], 0),
            ('plugin-failure', 'plugin', [marketplace, plugin], 1)):
        expected[feature][label] = {'exit': code, 'commands': commands}
        invoke(feature, label, failure)

    feature = 'install-codex-mcp'
    helper = runner.binary.parent / 'ast-index-mcp'
    if not helper.is_file():
        helper = scripts / 'ast-index-mcp'
    args = ['codex', 'mcp', 'add', '--env', f'AST_INDEX_ROOT={runner.root}',
            '--env', f'AST_INDEX_BIN={runner.binary}', 'ast-index', str(helper)]
    before = len(calls())
    _, output = runner.command(feature, '--dry-run')
    try:
        fallback = tomllib.loads(output.split('Fallback ~/.codex/config.toml:\n', 1)[1])
    except (IndexError, tomllib.TOMLDecodeError) as error:
        raise ToolError('installation fixture could not parse Codex fallback config') from error
    expected[feature]['dry-run'] = {'external-calls': [], 'fallback': {'mcp_servers': {'ast-index': {
        'command': str(helper), 'env': {'AST_INDEX_ROOT': str(runner.root), 'AST_INDEX_BIN': str(runner.binary)}}}}}
    actual[feature]['dry-run'] = {'external-calls': calls()[before:], 'fallback': fallback}
    for label, failure, code in (('success', '', 0), ('failure', 'codex', 1)):
        expected[feature][label] = {'exit': code, 'commands': [args]}
        invoke(feature, label, failure)

    feature = 'install-git-hooks'
    hooks = runner.root / '.git' / 'hooks'
    events = ('post-checkout', 'post-merge', 'post-rewrite')
    before = len(calls())
    runner.command(feature, '--dry-run')
    expected[feature]['dry-run'] = {'writes': False, 'external-calls': []}
    actual[feature]['dry-run'] = {'writes': hooks.exists(), 'external-calls': calls()[before:]}
    hooks.mkdir(exist_ok=True)
    custom = b'#!/bin/sh\n# public synthetic user hook\nexit 0\n'
    (hooks / events[0]).write_bytes(custom)
    runner.command(feature)
    expected[feature]['preserve-custom'] = True
    actual[feature]['preserve-custom'] = (hooks / events[0]).read_bytes() == custom
    runner.command(feature, '--force')
    contents = {event: (hooks / event).read_bytes() for event in events}
    expected[feature]['installed'] = {'equal': True, 'executable': True, 'watch': True, 'update': True}
    actual[feature]['installed'] = {
        'equal': len(set(contents.values())) == 1,
        'executable': all(os.access(hooks / event, os.X_OK) for event in events),
        'watch': all(b'ast-index watch-status --quiet' in script for script in contents.values()),
        'update': all(b'ast-index update --background --debounce-ms 500' in script for script in contents.values()),
    }
    runner.command(feature)
    expected[feature]['idempotent'] = True
    actual[feature]['idempotent'] = all((hooks / event).read_bytes() == content for event, content in contents.items())
    common = directory / 'common' / '.git'
    metadata = common / 'worktrees' / 'linked'
    metadata.mkdir(parents=True)
    (metadata / 'commondir').write_text('../..\n')
    linked = directory / 'linked'
    linked.mkdir()
    (linked / '.git').write_text('gitdir: ../common/.git/worktrees/linked\n')
    (linked / 'Linked.java').write_text('public class Linked {}\n')
    runner.command(feature, cwd=linked)
    expected[feature]['linked-worktree'] = {'shared-hooks': True, 'private-hooks': False}
    actual[feature]['linked-worktree'] = {
        'shared-hooks': all((common / 'hooks' / event).is_file() for event in events),
        'private-hooks': (metadata / 'hooks').exists(),
    }
    for label, watch, update, wanted in (
            ('queue-update', '1', '0', [['ast-index', 'watch-status', '--quiet'],
                                      ['ast-index', 'update', '--background', '--debounce-ms', '500']]),
            ('active-watcher', '0', '0', [['ast-index', 'watch-status', '--quiet']]),
            ('failed-update', '1', '7', [['ast-index', 'watch-status', '--quiet'],
                                       ['ast-index', 'update', '--background', '--debounce-ms', '500']])):
        before = len(calls())
        result = subprocess.run([str(hooks / events[0])], cwd=runner.root,
            env={**runner.environment, 'AUDIT_INSTALL_WATCH_STATUS': watch,
                 'AUDIT_INSTALL_UPDATE_STATUS': update}, capture_output=True, timeout=15)
        expected[feature][label] = {'exit': 0, 'commands': wanted, 'warn': label == 'failed-update'}
        actual[feature][label] = {'exit': result.returncode, 'commands': calls()[before:],
                                'warn': b'failed to queue' in result.stderr}
    return expected, actual
