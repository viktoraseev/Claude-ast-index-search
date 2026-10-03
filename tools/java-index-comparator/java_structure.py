"""Independent Java syntax oracle; all generated files stay in private artifacts."""
import atexit
import fcntl
import os
import json
from pathlib import Path
import subprocess
import selectors
import tempfile
from collections import OrderedDict

from common import ToolError, file_sha256


class JavaStructure:
    def __init__(self, directory: Path):
        source = Path(__file__).with_name('JavaStructure.java')
        base = directory / 'java-structure'
        base.mkdir(parents=True, exist_ok=True)
        classes = base / file_sha256(source)[:20]
        with (base / 'compile.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not classes.is_dir():
                with tempfile.TemporaryDirectory(dir=base) as staging:
                    result = subprocess.run(['javac', '-d', staging, str(source)], capture_output=True, timeout=60)
                    if result.returncode:
                        raise ToolError('JDK 17+ is required for independent Java structure checks')
                    os.replace(staging, classes)
        self.process = subprocess.Popen(['java', '-cp', str(classes), 'JavaStructure'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        atexit.register(self.close)

    def read(self, path: Path, *, document=False):
        if '\n' in str(path):
            raise ToolError('Java structure path contains a newline')
        self.process.stdin.write(str(path) + '\n')
        self.process.stdin.flush()
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            if not ready.select(timeout=60):
                self.process.kill()
                raise ToolError('Java structure oracle timed out')
        response = self.process.stdout.readline()
        if not response:
            raise ToolError('Java structure oracle terminated')
        value = json.loads(response)
        if 'error' in value:
            raise ToolError(value['error'])
        return value if document else value['entries']

    def close(self):
        atexit.unregister(self.close)
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


_SERVERS = OrderedDict()


def structure_server(directory: Path):
    directory = directory.resolve()
    if directory not in _SERVERS:
        if len(_SERVERS) >= 2:
            _SERVERS.popitem(last=False)[1].close()
        _SERVERS[directory] = JavaStructure(directory)
    _SERVERS.move_to_end(directory)
    return _SERVERS[directory]
