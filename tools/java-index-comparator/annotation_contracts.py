"""MCP annotation anchors bound to independent Java/Kotlin declarations.

Java binding uses javac. Kotlin binding uses a separate token reader and fails
closed on unsupported annotated signatures; it never consults native symbols.
These hybrid checks are not MCP semantic navigation equivalence.
"""
from pathlib import Path
from itertools import islice
import re
import subprocess

from common import ToolError, canonical_json, stable_id
import mobile_contracts
from java_structure import structure_server


EXTENSIONS = {'provides': ('.java', '.kt', '.kts'),
              'composables': ('.kt', '.kts'), 'previews': ('.kt', '.kts')}
ANNOTATIONS = {'provides': ('Provides', 'Binds'), 'composables': ('Composable',),
               'previews': ('Preview',)}
MAX_SOURCE_BYTES = 4 * 1024 * 1024


class UnresolvedSyntax(ToolError):
    pass


def java_scope(state):
    row = state.execute("SELECT value FROM metadata WHERE key='audit_scope'").fetchone()
    return row is not None and row[0] == 'java'


def scope_extensions(state):
    return {'provides': ('.java',)} if java_scope(state) else EXTENSIONS


def applicability(state, feature, root=None):
    if java_scope(state) and feature != 'provides':
        return 'out-of-scope', 'explicit Java-only repair scope; not checked and not passing'
    status, reason = mobile_contracts.applicability(state, feature, scope_extensions(state))
    if root is not None and status == 'pending' and 'alongside ignore rules' in reason:
        # Prove that the Git ignore scope does not remove any relevant source.
        # This is independent scope evidence, not native DB/MCP equivalence.
        proof = {'inventory_sha256': state.execute("SELECT value FROM metadata WHERE key='inventory_sha256'").fetchone()[0],
                 'source': 'independent git check-ignore --no-index -z --stdin',
                 'checked': 0, 'ignored': [], 'complete': False}
        if any(Path(row['path']).name in {'.ignore', '.arcignore'} and row['size']
               for row in state.execute('SELECT path,size FROM file_inventory')):
            reason = 'non-Git ignore scope is unresolved for annotation searches'
        elif not (root / '.git').is_dir() or (root / '.git').is_symlink():
            reason = 'Git ignore scope cannot be verified inside the exact target root'
        else:
            rows = applicable_paths(state, feature)
            while batch := list(islice(rows, 100)):
                payload = b''.join(row['path'].encode() + b'\0' for row in batch)
                result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '-z', '--stdin'],
                                        input=payload, capture_output=True, timeout=30)
                if result.returncode not in (0, 1):
                    reason = 'independent Git ignore scope probe failed'
                    break
                proof['checked'] += len(batch)
                if result.stdout:
                    proof['ignored'] = result.stdout.decode().rstrip('\0').split('\0')
                    reason = 'applicable annotation source is ignored; CLI/oracle scope alignment remains unresolved'
                    break
            else:
                proof['complete'] = True
                status = 'implemented'
        with state:
            state.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                          ('annotation_scope:' + feature, canonical_json(proof)))
    if status == 'implemented':
        binding = 'javac Java-only declaration binding' if java_scope(state) else 'javac/Kotlin token binding'
        reason = 'hybrid MCP/source annotation text anchors and independent ' + binding + '; filters and ordered limits; not MCP semantic navigation'
    return status, reason


def applicable_paths(state, feature):
    return mobile_contracts.applicable_paths(state, feature, scope_extensions(state))


def pattern(feature):
    return r'@(?:[\w$]+\.)*(?:' + '|'.join(ANNOTATIONS[feature]) + r')(?:[^\w$]|$)'


def balanced(tokens, start, opening, closing):
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index][0]
        if token == opening:
            depth += 1
        elif token == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    raise UnresolvedSyntax('unterminated annotated Kotlin signature')


def kotlin_code(content):
    """Mask literals and Kotlin's nested comments without changing line numbers."""
    output, index = [], 0
    while index < len(content):
        start = index
        if content.startswith('//', index):
            end = content.find('\n', index)
            index = len(content) if end < 0 else end
        elif content.startswith('/*', index):
            index += 2
            depth = 1
            while index < len(content) and depth:
                if content.startswith('/*', index):
                    depth += 1
                    index += 2
                elif content.startswith('*/', index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise UnresolvedSyntax('unterminated Kotlin comment')
        elif content.startswith('"""', index):
            end = content.find('"""', index + 3)
            if end < 0:
                raise UnresolvedSyntax('unterminated Kotlin raw string')
            index = end + 3
        elif content[index] in {'"', "'"}:
            quote = content[index]
            index += 1
            while index < len(content):
                if content[index] == '\\':
                    index += 2
                elif content[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise UnresolvedSyntax('unterminated Kotlin literal')
        elif content[index] == '`':
            end = content.find('`', index + 1)
            if end < 0:
                raise UnresolvedSyntax('unterminated Kotlin identifier')
            output.append(content[index:end + 1])
            index = end + 1
            continue
        else:
            output.append(content[index])
            index += 1
            continue
        fragment = content[start:index]
        if fragment.startswith('"') and '${' in fragment and any('@' + name in fragment for names in ANNOTATIONS.values() for name in names):
            raise UnresolvedSyntax('annotated Kotlin string interpolation requires a separate syntax contract')
        output.append(''.join('\n' if char == '\n' else ' ' for char in fragment))
    return ''.join(output)


def kotlin_declarations(content):
    """Read explicit annotated function signatures, retaining physical lines."""
    code = kotlin_code(content)
    tokens, line, previous = [], 1, 0
    for match in re.finditer(r'`[^`]+`|[\w$]+|[^\s]', code):
        line += code[previous:match.start()].count('\n')
        tokens.append((match[0], line))
        line += match[0].count('\n')
        previous = match.end()
    modifiers = set('public private protected internal inline noinline crossinline tailrec operator infix external suspend override open final abstract expect actual'.split())
    pending, index = [], 0
    while index < len(tokens):
        token, line = tokens[index]
        if token == '@':
            index += 1
            if index >= len(tokens):
                raise UnresolvedSyntax('incomplete Kotlin annotation')
            if tokens[index][0] in {'[', 'file', 'get', 'set', 'field', 'receiver', 'param', 'setparam', 'delegate'}:
                raise UnresolvedSyntax('grouped/use-site Kotlin annotations require a separate syntax contract')
            name = tokens[index][0]
            index += 1
            while index + 1 < len(tokens) and tokens[index][0] == '.':
                name = tokens[index + 1][0]
                index += 2
            if index < len(tokens) and tokens[index][0] == '(':
                index = balanced(tokens, index, '(', ')')
            pending.append((name, line))
            continue
        if token in modifiers:
            index += 1
            continue
        if token == 'fun' and pending:
            declaration = index
            index += 1
            if index < len(tokens) and tokens[index][0] == '<':
                index = balanced(tokens, index, '<', '>')
            name = None
            while index < len(tokens) and tokens[index][0] != '(':
                value, name_line = tokens[index]
                if value == '<':
                    index = balanced(tokens, index, '<', '>')
                    continue
                if value not in {'.', '?'}:
                    if not re.fullmatch(r'`[^`]+`|[\w$]+', value):
                        raise UnresolvedSyntax('unsupported annotated Kotlin receiver/name')
                    name = (value.strip('`'), name_line)
                index += 1
            if name is None or index >= len(tokens):
                raise UnresolvedSyntax('missing annotated Kotlin function name')
            index = balanced(tokens, index, '(', ')')
            return_type = None
            if index < len(tokens) and tokens[index][0] == ':':
                index += 1
                if index >= len(tokens) or not re.fullmatch(r'[\w$]+', tokens[index][0]):
                    raise UnresolvedSyntax('unsupported annotated Kotlin return type')
                return_type = tokens[index][0]
                index += 1
                while index + 1 < len(tokens) and tokens[index][0] == '.':
                    return_type += '.' + tokens[index + 1][0]
                    index += 2
            for annotation, anchor in pending:
                yield {'annotation': annotation, 'anchor': anchor, 'name': name[0],
                       'line': name[1], 'return_type': return_type, 'declaration': declaration}
            pending = []
            continue
        pending = []
        index += 1


def declarations(fixture, row):
    path = fixture.root / row['path']
    if row['size'] > MAX_SOURCE_BYTES:
        raise UnresolvedSyntax('annotation source exceeds bounded parser size')
    if row['extension'] == '.java':
        for entry in fixture.structure(row['path']):
            if entry['kind'] == 'annotation' and entry.get('method_name'):
                yield {'annotation': entry['name'][1:], 'anchor': entry['line'],
                       'name': entry['method_name'], 'line': entry['line'],
                       'return_type': entry['return_type'], 'declaration': entry['method_position']}
    else:
        yield from kotlin_declarations(path.read_text(encoding='utf-8'))


def output_locations(feature, output, root, limit):
    lines = output.splitlines()
    header = (r"Providers for '.*'" if feature == 'provides' else
              re.escape('@' + ANNOTATIONS[feature][0] + ' functions'))
    count = re.fullmatch(header + r' \((\d+)\):', lines[0]) if lines else None
    if not count:
        raise ToolError('unrecognized annotation function header')
    entries = []
    stride = 2 if feature == 'provides' else 1
    if (len(lines) - 1) % stride:
        raise ToolError('incomplete annotation function output')
    for index in range(1, len(lines), stride):
        match = re.fullmatch(r'  (.+):(\d+)' if feature == 'provides' else r'  (.+): (.+):(\d+)', lines[index])
        if not match or int(match[2 if feature == 'provides' else 3]) < 1 or (stride == 2 and not lines[index + 1].startswith('    ')):
            raise ToolError('invalid annotation function location')
        name, file, line = (None, match[1], int(match[2])) if feature == 'provides' else (match[1], match[2], int(match[3]))
        path = Path(file)
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError as error:
                raise ToolError('annotation function path outside target') from error
        if '..' in path.parts or path.suffix not in EXTENSIONS[feature]:
            raise ToolError('annotation function path outside source scope')
        entries.append((path.as_posix(), line) if name is None else (path.as_posix(), line, name))
    if len(entries) != int(count[1]) or len(entries) > limit:
        raise ToolError('annotation function count differs from rendered locations')
    return entries


def plan_annotations(state, root):
    if root is None:
        return
    class PlanningSource:
        def __init__(self):
            self.root = root

        def structure(self, file):
            directory = Path(state.execute('PRAGMA database_list').fetchone()[2]).parent
            return structure_server(directory).read(root / file)

    source = PlanningSource()
    # The shared full-type inventory was atomically completed by plan_mobile.
    with state:
        for feature in EXTENSIONS:
            status, reason = applicability(state, feature, root)
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, status, reason))
            if status == 'out-of-scope':
                continue
            if status == 'implemented':
                try:
                    for row in applicable_paths(state, feature):
                        if row['size'] > MAX_SOURCE_BYTES:
                            raise UnresolvedSyntax('annotation source exceeds bounded parser size')
                        with (root / row['path']).open(encoding='utf-8') as stream:
                            has_anchor = any(re.search(pattern(feature), line) for line in stream)
                        if not has_anchor:
                            continue
                        for entry in declarations(source, row):
                            if entry['annotation'] not in ANNOTATIONS[feature]:
                                continue
                            filters = ((entry['return_type'], entry['return_type'].rsplit('.', 1)[-1])
                                       if feature == 'provides' and entry['return_type'] else
                                       () if feature == 'provides' else (entry['name'], entry['name'].upper()))
                            for query in filters:
                                subject = canonical_json({'query': query})
                                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                                              (stable_id({'feature': feature, 'subject': subject}), feature, subject))
                except UnresolvedSyntax as error:
                    state.execute("UPDATE coverage SET status='pending',reason=? WHERE feature=?", (str(error), feature))
            queries = ('', 'a', 'A', '[', '__audit_absent_annotation__') if feature == 'provides' else (None, '', 'a', 'A', '[', '__audit_absent_annotation__')
            for query in queries:
                subject = canonical_json({'query': query})
                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                              (stable_id({'feature': feature, 'subject': subject}), feature, subject))
