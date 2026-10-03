"""Production index lifecycle on disposable, public synthetic source only.

This is an independent source/state contract, not MCP differential evidence.
The target project is never passed to any mutating command here.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from common import ToolError


FEATURES = {'rebuild', 'update', 'restore', 'clear', 'watch', 'watch-status'}
REASON = 'independent source/state: production lifecycle on disposable synthetic source; not MCP equivalence'


class Runner:
    def __init__(self, binary, directory):
        self.binary, self.directory = binary, directory
        self.root = directory / 'project'
        self.root.mkdir()
        (self.root / '.git').mkdir()
        self.database = directory / 'index.sqlite'
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(('AST_INDEX_', 'KOTLIN_INDEX_'))}
        self.environment.update(AST_INDEX_DB_PATH=str(self.database),
                                AST_INDEX_CACHE_DIR=str(directory / 'cache'),
                                AST_INDEX_DISABLE_GC='1', AST_INDEX_THREADS='2', NO_COLOR='1')
        self.sequence = 0

    def command(self, *arguments, acceptable=(0,), other=False):
        self.sequence += 1
        name = f'{self.sequence:03d}-{arguments[-1] if arguments[-1] in FEATURES else arguments[0]}'
        environment = self.environment.copy()
        root = self.root
        if other:
            root = self.directory / 'other-project'
            root.mkdir(exist_ok=True)
            (root / '.git').mkdir(exist_ok=True)
            environment['AST_INDEX_DB_PATH'] = str(self.directory / 'other.sqlite')
        stdout_path = self.directory / f'{name}.stdout.log'
        with stdout_path.open('wb') as stdout, (self.directory / f'{name}.stderr.log').open('wb') as stderr:
            result = subprocess.run([str(self.binary), *arguments], cwd=root, env=environment,
                                    stdout=stdout, stderr=stderr, timeout=10)
        if result.returncode not in acceptable:
            raise ToolError(f'disposable lifecycle command {arguments[0]} failed; see private fixture logs')
        with stdout_path.open('rb') as output:
            value = output.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise ToolError('disposable lifecycle command exceeded its output budget')
        return result.returncode, value.decode('utf-8')

    def classes(self):
        _, output = self.command('--format', 'json', 'class', '--pattern', '*', '--limit', '100')
        value = json.loads(output)
        items = value.get('items')
        pagination = value.get('pagination', {})
        if not isinstance(items, list) or pagination.get('has_more') or len(items) >= 100:
            raise ToolError('disposable lifecycle declaration response is incomplete')
        return sorted((item['path'], item['name'], item['line'], item.get('qualified_name')) for item in items)

    def status(self, *, other=False, quiet=False, text=False):
        arguments = ['watch-status'] if text or quiet else ['--format', 'json', 'watch-status']
        if quiet:
            arguments.append('--quiet')
        code, output = self.command(*arguments, acceptable=(0, 1), other=other)
        value = output if quiet or text else json.loads(output).get('watching')
        return code, value

    def watch(self, samples):
        with (self.directory / 'watch.stdout.log').open('wb') as stdout, \
                (self.directory / 'watch.stderr.log').open('wb') as stderr:
            child = subprocess.Popen([str(self.binary), 'watch'], cwd=self.root, env=self.environment,
                                     stdout=stdout, stderr=stderr)
            try:
                deadline = time.monotonic() + 15
                while self.status()[1] is not True:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise ToolError('disposable watcher did not become ready')
                    time.sleep(.05)
                samples['watch-status']['active'] = self.status()
                samples['watch-status']['active-text'] = self.status(text=True)
                samples['watch-status']['active-quiet'] = self.status(quiet=True)
                samples['watch-status']['other-project'] = self.status(other=True)
                self.command('watch')  # A second watcher must not steal the lock.
                samples['watch']['singleton'] = child.poll() is None and self.status()[1] is True
                source = self.root / 'Gamma.java'
                last_touch = 0
                while True:
                    if time.monotonic() - last_touch >= 1:
                        source.write_text('package fixture; class Watched {}\n')
                        last_touch = time.monotonic()
                    observed = self.classes()
                    if any(item[1] == 'Watched' for item in observed):
                        samples['watch']['updated'] = observed
                        break
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise ToolError('disposable watcher did not index a source change')
                    time.sleep(.1)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
                else:
                    child.wait()
        samples['watch-status']['stopped'] = self.status()


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('lifecycle fixture artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='lifecycle-', dir=base))
    runner = Runner(Path(binary).resolve(), directory)
    samples = {feature: {} for feature in FEATURES}
    initial = [('Alpha.java', 'Alpha', 1, 'fixture.Alpha'), ('Removed.java', 'Removed', 1, 'fixture.Removed')]
    updated = [('Alpha.java', 'Beta', 1, 'fixture.Beta'), ('Gamma.java', 'Gamma', 1, 'fixture.Gamma')]
    watched = [('Alpha.java', 'Beta', 1, 'fixture.Beta'), ('Gamma.java', 'Watched', 1, 'fixture.Watched')]
    expected = {
        'rebuild': {'initial': initial, 'repeated': initial, 'after-clear': updated},
        'update': {'changed-added-deleted': updated, 'unchanged': updated},
        'restore': {'snapshot': initial, 'same-source-rejected': True, 'preserved': initial},
        'clear': {'database-removed': True, 'lookup-refused': True},
        'watch': {'singleton': True, 'updated': watched},
        'watch-status': {'idle': (1, False), 'active': (0, True), 'active-text': (0, 'watching\n'),
                         'active-quiet': (0, ''), 'other-project': (1, False), 'stopped': (1, False)},
    }
    for name in ('Alpha', 'Removed'):
        (runner.root / f'{name}.java').write_text(f'package fixture; class {name} {{}}\n')
    rebuild = ('rebuild', '--force', '--max-files', '0', '--threads', '2')
    runner.command(*rebuild)
    samples['rebuild']['initial'] = runner.classes()
    runner.command(*rebuild)
    samples['rebuild']['repeated'] = runner.classes()
    backup = directory / 'snapshot.sqlite'
    source = sqlite3.connect(f'file:{runner.database}?mode=ro', uri=True)
    dest = sqlite3.connect(backup)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    (runner.root / 'Alpha.java').write_text('package fixture; class Beta {}\n')
    (runner.root / 'Removed.java').unlink()
    (runner.root / 'Gamma.java').write_text('package fixture; class Gamma {}\n')
    runner.command('update')
    samples['update']['changed-added-deleted'] = runner.classes()
    runner.command('update')
    samples['update']['unchanged'] = runner.classes()
    runner.command('restore', str(backup))
    samples['restore']['snapshot'] = runner.classes()
    code, _ = runner.command('restore', str(runner.database), acceptable=(0, 1))
    samples['restore']['same-source-rejected'] = code != 0
    samples['restore']['preserved'] = runner.classes()
    runner.command('clear')
    samples['clear']['database-removed'] = not runner.database.exists()
    _, output = runner.command('class', 'Alpha')
    samples['clear']['lookup-refused'] = 'Index not found.' in output
    runner.command(*rebuild)
    samples['rebuild']['after-clear'] = runner.classes()
    samples['watch-status']['idle'] = runner.status()
    runner.watch(samples)
    return expected, samples
