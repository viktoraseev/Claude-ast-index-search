"""Independent Maven/Java module graph contracts; never MCP equivalence.

The inventory is complete, but effective Maven models (profiles, inherited
reactor dependencies and unresolved properties) stay pending. No DB rows are
used as expected module or edge identities.
"""
from collections import Counter, deque
from pathlib import Path
import json
import re
import subprocess
import xml.etree.ElementTree as ET

from common import ToolError, canonical_json, file_sha256, stable_id
import mobile_contracts

FEATURES = {'module', 'deps', 'dependents', 'module-route'}
REASON = 'independent source/state: Maven descriptors and Java ownership; production module graph/navigation; not MCP equivalence'
MAX_BYTES = 4 * 1024 * 1024
MAX_MODULES = 256
MAX_PATHS = 4096
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
BUILD_NAMES = {'build.gradle', 'build.gradle.kts', 'ya.make', 'pom.xml'}
FOREIGN_BUILD_NAMES = {'Package.swift', 'Project.swift', 'pyproject.toml', 'setup.py', 'setup.cfg'}


class Unresolved(ToolError):
    pass


def manifests(state, root):
    """One bounded descriptor at a time, with private inventory fingerprints."""
    if not state.execute("SELECT 1 FROM metadata WHERE key='inventory_sha256'").fetchone():
        raise Unresolved('full file-type inventory is required')
    if state.execute("SELECT 1 FROM file_inventory WHERE kind='link-directory' LIMIT 1").fetchone():
        raise Unresolved('untraversed directory link prevents module absence evidence')
    for row in state.execute('SELECT * FROM file_inventory ORDER BY path'):
        path = Path(row['path'])
        if path.name in FOREIGN_BUILD_NAMES:
            directory = path.parent.as_posix()
            directory = '' if directory == '.' else directory + '/'
            if state.execute("SELECT 1 FROM file_inventory WHERE extension='.java' AND substr(path,1,?)=? LIMIT 1",
                             (len(directory), directory)).fetchone():
                raise Unresolved('non-Maven build descriptor with Java ownership requires a source graph contract')
        if path.name not in BUILD_NAMES:
            continue
        if row['kind'] != 'file':
            raise Unresolved('linked build descriptor has unresolved scope')
        # Never silently classify a present Gradle/ya.make project as absent.
        if path.name != 'pom.xml':
            raise Unresolved('Gradle/ya.make source graph contract remains pending')
        if any(p.startswith('.') or p in {'target', 'build', 'out'} for p in path.parts[:-1]):
            raise Unresolved('hidden/generated Maven descriptor scope remains pending')
        if row['size'] > MAX_BYTES:
            raise Unresolved('Maven descriptor exceeds bounded parser size')
        if state.execute("SELECT 1 FROM file_inventory WHERE (path='.ignore' OR path LIKE '%/.ignore' OR path='.arcignore' OR path LIKE '%/.arcignore') AND size>0 LIMIT 1").fetchone():
            raise Unresolved('custom ignore scope alignment remains pending')
        if (root / '.git').exists():
            repository = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                                        capture_output=True, text=True, timeout=10)
            if repository.returncode or Path(repository.stdout.strip()).resolve() != root.resolve():
                raise Unresolved('build descriptor Git scope differs from the exact target root')
            result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--', path.as_posix()],
                                    cwd=root, capture_output=True, timeout=10)
            if result.returncode == 0:
                continue
            if result.returncode != 1:
                raise Unresolved('Maven descriptor Git ignore scope cannot be established')
        elif state.execute("SELECT 1 FROM file_inventory WHERE (path='.gitignore' OR path LIKE '%/.gitignore') AND size>0 LIMIT 1").fetchone():
            raise Unresolved('ancestor Git discovery scope remains pending')
        source = root / path
        before = source.stat()
        if (before.st_size, before.st_mtime_ns) != (row['size'], row['modified']):
            raise ToolError('Maven descriptor changed after inventory')
        fingerprint = file_sha256(source)
        try:
            tree = ET.parse(source).getroot()
        except ET.ParseError as error:
            raise Unresolved('Maven descriptor XML is unresolved') from error
        if file_sha256(source) != fingerprint:
            raise ToolError('Maven descriptor changed during verification')
        for element in tree.iter():
            element.tag = element.tag.rsplit('}', 1)[-1]
        if tree.tag != 'project':
            raise Unresolved('pom.xml is not a Maven project')
        yield path, tree, fingerprint


def graph(state, root):
    if state.execute("SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0] > 100000:
        raise Unresolved('Java ownership inventory exceeds bounded size')
    if state.execute("SELECT 1 FROM file_inventory WHERE extension='.java' AND kind!='file' LIMIT 1").fetchone():
        raise Unresolved('linked Java source ownership remains pending')
    java = [r[0] for r in state.execute("SELECT path FROM file_inventory WHERE extension='.java' AND kind='file'")]
    modules, coordinates, descriptors, fingerprints = {}, {}, {}, {}
    for path, tree, fingerprint in manifests(state, root):
        # Maven ignores plugin dependencies and dependencyManagement as direct
        # reactor edges. Profile activation requires a separate effective model.
        if tree.findall('./profiles/profile/dependencies/dependency') or tree.findall('./profiles/profile/modules/module'):
            raise Unresolved('profile-dependent Maven graph requires an effective-model contract')
        directory = path.parent.as_posix()
        directory = '' if directory == '.' else directory
        artifact = (tree.findtext('./artifactId') or '').strip()
        group = (tree.findtext('./groupId') or tree.findtext('./parent/groupId') or '').strip()
        properties = {p.tag: (p.text or '').strip() for p in tree.findall('./properties/*')}
        properties.update({'project.groupId': group, 'pom.groupId': group,
                           'project.artifactId': artifact, 'pom.artifactId': artifact})
        def resolve(value):
            value = value.strip()
            for _ in range(16):
                new = re.sub(r'\$\{([^}]+)\}', lambda m: properties.get(m[1], m[0]), value)
                if new == value:
                    break
                value = new
            if '${' in value:
                raise Unresolved('unresolved Maven graph property')
            return value
        group, artifact = resolve(group), resolve(artifact)
        if not artifact or not group:
            raise Unresolved('Maven coordinate is incomplete')
        name = directory.replace('/', '.') or artifact
        coordinate = (group, artifact)
        if coordinate in coordinates or name in descriptors:
            raise Unresolved('ambiguous Maven coordinate or module identity')
        coordinates[coordinate] = name
        managed = {(d.findtext('groupId') or '', d.findtext('artifactId') or '')
                   for d in tree.findall('./dependencyManagement/dependencies/dependency')}
        if any(d.find('scope') is None and (d.findtext('groupId') or '', d.findtext('artifactId') or '') in managed
               for d in tree.findall('./dependencies/dependency')):
            raise Unresolved('managed dependency scopes require an effective-model contract')
        deps = [(resolve(d.findtext('groupId') or ''), resolve(d.findtext('artifactId') or ''),
                 resolve(d.findtext('scope') or 'compile')) for d in tree.findall('./dependencies/dependency')]
        descriptors[name] = (directory, deps, tree.find('parent') is not None)
        fingerprints[path.as_posix()] = fingerprint
        if any(not directory or p.startswith(directory + '/') for p in java):
            modules[name] = directory
        if len(descriptors) > MAX_MODULES:
            raise Unresolved('Maven graph exceeds bounded module count')
    if any(parent for _, _, parent in descriptors.values()):
        raise Unresolved('inherited reactor model requires a separate source contract')
    edges = set()
    for name, (_, dependencies, _) in descriptors.items():
        for group, artifact, kind in dependencies:
            target = coordinates.get((group, artifact))
            if name in modules and target in modules:
                edges.add((name, target, kind))
            elif target and (name in modules or target in modules):
                raise Unresolved('reactor path crosses a module without Java ownership')
    return modules, edges, fingerprints


def plan_modules(state, root):
    if root is None:
        return
    try:
        modules, edges, fingerprints = graph(state, root)
        status, reason = 'implemented', REASON
    except Unresolved as error:
        status, reason = 'pending', str(error)
        modules = {}
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      ('module-route:budgets', 'pending', 'Path-count truncation and wall-clock budgets require a separate disposable Java graph contract'))
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, status, reason))
            subject = 'source-module-graph'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def module_rows(output):
    lines = output.splitlines()
    if not lines or not re.fullmatch(r"Modules matching '.+':", lines[0]):
        raise Unresolved('unrecognized module output')
    result = []
    for line in lines[1:]:
        if line == '  No modules found.':
            continue
        match = re.fullmatch(r'  (.+): (.*)', line)
        if not match:
            raise Unresolved('unrecognized module identity')
        result.append((match[1], match[2]))
    return result


def edge_rows(output, feature):
    if output.strip() in {"Module dependencies not indexed. Run 'ast-index rebuild' first.", "Module dependencies not indexed. Run 'ast-index rebuild' to index them."}:
        return [('not_indexed', '', '')]
    lines = output.splitlines()
    header = 'Dependencies of' if feature == 'deps' else 'Modules depending on'
    if not lines or not (match := re.fullmatch(header + r" '.+' \((\d+)\):", lines[0])):
        raise Unresolved('missing completed module dependency result')
    expected_count = int(match[1])
    rows, kind = [], None
    for line in lines[1:]:
        if line in ('  No dependencies found.', '  No dependents found.'):
            continue
        if line.startswith('  ') and not line.startswith('    '):
            if 'implementation' in line:
                kind = 'implementation'
            elif 'api' in line:
                kind = 'api'
            elif 'other' in line:
                kind = None
            else:
                raise Unresolved('unrecognized dependency section')
            continue
        match = re.fullmatch(r'    (.+) \((.*)\)(?: \[([^]]+)\])?', line)
        if not match or not (match[3] or kind):
            raise Unresolved('unrecognized dependency identity')
        rows.append((match[1], match[2], match[3] or kind))
    if len(rows) != expected_count:
        raise Unresolved('dependency header count differs from rendered rows')
    return rows


def paths(edges, start, end, depth, kind):
    adjacency = {}
    # Match the documented module-route edge deduplication: one kind per hop,
    # lexicographically first when all kinds are allowed.
    for a, b, k in sorted(edges):
        if kind == 'all' or k == kind:
            adjacency.setdefault(a, {}).setdefault(b, k)
    if start == end:
        k = adjacency.get(start, {}).get(end)
        return [((start, end, k),)] if k else []
    result, queue = [], deque([(start, (), frozenset({start}))])
    explored = 0
    while queue:
        node, hops, seen = queue.popleft()
        explored += 1
        if explored > 100000 or len(result) > MAX_PATHS:
            raise Unresolved('source route enumeration exceeds bounded work limit')
        if len(hops) >= depth:
            continue
        for target, k in sorted(adjacency.get(node, {}).items()):
            next_hops = (*hops, (node, target, k))
            if target == end:
                result.append(next_hops)
            elif target not in seen:
                queue.append((target, next_hops, seen | {target}))
    return sorted(result, key=lambda p: (len(p), [h[1] for h in p]))


def verify(fixture, feature):
    from audit import Unsupported
    try:
        modules, edges, fingerprints = graph(fixture.state, fixture.root)
        expected_keys, actual_keys, outputs = Counter(), Counter(), {}
        output_bytes = 0
        def capture(key, output):
            nonlocal output_bytes
            output_bytes += len(canonical_json(output).encode())
            if output_bytes > MAX_OUTPUT_BYTES:
                raise Unresolved('module evidence exceeds bounded per-check payload budget')
            outputs[key] = output
        absent = '__audit_absent_module__'
        while absent in modules:
            absent += '_'
        if feature == 'module':
            java_paths = [r[0] for r in fixture.state.execute("SELECT path FROM file_inventory WHERE extension='.java' AND kind='file'")]
            def java_owned(path):
                if Path(path).is_absolute() or '..' in Path(path).parts:
                    raise Unresolved('module path is outside the source inventory scope')
                return any(not path or p.startswith(path.rstrip('/') + '/') for p in java_paths)
            for query in ['', absent, '%', '_', *sorted(modules), *(n.upper() for n in sorted(modules))]:
                pattern = re.compile(re.escape(query).replace('%', '.*').replace('_', '.'), re.IGNORECASE | re.ASCII)
                matches = sorted((n, p) for n, p in modules.items() if pattern.search(n))
                full_limit = 100000
                full = module_rows(fixture.text_cli('module', query, '--limit', str(full_limit)))
                if len(full) >= full_limit:
                    raise Unresolved('native module collection cap prevents Java normalization')
                key = canonical_json([query, 'java-scope'])
                java_rows = [row for row in full if java_owned(row[1])]
                expected_keys.update((key, i, *r) for i, r in enumerate(matches))
                actual_keys.update((key, i, *r) for i, r in enumerate(java_rows))
                capture(key, java_rows)
                # Native limits apply before Java normalization. Confirm their
                # stable prefix independently, without charging foreign rows
                # against the Java expected result.
                for limit in (0, 1, len(full) + 1):
                    key = canonical_json([query, limit])
                    output = fixture.text_cli('module', query, '--limit', str(limit))
                    capture(key, output)
                    expected_keys.update((key, i, *r) for i, r in enumerate(full[:limit]))
                    actual_keys.update((key, i, *r) for i, r in enumerate(module_rows(output)))
        elif feature in {'deps', 'dependents'}:
            for name in [absent, *sorted(modules)]:
                output = fixture.text_cli(feature, name)
                capture(name, output)
                expected = [(b, modules[b], k) if feature == 'deps' else (a, modules[a], k)
                            for a, b, k in edges if (a if feature == 'deps' else b) == name]
                expected_keys.update((name, *r) for r in expected)
                actual_keys.update((name, *r) for r in edge_rows(output, feature))
        else:
            # Bound pairs to the source graph; oversized enumeration stays pending.
            if len(modules) > 24:
                raise Unresolved('complete route pair audit exceeds bounded module count')
            names = [absent, *sorted(modules)]
            for start in names:
                for end in names:
                    for kind in ('all', 'api', 'implementation'):
                        for depth in sorted({0, 1, len(modules)}):
                            all_paths = paths(edges, start, end, depth, kind)
                            for all_mode in (False, True):
                                args = ['--from', start, '--to', end, '--via-kind', kind,
                                        '--max-depth', str(depth), '--max-paths', str(MAX_PATHS + 1)]
                                if all_mode:
                                    args.append('--all')
                                key = canonical_json([start, end, kind, depth, all_mode])
                                result = fixture.cli('module-route', *args)
                                capture(key, result)
                                selected = all_paths if all_mode else all_paths[:1]
                                reason = ('missing_module_from' if start not in modules else
                                          'missing_module_to' if end not in modules else
                                          'self' if start == end and not selected else
                                          None if selected else
                                          'kind_filter' if kind != 'all' and paths(edges, start, end, depth, 'all') else 'unreachable')
                                expected_keys[(key, 'envelope', canonical_json([start, end, len(selected), False, reason]))] += 1
                                actual_keys[(key, 'envelope', canonical_json([result.get('from'), result.get('to'), result.get('count'), result.get('truncated'), result.get('empty_reason')]))] += 1
                                for i, path in enumerate(selected):
                                    expected_keys[(key, 'path', i, canonical_json([len(path), path]))] += 1
                                for i, path in enumerate(result.get('paths', [])):
                                    hops = [(h.get('from'), h.get('to'), h.get('kind')) for h in path.get('hops', [])]
                                    actual_keys[(key, 'path', i, canonical_json([path.get('length'), hops]))] += 1
                                if len(expected_keys) + len(actual_keys) > 100000:
                                    raise Unresolved('module route evidence exceeds bounded identity budget')
        return {'source': REASON, 'modules': modules, 'edges': sorted(edges), 'descriptors': fingerprints}, outputs, expected_keys, actual_keys
    except Unresolved as error:
        raise Unsupported(str(error)) from error
