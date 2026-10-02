"""Isolated native rebuild with durable logs and no project payload on stdout."""
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from common import ToolError, connect, file_sha256


def freeze_binary(binary: Path, directory: Path, expected_hash: str) -> Path:
    """Pin an executable so concurrent Cargo builds cannot change an audit."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "ast-index"
    if target.resolve() == binary.resolve():
        raise ToolError("binary snapshot must be separate from the build output")
    if target.exists():
        if file_sha256(target) != expected_hash:
            raise ToolError("pinned binary differs from the audit epoch")
        return target
    descriptor, name = tempfile.mkstemp(prefix=".binary-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, binary.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if file_sha256(temporary) != expected_hash:
            raise ToolError("binary changed while creating its snapshot")
        temporary.chmod(0o755)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def build_ast_index(binary: str, root: Path, database: Path, snapshot: str,
                    threads: int = 4, rebuild: bool = False) -> None:
    if database.exists() and not rebuild:
        state = connect(database)
        try:
            row = state.execute("SELECT value FROM metadata WHERE key='audit_source_snapshot'").fetchone()
            if row and row[0] == snapshot:
                return
        except sqlite3.Error:
            pass
        finally:
            state.close()
    database.parent.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "AST_INDEX_DB_PATH": str(database),
                   "AST_INDEX_CACHE_DIR": str(database.parent / "cache"), "NO_COLOR": "1"}
    command = [binary, "rebuild", "--force", "--max-files", "0", "--threads", str(threads)]
    with database.with_suffix(".build.stdout.log").open("ab") as stdout, database.with_suffix(".build.stderr.log").open("ab") as stderr:
        result = subprocess.run(command, cwd=root, env=environment, stdout=stdout, stderr=stderr)
    if result.returncode:
        raise ToolError(f"native rebuild failed ({result.returncode}); see artifact build logs")
    state = connect(database)
    try:
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('audit_source_snapshot',?)", (snapshot,))
    finally:
        state.close()
