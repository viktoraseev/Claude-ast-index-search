"""Shared, request-bound oracle captures and bounded read-only reply caching."""
from collections import OrderedDict
from contextlib import contextmanager
import time
import threading

from common import canonical_json, oracle_response_id, now_ms


SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_responses(
 id TEXT PRIMARY KEY, tool TEXT NOT NULL,
 request_json TEXT NOT NULL, response_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oracle_pages(
 check_id TEXT NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
 page INTEGER NOT NULL, response_id TEXT NOT NULL REFERENCES oracle_responses(id),
 PRIMARY KEY(check_id,page)
);
CREATE INDEX IF NOT EXISTS oracle_pages_response ON oracle_pages(response_id);
CREATE VIEW IF NOT EXISTS pages AS
 SELECT p.check_id,p.page,r.request_json,r.response_json,r.tool
 FROM oracle_pages p JOIN oracle_responses r ON r.id=p.response_id;
CREATE TRIGGER IF NOT EXISTS pages_insert INSTEAD OF INSERT ON pages BEGIN
 INSERT OR IGNORE INTO oracle_responses VALUES(
  oracle_response_id(NEW.tool,NEW.request_json,NEW.response_json),NEW.tool,NEW.request_json,NEW.response_json);
 INSERT OR REPLACE INTO oracle_pages VALUES(
  NEW.check_id,NEW.page,oracle_response_id(NEW.tool,NEW.request_json,NEW.response_json));
END;
CREATE TRIGGER IF NOT EXISTS pages_delete INSTEAD OF DELETE ON pages BEGIN
 DELETE FROM oracle_pages WHERE check_id=OLD.check_id AND page=OLD.page;
END;
CREATE TABLE IF NOT EXISTS oracle_cache(
 request_key TEXT PRIMARY KEY,response_id TEXT NOT NULL REFERENCES oracle_responses(id)
);
CREATE TABLE IF NOT EXISTS oracle_failures(
 id TEXT PRIMARY KEY,tool TEXT NOT NULL,request_json TEXT NOT NULL,
 kind TEXT NOT NULL,response_json TEXT NOT NULL,attempts INTEGER NOT NULL,
 captured_at INTEGER NOT NULL
);
CREATE VIEW IF NOT EXISTS invocation_cache AS
 SELECT c.request_key,r.response_json FROM oracle_cache c JOIN oracle_responses r ON r.id=c.response_id;
CREATE TRIGGER IF NOT EXISTS invocation_cache_delete INSTEAD OF DELETE ON invocation_cache BEGIN
 DELETE FROM oracle_cache WHERE request_key=OLD.request_key;
END;
CREATE TABLE IF NOT EXISTS performance(
 category TEXT PRIMARY KEY, count INTEGER NOT NULL, seconds REAL NOT NULL, bytes INTEGER NOT NULL
);
"""


class Metrics:
    """Only aggregate timings; no query, path, source or response content."""
    def __init__(self, state):
        self.state, self.pending = state, {}
        self.lock = threading.Lock()

    def record(self, category, seconds=0.0, byte_count=0):
        with self.lock:
            item = self.pending.setdefault(category, [0, 0.0, 0])
            item[0] += 1
            item[1] += seconds
            item[2] += byte_count

    def flush(self):
        with self.lock:
            samples, self.pending = self.pending, {}
        self.state.executemany("""INSERT INTO performance VALUES (?,?,?,?)
            ON CONFLICT(category) DO UPDATE SET count=count+excluded.count,
            seconds=seconds+excluded.seconds,bytes=bytes+excluded.bytes""",
            ((key, *value) for key, value in samples.items()))

    def summary(self):
        with self.state:
            self.flush()
        return {row['category']: {'count': row['count'], 'seconds': round(row['seconds'], 6),
                                 'bytes': row['bytes']} for row in self.state.execute('SELECT * FROM performance')}

    @contextmanager
    def checkpoint(self, category):
        started = time.perf_counter()
        try:
            with self.state:
                yield
                self.flush()
        finally:
            self.record(category, time.perf_counter() - started)


def _readonly(*args, **kwargs):
    raise TypeError('oracle replies are read-only; copy before modifying')


class ReadOnlyDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly


class ReadOnlyList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly


def freeze(value):
    if isinstance(value, dict):
        return ReadOnlyDict((key, freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return ReadOnlyList(freeze(item) for item in value)
    return value


class Reply(ReadOnlyDict):
    def __init__(self, response, state, response_id, tool, request_json, byte_count):
        super().__init__((key, freeze(value)) for key, value in response.items())
        self.state, self.response_id, self.tool = state, response_id, tool
        self.request_json, self.byte_count = request_json, byte_count


class ReplyCache:
    def __init__(self, max_entries=256, max_bytes=16 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries, self.bytes = OrderedDict(), 0

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        self.entries.move_to_end(key)
        return entry[0]

    def put(self, key, reply):
        # Charge conservatively for Python containers, not just wire bytes.
        cost = reply.byte_count * 8 + len(reply.request_json.encode()) * 8 + 512
        if cost > self.max_bytes or self.max_entries <= 0:
            return
        old = self.entries.pop(key, None)
        if old:
            self.bytes -= old[1]
        self.entries[key] = (reply, cost)
        self.bytes += cost
        while len(self.entries) > self.max_entries or self.bytes > self.max_bytes:
            _, (_, charge) = self.entries.popitem(last=False)
            self.bytes -= charge


class OracleStore:
    def __init__(self, state, metrics=None):
        self.state, self.metrics = state, metrics or Metrics(state)

    def capture_failure(self, tool, arguments, error):
        request, response = canonical_json(arguments), canonical_json(error.response)
        identity = oracle_response_id(error.kind + ':' + tool, request, response)
        # Failed diagnostics never enter successful responses, pages or caches.
        # A repeated remote failure increments its count instead of copying it.
        self.state.execute('''INSERT INTO oracle_failures VALUES (?,?,?,?,?,1,?)
            ON CONFLICT(id) DO UPDATE SET attempts=attempts+1,captured_at=excluded.captured_at''',
            (identity, tool, request, error.kind, response, now_ms()))
        self.metrics.record('oracle.failure_capture', byte_count=len(response.encode()))

    def capture(self, tool, arguments, response):
        started = time.perf_counter()
        request = canonical_json(arguments)
        if isinstance(response, Reply) and response.state is self.state and response.tool == tool and response.request_json == request:
            return response
        serialized = canonical_json(response)
        identity = oracle_response_id(tool, request, serialized)
        self.state.execute('INSERT OR IGNORE INTO oracle_responses VALUES (?,?,?,?)',
                           (identity, tool, request, serialized))
        self.metrics.record('oracle.capture', time.perf_counter() - started, len(serialized.encode()))
        if isinstance(response, dict):
            return Reply(response, self.state, identity, tool, request, len(serialized.encode()))
        return identity

    def page(self, check_id, page, tool, arguments, response):
        started = time.perf_counter()
        capture = self.capture(tool, arguments, response)
        identity = capture.response_id if isinstance(capture, Reply) else capture
        # Commit at the check checkpoint, not on every cached lookup. Unique
        # network replies are separately made durable by InvocationOracle.
        self.state.execute('INSERT OR REPLACE INTO oracle_pages VALUES (?,?,?)', (check_id, page, identity))
        self.metrics.record('oracle.page_link', time.perf_counter() - started)
