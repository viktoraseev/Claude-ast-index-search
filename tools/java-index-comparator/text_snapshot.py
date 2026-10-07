"""Bounded, request-provenanced MCP full-line snapshots for literal searches.

This is text evidence only, never a replacement for semantic navigation.
Every stored line is confirmed by both its MCP location and full-line preview.
"""
import hashlib
import json
import re
import time

from common import ToolError, McpRemoteError, canonical_json, file_sha256, java_files, oracle_response_id
from oracle_store import OracleStore, Reply


SCHEMA = '''
CREATE TABLE IF NOT EXISTS text_snapshot_files(
 path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS text_snapshot_lines(
 path TEXT NOT NULL REFERENCES text_snapshot_files(path),line INTEGER NOT NULL,content TEXT NOT NULL,
 PRIMARY KEY(path,line)
);
CREATE TABLE IF NOT EXISTS text_snapshot_pages(
 path TEXT NOT NULL REFERENCES text_snapshot_files(path),page INTEGER NOT NULL,
 response_id TEXT NOT NULL REFERENCES oracle_responses(id),PRIMARY KEY(path,page)
);
CREATE TABLE IF NOT EXISTS text_snapshot_dependencies(
 check_id TEXT PRIMARY KEY REFERENCES checks(id)
);
'''


class SnapshotUnavailable(ToolError):
    """An optimization is inapplicable; use ordinary request-bound searches."""


def source_lines(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise SnapshotUnavailable('full-line snapshot source budget exceeded')
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise SnapshotUnavailable('full-line snapshot source budget exceeded')
    try:
        return data, [line.removesuffix('\r') for line in data.decode('utf-8').split('\n')]
    except UnicodeError as error:
        raise SnapshotUnavailable('full-line snapshot needs strict UTF-8') from error


def confirm_lines(root, relative, lines, matches):
    expected = {index for index, line in enumerate(lines, 1) if line}
    actual = set()
    for item in matches:
        file = item.get('file', item.get('path'))
        if file == str(root / relative):
            file = relative
        line = item.get('line')
        if (file != relative or type(line) is not int or line not in expected or
                item.get('context') != lines[line - 1].strip()):
            raise SnapshotUnavailable('MCP full-line evidence differs from source')
        actual.add(line)
    if actual != expected:
        raise SnapshotUnavailable('MCP full-line evidence is incomplete')


class TextSnapshot:
    GROUP_FILES = 16
    GROUP_LINES = 4000
    GROUP_BYTES = 8 * 1024 * 1024

    def __init__(self, root, state, client, metrics):
        self.root, self.state, self.client, self.metrics = root, state, client, metrics
        self.store = OracleStore(state, metrics)
        self._ready = False

    @staticmethod
    def eligible(query):
        # Trimming IDE previews cannot prove leading/trailing whitespace. Java
        # identifiers/annotation names need no such whitespace-sensitive truth.
        return bool(re.fullmatch(r'@?[\w$]+', query))

    def arguments(self, relative):
        return {'project_path': str(self.root), 'paths': [relative], 'filePattern': '*.java',
                'query': '(?m)^.+$', 'regex': True, 'caseSensitive': True,
                'context': 'all', 'pageSize': 500}

    def _call(self, arguments):
        for attempt in range(3):
            try:
                return self.client.call('ide_search_text', arguments)
            except ToolError as error:
                transient = (not isinstance(error, McpRemoteError)
                             and str(error) == 'MCP HTTP request failed for tools/call: TimeoutError')
                if not transient or attempt == 2:
                    raise
                self.metrics.record('oracle.text_snapshot_timeout_retry')
                time.sleep(0.2 * (attempt + 1))

    def file(self, relative, path):
        data, lines = source_lines(path)
        fingerprint = file_sha256(path)
        existing = self.state.execute('SELECT sha256,status FROM text_snapshot_files WHERE path=?', (relative,)).fetchone()
        if existing and existing['status'] == 'complete' and existing['sha256'] == fingerprint:
            return
        with self.state:
            self.state.execute('''INSERT INTO text_snapshot_files VALUES (?,?,?)
                ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,status=excluded.status''',
                               (relative, fingerprint, 'pending'))
            self.state.execute('DELETE FROM text_snapshot_lines WHERE path=?', (relative,))
            self.state.execute('DELETE FROM text_snapshot_pages WHERE path=?', (relative,))
        arguments, cursors, collected, page = self.arguments(relative), set(), [], 0
        while True:
            response = self._call(arguments)
            if not isinstance(response, dict) or not isinstance(response.get('matches'), list):
                raise SnapshotUnavailable('MCP full-line response is unknown')
            if response.get('stale') or response.get('truncated'):
                raise ToolError('MCP full-line response is stale or truncated')
            if any(not isinstance(item, dict) for item in response['matches']):
                raise SnapshotUnavailable('MCP full-line response has invalid matches')
            with self.state:
                capture = self.store.capture('ide_search_text', arguments, response)
                identity = capture.response_id if isinstance(capture, Reply) else capture
                self.state.execute('INSERT INTO text_snapshot_pages VALUES (?,?,?)', (relative, page, identity))
            collected.extend(response['matches'])
            cursor = response.get('nextCursor')
            if response.get('hasMore') and not cursor:
                raise ToolError('MCP full-line pagination lacks a cursor')
            if len(collected) >= 5000:
                raise SnapshotUnavailable('MCP full-line collection cap reached')
            if not cursor:
                break
            if cursor in cursors:
                raise ToolError('MCP full-line pagination repeats a cursor')
            cursors.add(cursor)
            page += 1
            arguments = {'project_path': str(self.root), 'pageSize': 500, 'cursor': cursor}
        confirm_lines(self.root, relative, lines, collected)
        if path.read_bytes() != data:
            raise ToolError('source changed during full-line collection')
        with self.state:
            self.state.executemany('INSERT INTO text_snapshot_lines VALUES (?,?,?)',
                                   ((relative, number, line) for number, line in enumerate(lines, 1) if line))
            self.state.execute("UPDATE text_snapshot_files SET status='complete' WHERE path=?", (relative,))

    def group(self, entries):
        if getattr(self, '_safe_group_files', self.GROUP_FILES) == 1:
            for relative, path, _, _ in entries:
                self.file(relative, path)
            return
        try:
            self._group(entries)
        except (SnapshotUnavailable, McpRemoteError) as error:
            if len(entries) == 1:
                raise
            if isinstance(error, McpRemoteError):
                response = error.response
                rpc_error = response.get('error') if isinstance(response, dict) else None
                if error.kind != 'rpc' or not isinstance(rpc_error, dict) or rpc_error.get('code') != -32602:
                    raise
            # Unproved group capability is not a native mismatch. Fall back to
            # the original per-file proof, never to a partial local snapshot.
            self._safe_group_files = 1
            for relative, path, _, _ in entries:
                self.file(relative, path)

    def _group(self, entries):
        if len(entries) == 1:
            self.file(entries[0][0], entries[0][1])
            return
        arguments = self.arguments(entries[0][0])
        arguments['paths'] = [relative for relative, _, _, _ in entries]
        allowed = set(arguments['paths'])
        with self.state:
            for relative, _, data, _ in entries:
                self.state.execute('''INSERT INTO text_snapshot_files VALUES (?,?,?)
                    ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,status=excluded.status''',
                                   (relative, hashlib.sha256(data).hexdigest(), 'pending'))
                self.state.execute('DELETE FROM text_snapshot_lines WHERE path=?', (relative,))
                self.state.execute('DELETE FROM text_snapshot_pages WHERE path=?', (relative,))
        collected, cursors, page = [], set(), 0
        while True:
            response = self._call(arguments)
            if not isinstance(response, dict) or not isinstance(response.get('matches'), list):
                raise SnapshotUnavailable('MCP grouped full-line response is unknown')
            if response.get('stale') or response.get('truncated'):
                raise ToolError('MCP grouped full-line response is stale or truncated')
            for item in response['matches']:
                if not isinstance(item, dict):
                    raise SnapshotUnavailable('MCP grouped response has invalid matches')
                location = item.get('file', item.get('path'))
                if isinstance(location, str) and location.startswith(str(self.root) + '/'):
                    location = location[len(str(self.root)) + 1:]
                if location not in allowed:
                    raise SnapshotUnavailable('MCP grouped response has foreign locations')
            with self.state:
                capture = self.store.capture('ide_search_text', arguments, response)
                identity = capture.response_id if isinstance(capture, Reply) else capture
                self.state.executemany('INSERT INTO text_snapshot_pages VALUES (?,?,?)',
                                       ((relative, page, identity) for relative in sorted(allowed)))
            collected.extend(response['matches'])
            cursor = response.get('nextCursor')
            if response.get('hasMore') and not cursor:
                raise ToolError('MCP grouped full-line pagination lacks a cursor')
            if len(collected) >= 5000:
                raise SnapshotUnavailable('MCP grouped full-line collection cap reached')
            if not cursor:
                break
            if cursor in cursors:
                raise ToolError('MCP grouped full-line pagination repeats a cursor')
            cursors.add(cursor)
            page += 1
            arguments = {'project_path': str(self.root), 'pageSize': 500, 'cursor': cursor}
        for relative, path, data, lines in entries:
            matches = [item for item in collected
                       if item.get('file', item.get('path')) in (relative, str(self.root / relative))]
            confirm_lines(self.root, relative, lines, matches)
            if path.read_bytes() != data:
                raise ToolError('source changed during grouped full-line collection')
        with self.state:
            for relative, _, _, lines in entries:
                self.state.executemany('INSERT INTO text_snapshot_lines VALUES (?,?,?)',
                                       ((relative, number, line) for number, line in enumerate(lines, 1) if line))
                self.state.execute("UPDATE text_snapshot_files SET status='complete' WHERE path=?", (relative,))

    def ensure(self):
        if self._ready:
            return
        marker = self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_project_root'").fetchone()
        if marker and marker[0] != str(self.root):
            raise ToolError('full-line snapshot belongs to another target')
        ready = self.state.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone()
        if ready and ready[0] == 'true':
            stored = {row['path']: row['sha256'] for row in self.state.execute(
                "SELECT path,sha256 FROM text_snapshot_files WHERE status='complete'")}
            for path in java_files(self.root):
                relative = path.relative_to(self.root).as_posix()
                if stored.pop(relative, None) != file_sha256(path):
                    raise ToolError('full-line snapshot source changed')
            if stored:
                raise ToolError('full-line snapshot source inventory changed')
            self._ready = True
            return
        with self.state:
            self.state.execute("INSERT OR REPLACE INTO metadata VALUES ('text_snapshot_project_root',?)", (str(self.root),))
        pending, byte_count, line_count = [], 0, 0
        for path in java_files(self.root):
            relative = path.relative_to(self.root).as_posix()
            cached = self.state.execute('SELECT sha256,status FROM text_snapshot_files WHERE path=?', (relative,)).fetchone()
            if cached and cached['status'] == 'complete' and cached['sha256'] == file_sha256(path):
                continue
            data, lines = source_lines(path)
            count = sum(bool(line) for line in lines)
            if pending and (len(pending) >= getattr(self, '_safe_group_files', self.GROUP_FILES) or byte_count + len(data) > self.GROUP_BYTES
                            or line_count + count > self.GROUP_LINES):
                self.group(pending)
                pending, byte_count, line_count = [], 0, 0
            pending.append((relative, path, data, lines))
            byte_count += len(data)
            line_count += count
        if pending:
            self.group(pending)
        with self.state:
            self.state.execute("INSERT OR REPLACE INTO metadata VALUES ('text_snapshot_complete','true')")
        self._ready = True

    def search(self, check_id, query):
        self.ensure()
        rows = self.state.execute('SELECT path,line FROM text_snapshot_lines WHERE instr(content,?)>0 '
                                  'ORDER BY path,line LIMIT 1000001', (query,)).fetchall()
        if len(rows) > 1000000:
            raise SnapshotUnavailable('literal snapshot result exceeds CLI verification budget')
        with self.state:
            self.state.execute('INSERT OR IGNORE INTO text_snapshot_dependencies VALUES (?)', (check_id,))
        self.metrics.record('oracle.text_snapshot_hit')
        return [{'file': row[0], 'line': row[1], 'provenance': 'MCP full-line snapshot'} for row in rows]


def copy_snapshot(source, destination, root):
    """Replay raw proof, not just derived rows; bounded to one source file."""
    ready = source.execute("SELECT value FROM metadata WHERE key='text_snapshot_complete'").fetchone()
    if not ready or ready[0] != 'true':
        raise ToolError('recorded full-line snapshot is incomplete')
    files = {row['path']: row for row in source.execute('SELECT * FROM text_snapshot_files')}
    seen = set()
    for path in java_files(root):
        relative = path.relative_to(root).as_posix()
        row = files.get(relative)
        if row is None or row['status'] != 'complete' or row['sha256'] != file_sha256(path):
            raise ToolError('recorded full-line source inventory differs')
        data, lines = source_lines(path)
        matches, cursor, pages, cursors = [], None, [], set()
        initial = TextSnapshot(root, destination, None, None).arguments(relative)
        allowed = None
        for page in source.execute('''SELECT p.page,r.* FROM text_snapshot_pages p
            JOIN oracle_responses r ON r.id=p.response_id WHERE p.path=? ORDER BY p.page''', (relative,)):
            request, response = json.loads(page['request_json']), json.loads(page['response_json'])
            if page['id'] != oracle_response_id(page['tool'], page['request_json'], page['response_json']):
                raise ToolError('recorded full-line response/request identity is invalid')
            if not pages:
                paths = request.get('paths') if isinstance(request, dict) else None
                if (not isinstance(paths, list) or not paths or len(paths) > TextSnapshot.GROUP_FILES
                        or any(not isinstance(p, str) or p not in files for p in paths)
                        or len(set(paths)) != len(paths) or relative not in paths):
                    raise ToolError('recorded full-line group paths are invalid')
                allowed = set(paths)
                initial['paths'] = paths
            expected_request = initial if not pages else {'project_path': str(root), 'pageSize': 500, 'cursor': cursor}
            if (page['page'] != len(pages) or page['tool'] != 'ide_search_text' or request != expected_request or
                    (pages and not cursor) or response.get('stale') or response.get('truncated') or
                    not isinstance(response.get('matches'), list) or
                    any(not isinstance(item, dict) for item in response.get('matches', [])) or
                    (response.get('hasMore') and not response.get('nextCursor'))):
                raise ToolError('recorded full-line pagination proof is invalid')
            for item in response['matches']:
                location = item.get('file', item.get('path'))
                if isinstance(location, str) and location.startswith(str(root) + '/'):
                    location = location[len(str(root)) + 1:]
                if location not in allowed:
                    raise ToolError('recorded full-line group contains foreign locations')
            matches.extend(response['matches'])
            cursor = response.get('nextCursor')
            if cursor and cursor in cursors:
                raise ToolError('recorded full-line cursor repeats')
            if cursor:
                cursors.add(cursor)
            pages.append(page)
            if len(matches) >= 5000:
                raise ToolError('recorded full-line proof exceeds collection cap')
        if not pages or cursor:
            raise ToolError('recorded full-line pagination proof is incomplete')
        confirm_lines(root, relative, lines,
                      [item for item in matches if item.get('file', item.get('path')) in (relative, str(root / relative))])
        # Recreate rows from source AFTER validating their raw MCP proof. A
        # corrupt derived table can never become replay's source of truth.
        with destination:
            destination.execute('''INSERT INTO text_snapshot_files VALUES (?,?,?)
                ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,status=excluded.status''',
                                (relative, row['sha256'], 'pending'))
            destination.execute('DELETE FROM text_snapshot_lines WHERE path=?', (relative,))
            destination.execute('DELETE FROM text_snapshot_pages WHERE path=?', (relative,))
            for page in pages:
                destination.execute('INSERT OR IGNORE INTO oracle_responses VALUES (?,?,?,?)',
                                    (page['id'], page['tool'], page['request_json'], page['response_json']))
                destination.execute('INSERT INTO text_snapshot_pages VALUES (?,?,?)', (relative, page['page'], page['id']))
            destination.executemany('INSERT INTO text_snapshot_lines VALUES (?,?,?)',
                                    ((relative, number, line) for number, line in enumerate(lines, 1) if line))
            destination.execute("UPDATE text_snapshot_files SET status='complete' WHERE path=?", (relative,))
        if path.read_bytes() != data:
            raise ToolError('source changed during full-line proof replay')
        seen.add(relative)
    if seen != files.keys():
        raise ToolError('recorded full-line inventory has extra files')
    with destination:
        destination.execute("INSERT OR REPLACE INTO metadata VALUES ('text_snapshot_complete','true')")
        destination.execute("INSERT OR REPLACE INTO metadata VALUES ('text_snapshot_project_root',?)", (str(root),))
