"""Inventory and lexical contracts for Kotlin/Swift line-search commands.

Inventory covers every file type, including ignored directories. It never
follows links outside the exact target. File rows and source fingerprints are
private SQLite evidence; no source content is copied into public fixtures.
"""
import hashlib
import os
from pathlib import Path
import re

from common import ToolError, canonical_json, file_sha256, stable_id


EXTENSIONS = {
    'suspend': ('.kt', '.kts'),
    'flows': ('.kt', '.kts'),
    'extensions': ('.kt', '.kts', '.swift'),
    'publishers': ('.swift',),
    'main-actor': ('.swift',),
}
SCHEMA = '''CREATE TABLE IF NOT EXISTS file_inventory(
    path TEXT PRIMARY KEY, extension TEXT NOT NULL, kind TEXT NOT NULL,
    size INTEGER NOT NULL, modified INTEGER NOT NULL, sha256 TEXT NOT NULL
);'''

# Fingerprint every source type used by a lexical contract, including Perl
# files that are not in the Java navigation snapshot.
LEXICAL_EXTENSIONS = {'.kt', '.kts', '.swift', '.pm', '.pl', '.pod', '.t'}


def inventory_rows(root):
    """Bound memory to one directory listing and a streamed file hash."""
    def fail(error):
        raise error
    for directory, names, files in os.walk(root, followlinks=False, onerror=fail):
        names.sort()
        files.sort()
        for name in [*files, *(name for name in names if (Path(directory) / name).is_symlink())]:
            path = Path(directory) / name
            stat = path.lstat()
            kind = 'link-directory' if name in names else 'link-file' if path.is_symlink() else 'file'
            extension = path.suffix.lower()
            fingerprint = (os.readlink(path) if path.is_symlink() else
                           file_sha256(path) if extension in LEXICAL_EXTENSIONS else '')
            yield (path.relative_to(root).as_posix(), extension, kind,
                   stat.st_size, stat.st_mtime_ns, fingerprint)


def inventory_snapshot(root):
    digest = hashlib.sha256()
    for row in inventory_rows(root):
        digest.update((canonical_json(row) + '\n').encode())
    return digest.hexdigest()


def inventory(state, root):
    digest = hashlib.sha256()
    # An interrupted walk cannot leave a reusable absence assertion.
    with state:
        state.execute('DELETE FROM file_inventory')
        for row in inventory_rows(root):
            state.execute('INSERT INTO file_inventory VALUES (?,?,?,?,?,?)', row)
            digest.update((canonical_json(row) + '\n').encode())
        state.execute("INSERT OR REPLACE INTO metadata VALUES ('inventory_sha256',?)", (digest.hexdigest(),))
    return digest.hexdigest()


def applicable_paths(state, feature, contracts=EXTENSIONS):
    extensions = contracts[feature]
    return state.execute('SELECT * FROM file_inventory WHERE extension IN (' +
                         ','.join('?' for _ in extensions) + ') ORDER BY path', extensions)


def applicability(state, feature, contracts=EXTENSIONS):
    if not state.execute("SELECT 1 FROM metadata WHERE key='inventory_sha256'").fetchone():
        return 'pending', 'full file-type inventory has not been completed'
    if state.execute("SELECT 1 FROM file_inventory WHERE kind='link-directory' LIMIT 1").fetchone():
        return 'pending', 'inventory contains an untraversed directory link; absence cannot be established'
    rows = applicable_paths(state, feature, contracts)
    first = next(rows, None)
    if first is None:
        return 'inapplicable', 'independent source inventory: no ' + '/'.join(contracts[feature]) + ' files; CLI empty-result checks required'
    if state.execute('SELECT 1 FROM file_inventory WHERE kind!=\'file\' AND extension IN (' +
                     ','.join('?' for _ in contracts[feature]) + ') LIMIT 1', contracts[feature]).fetchone():
        return 'pending', 'relevant source link is not followed; source scope unresolved'
    if any(Path(row['path']).suffix != row['extension'] for row in applicable_paths(state, feature, contracts)):
        return 'pending', 'uppercase source suffix requires a separate CLI/oracle scope contract'
    # Presence in the full inventory proves applicability, but does not prove
    # that the CLI and IDE search the same ignored/generated source scope.
    # Keep that unresolved rather than manufacture a production mismatch.
    source = Path(__file__).resolve().parents[2] / 'src' / 'indexer.rs'
    body = source.read_text().split('const EXCLUDED_DIRS:', 1)[1].split('];', 1)[0]
    excluded = set(re.findall(r'"([^"]+)"', body))
    if any(any(part.startswith('.') or part in excluded for part in Path(row['path']).parts[:-1])
           for row in applicable_paths(state, feature, contracts)):
        return 'pending', 'applicable source exists in hidden/generated directories; CLI/oracle source scope alignment remains unresolved'
    if any(Path(row['path']).name in {'.gitignore', '.arcignore', '.ignore'} and row['size']
           for row in state.execute('SELECT path,size FROM file_inventory')):
        return 'pending', 'applicable source exists alongside ignore rules; CLI/oracle ignored-file alignment remains unresolved'
    return 'implemented', 'live MCP lexical text locations, name/line filters and ordered limits; not semantic navigation'


def query_pattern(feature, query, extension):
    if feature == 'suspend':
        return r'\bsuspend\s+fun\s+'
    if feature == 'flows':
        return r'\b(?:MutableStateFlow|MutableSharedFlow|StateFlow|SharedFlow|Flow)\s*<'
    if feature == 'publishers':
        return r'\b(?:PassthroughSubject|CurrentValueSubject|AnyPublisher)\b\s*[<(]|@Published\b'
    if feature == 'main-actor':
        return r'@MainActor\b'
    receiver = re.escape(query)
    return (rf'\bextension\s+{receiver}(?:\s|[<:{{]|$)' if extension == '.swift' else
            rf'\bfun\s+{receiver}\.(\w+)')


def accepts(feature, query, line):
    if feature == 'suspend':
        # The oracle confirms a broad lexical anchor. Independently extract
        # the function name; a receiver/body occurrence is not a name filter.
        tail = re.search(r'\bsuspend\s+fun\s+(.*)', line)
        if not tail:
            return False
        signature = re.sub(r'^<[^>]*>\s*', '', tail[1])
        match = re.search(r'`?(\w+)`?\s*[(<]', signature)
        if not match:
            return False
        name = match[1]
        # A generic receiver can precede the function; choose the name
        # immediately before the parameter list, rather than its type name.
        method = re.search(r'\.\s*`?(\w+)`?\s*\(', signature)
        if method:
            name = method[1]
        return not query or query.lower() in name.lower()
    return feature == 'extensions' or not query or query.lower() in line.lower()


def output_locations(feature, output, root, limit):
    headers = {'suspend': 'Suspend functions', 'flows': 'Flow declarations',
               'publishers': 'Combine publishers', 'main-actor': '@MainActor usages',
               'extensions': 'Extensions for .+'}
    header = headers[feature] if feature == 'extensions' else re.escape(headers[feature])
    lines = output.splitlines()
    count = re.fullmatch(header + r' \((\d+)\):', lines[0]) if lines else None
    if count is None:
        raise ToolError('unrecognized mobile search header')
    entries = []
    for line in lines[1:]:
        if line.startswith('    ') or not line.strip():
            continue
        patterns = {
            'suspend': r'  .+?: (.+):(\d+)',
            'flows': r'  \[[^]]+\] (.+):(\d+)',
            'publishers': r'  (?:@Published|PassthroughSubject|CurrentValueSubject|AnyPublisher) (.+):(\d+)',
            'main-actor': r'  (.+):(\d+)',
        }
        if feature == 'extensions':
            match = re.fullmatch(r'  (.+\.swift):(\d+) .*', line) or re.fullmatch(r'  .+?: (.+):(\d+)', line)
        else:
            match = re.fullmatch(patterns[feature], line)
        if not match or int(match[2]) < 1:
            raise ToolError('unrecognized mobile search location')
        path = Path(match[1])
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError as error:
                raise ToolError('mobile search path outside target') from error
        if '..' in path.parts or path.suffix not in EXTENSIONS[feature]:
            raise ToolError('mobile search path outside language scope')
        entries.append((path.as_posix(), int(match[2])))
    if len(entries) != int(count[1]) or len(entries) > limit or len(set(entries)) != len(entries):
        raise ToolError('mobile search count does not match unique rendered locations')
    return entries


def plan_mobile(state, root):
    if root is None:
        return
    inventory(state, root)
    with state:
        for feature in EXTENSIONS:
            status, reason = applicability(state, feature)
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, status, reason))
            queries = ['String', '__audit_absent_receiver__'] if feature == 'extensions' else [None, '', 'a', 'A', '[', '__audit_absent_mobile__']
            for query in queries:
                subject = canonical_json({'query': query})
                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                              (stable_id({'feature': feature, 'subject': subject}), feature, subject))
            # Cover project receiver names too, rather than repeatedly asking
            # about a hard-coded type that might have no extension declarations.
            if feature == 'extensions' and status == 'implemented':
                for row in applicable_paths(state, feature):
                    pattern = (r'\bextension\s+([\w.]+)' if row['extension'] == '.swift' else
                               r'\bfun\s+([\w.]+)\.')
                    with (root / row['path']).open(encoding='utf-8') as source:
                        for line in source:
                            for match in re.finditer(pattern, line):
                                subject = canonical_json({'query': match[1]})
                                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                              (stable_id({'feature': feature, 'subject': subject}), feature, subject))
