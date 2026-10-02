"""Isolated native rebuild with durable logs and no project payload on stdout."""
import os
from pathlib import Path
import sqlite3
import subprocess

from common import ToolError, connect


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
