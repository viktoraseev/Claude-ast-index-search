"""Java project profiles with independent source expectations, never MCP truth.

The index supplies file scope only. javac supplies Java declarations/imports;
foreign aggregate contributions are internal normalization, not parser coverage.
Stack markers use the full private inventory. Composition flags and scanner
resource limits have a separate source/state fixture in stack_contracts.
"""
from collections import Counter, defaultdict
from pathlib import Path
import re
import subprocess

from common import ToolError, canonical_json, connect, stable_id
from java_structure import structure_server
import mobile_contracts

FEATURES = {'conventions', 'detect-stacks'}
REASON = ('independent source/state: javac Java imports/declarations and full marker inventory; '
          'native indexed file scope and internal foreign aggregate normalization; not MCP equivalence')
SUFFIXES = '''ViewModel Repository UseCase Service Controller Interactor Presenter Factory Mapper
Provider Manager Handler Adapter Delegate Store Reducer Component Fragment Activity Screen View
Widget Bloc Cubit Test Spec Module Router Navigator Middleware Interceptor Gateway'''.split()
ARCHITECTURES = [
    (['presentation', 'domain', 'data'], 'Clean Architecture'), (['feature'], 'Feature-sliced'),
    (['features'], 'Feature-sliced'), (['bloc', 'state', 'event'], 'BLoC'),
    (['views', 'controllers'], 'MVC'), (['viewmodel', 'view', 'model'], 'MVVM'),
    (['presenter'], 'MVP'), (['reducers', 'actions', 'store'], 'Redux'),
    (['composables'], 'Composition API'), (['hooks'], 'Hooks pattern'),
]
# Public convention vocabulary. Each import contributes to its first matching
# framework; a suffix alone (Test/ObjectMapper) cannot identify a package.
RULES = [
    ('dagger', 'DI', 'Dagger'), ('hilt', 'DI', 'Hilt'), ('koin', 'DI', 'Koin'),
    ('kodein', 'DI', 'Kodein'), ('javax.inject', 'DI', 'javax.inject'),
    ('com.google.inject', 'DI', 'Guice'), ('org.springframework.beans', 'DI', 'Spring'),
    ('org.springframework.context', 'DI', 'Spring'),
    ('kotlinx.coroutines', 'Async', 'Coroutines'), ('io.reactivex', 'Async', 'RxJava'),
    ('rx.', 'Async', 'Rx'), ('combine', 'Async', 'Combine'),
    ('kotlinx.coroutines.flow', 'Async', 'Flow'),
    ('retrofit', 'Network', 'Retrofit'), ('okhttp', 'Network', 'OkHttp'),
    ('alamofire', 'Network', 'Alamofire'), ('ktor', 'Network', 'Ktor'),
    ('androidx.room', 'DB', 'Room'), ('io.realm', 'DB', 'Realm'),
    ('app.cash.sqldelight', 'DB', 'SQLDelight'), ('coredata', 'DB', 'CoreData'),
    ('active_record', 'DB', 'ActiveRecord'), ('sequel', 'DB', 'Sequel'),
    ('androidx.compose', 'UI', 'Jetpack Compose'), ('swiftui', 'UI', 'SwiftUI'),
    ('react', 'UI', 'React'), ('vue', 'UI', 'Vue'), ('svelte', 'UI', 'Svelte'),
    ('flutter', 'UI', 'Flutter'), ('org.junit', 'Testing', 'JUnit'),
    ('io.kotest', 'Testing', 'Kotest'), ('xctest', 'Testing', 'XCTest'),
    ('pytest', 'Testing', 'pytest'), ('jest', 'Testing', 'Jest'),
    ('rspec', 'Testing', 'RSpec'), ('=testing', 'Testing', 'testing'),
    ('org.mockito', 'Testing', 'Mockito'), ('io.mockk', 'Testing', 'MockK'),
    ('kotlinx.serialization', 'Serialization', 'kotlinx.serialization'),
    ('com.google.gson', 'Serialization', 'Gson'), ('com.squareup.moshi', 'Serialization', 'Moshi'),
    ('com.fasterxml.jackson', 'Serialization', 'Jackson'), ('rails', 'Web', 'Rails'),
    ('django', 'Web', 'Django'), ('flask', 'Web', 'Flask'), ('fastapi', 'Web', 'FastAPI'),
    ('express', 'Web', 'Express'), ('sidekiq', 'Jobs', 'Sidekiq'), ('celery', 'Jobs', 'Celery'),
]
JVM_MARKERS = ['settings.gradle.kts', 'settings.gradle', 'build.gradle.kts',
               'build.gradle', 'libs.versions.toml', 'pom.xml']
EXCLUDED_DIRS = set('''node_modules __pycache__ build dist target Pods DerivedData venv coverage
out bazel-out bazel-bin bazel-genfiles bazel-testlogs buck-out _build tmp temp _site'''.split())
FIXTURE_DIRS = {'test', 'tests', '__tests__', 'fixtures', '__fixtures__', 'test-fixtures', 'testdata'}


class Unresolved(ToolError):
    pass


def framework(name):
    for prefix, category, label in RULES:
        if prefix.startswith('='):
            if name.lower() == prefix[1:]:
                return category, label
            continue
        for match in re.finditer(re.escape(prefix), name, re.IGNORECASE | re.ASCII):
            a, b = match.span()
            if (a == 0 or name[a - 1] in './:@') and (
                prefix.endswith('.') or b == len(name) or not name[b].isalpha()
                or (name[a].isupper() and name[b].isupper())
            ):
                return category, label
    return None


def require_inventory(state):
    if not state.execute("SELECT 1 FROM metadata WHERE key='inventory_sha256'").fetchone():
        raise Unresolved('full file-type inventory is required for Java project profiles')
    if state.execute("SELECT 1 FROM file_inventory WHERE kind='link-directory' LIMIT 1").fetchone():
        raise Unresolved('directory links prevent complete marker scope evidence')


def jvm_markers(state, root):
    require_inventory(state)
    markers = defaultdict(list)
    git_root_checked = False
    for row in state.execute('SELECT * FROM file_inventory ORDER BY path'):
        path = Path(row['path'])
        if path.name not in JVM_MARKERS:
            continue
        if row['kind'] != 'file':
            raise Unresolved('linked JVM build marker has unresolved scope')
        # Root markers are inspected directly, regardless of ignore rules.
        if len(path.parts) > 1:
            if len(path.parts) > 8 or any(p.startswith('.') or p in EXCLUDED_DIRS | FIXTURE_DIRS
                                         for p in path.parts[:-1]):
                continue
            if (root / '.git').exists():
                if not git_root_checked:
                    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                                            capture_output=True, text=True, timeout=10)
                    if result.returncode or Path(result.stdout.strip()).resolve() != root.resolve():
                        raise Unresolved('marker Git scope differs from exact target root')
                    git_root_checked = True
                result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--', path.as_posix()],
                                        cwd=root, capture_output=True, timeout=10)
                if result.returncode == 0:
                    continue
                if result.returncode != 1:
                    raise Unresolved('marker ignore scope cannot be established')
            if any(Path(r[0]).name in {'.ignore', '.arcignore'} and r[1]
                   for r in state.execute('SELECT path,size FROM file_inventory')):
                raise Unresolved('custom marker ignore scope remains unresolved')
            if not (root / '.git').exists() and any(Path(r[0]).name == '.gitignore' and r[1]
                    for r in state.execute('SELECT path,size FROM file_inventory')):
                raise Unresolved('marker ancestor Git ignore scope remains unresolved')
        stat = (root / path).stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['size'], row['modified']):
            raise ToolError('JVM marker changed after inventory')
        selected = markers[path.name]
        selected.append(path.as_posix())
        selected.sort(key=lambda p: ('/' in p, p))
        del selected[32:]
    # Root markers precede recursively discovered markers within each kind.
    ordered = [p for name in JVM_MARKERS for p in sorted(markers[name], key=lambda p: ('/' in p, p))]
    return ordered[:32]


def conventions(fixture):
    require_inventory(fixture.state)
    source = connect(fixture.database, read_only=True)
    naming, hits, segments = Counter(), Counter(), set()
    relevant_segments = {marker for markers, _ in ARCHITECTURES for marker in markers}
    java_count = 0
    try:
        for row in source.execute('SELECT path,root_path FROM files ORDER BY root_path,path'):
            path = row['path']
            segments.update(relevant_segments.intersection(Path(path.lower()).parts))
            if not path.endswith('.java'):
                continue
            if row['root_path'] and Path(row['root_path']).resolve() != fixture.root.resolve():
                raise Unresolved('extra-root Java profile scope requires a separate source inventory')
            inventory = fixture.state.execute('SELECT * FROM file_inventory WHERE path=?', (path,)).fetchone()
            if inventory is None or inventory['kind'] != 'file' or inventory['size'] > 4 * 1024 * 1024:
                raise Unresolved('Java profile source has linked, missing or oversized inventory scope')
            stat = (fixture.root / path).stat()
            if (stat.st_size, stat.st_mtime_ns) != (inventory['size'], inventory['modified']):
                raise ToolError('Java profile source changed after inventory')
            document = structure_server(fixture.database.parent).read(fixture.root / path, document=True)
            for entry in document['entries']:
                if entry['kind'] in {'class', 'interface', 'enum'}:
                    naming.update(s for s in SUFFIXES if entry['name'].endswith(s))
            for name in document['imports']:
                if hit := framework(name):
                    hits[hit] += 1
            java_count += 1
            if java_count > 100000:
                raise Unresolved('Java profile exceeds bounded source count')
        # Mixed outputs cannot attribute aggregate counts to a language. Keep
        # the foreign contribution as an internal baseline, with Java counts
        # obtained independently; no foreign parser assertion is made.
        for row in source.execute("""SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id
                WHERE substr(f.path,-5)!='.java' AND s.kind IN
                ('class','interface','struct','enum','object','protocol','trait','actor')"""):
            naming.update(s for s in SUFFIXES if row[0].lower().endswith(s.lower()))
        for row in source.execute("""SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id
                WHERE substr(f.path,-5)!='.java' AND s.kind='import' UNION ALL
                SELECT r.name FROM refs r JOIN files f ON f.id=r.file_id
                WHERE substr(f.path,-5)!='.java' AND r.context LIKE 'import%'"""):
            if hit := framework(row[0]):
                hits[hit] += 1
    finally:
        source.close()
    architecture = []
    for markers, label in ARCHITECTURES:
        if set(markers) <= segments and label not in architecture:
            architecture.append(label)
    frameworks = defaultdict(list)
    for (category, label), count in sorted(hits.items(), key=lambda r: (-r[1], r[0][1])):
        frameworks[category].append({'name': label, 'count': count})
    return {'architecture': architecture, 'frameworks': dict(frameworks),
            'naming_patterns': [{'suffix': suffix, 'count': count} for suffix, count in
                                sorted(naming.items(), key=lambda r: (-r[1], r[0])) if count >= 3]}


def plan_profiles(state, root):
    if root is None:
        return
    try:
        require_inventory(state)
        jvm_markers(state, root)
        marker_status, marker_reason = 'implemented', REASON
    except Unresolved as error:
        marker_status, marker_reason = 'pending', str(error)
    with state:
        for feature in sorted(FEATURES):
            status, reason = (marker_status, marker_reason) if feature == 'detect-stacks' else ('implemented', REASON)
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, status, reason))
            subject = 'java-project-profile'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      ('detect-stacks:composition-budgets', 'pending',
                       'Java marker contract excludes composition flags and scanner budget exhaustion; separate fixture contract required'))


def conventions_text(output):
    lines = output.splitlines()
    if not lines or lines[0] != 'Project Conventions:':
        raise Unresolved('unrecognized conventions text header')
    result = {'architecture': [], 'frameworks': {}, 'naming_patterns': []}
    naming = False
    for line in lines[1:]:
        if not line.strip():
            continue
        if line.startswith('Architecture: '):
            result['architecture'] = line[len('Architecture: '):].split(', ')
        elif line == 'Naming Patterns:':
            naming = True
        elif naming:
            match = re.fullmatch(r'  (\S+)\s+(\d+)', line)
            if not match:
                raise Unresolved('unrecognized conventions naming row')
            result['naming_patterns'].append({'suffix': match[1], 'count': int(match[2])})
        else:
            match = re.fullmatch(r'([^:]+): (.+)', line)
            if not match:
                raise Unresolved('unrecognized conventions framework category')
            hits = []
            for item in match[2].split(', '):
                hit = re.fullmatch(r'(.+) \((\d+)\)', item)
                if not hit:
                    raise Unresolved('unrecognized conventions framework row')
                hits.append({'name': hit[1], 'count': int(hit[2])})
            result['frameworks'][match[1]] = hits
    return result


def stack_text(output):
    lines = output.splitlines()
    if lines and lines[0] == 'No known project stacks detected at this root.':
        return []
    if not lines or not lines[0].startswith('Detected setup: '):
        raise Unresolved('unrecognized stack text header')
    stacks = []
    for line in lines[1:]:
        if not line.strip():
            continue
        if match := re.fullmatch(r'  (.+) \(([^()]+)\)', line):
            stacks.append({'kind': match[2], 'label': match[1], 'markers': []})
        elif line.startswith('    - ') and stacks:
            stacks[-1]['markers'].append(line[len('    - '):])
        elif line.startswith('This looks like') or line.startswith('Hint:'):
            continue
        else:
            raise Unresolved('unrecognized stack marker text row')
    return [s for s in stacks if s['kind'] == 'android']


def verify(fixture, feature):
    from audit import Unsupported
    try:
        if feature == 'conventions':
            expected = conventions(fixture)
            actual = {'json': fixture.cli(feature), 'text': conventions_text(fixture.text_cli(feature))}
        else:
            markers = jvm_markers(fixture.state, fixture.root)
            result = fixture.cli(feature)
            if result.get('scan_truncated') is not False:
                raise Unresolved('stack scanner exhausted its budget; complete JVM marker scope remains pending')
            expected = [{'kind': 'android', 'label': 'Android (Kotlin/Java/JVM)', 'markers': markers}] if markers else []
            actual = {'json': [s for s in result.get('stacks', []) if s.get('kind') == 'android'],
                      'text': stack_text(fixture.text_cli(feature))}
        return {'source': REASON, 'samples': expected}, actual, \
            {(mode, canonical_json(expected)) for mode in ('json', 'text')}, \
            {(mode, canonical_json(result)) for mode, result in actual.items()}
    except Unresolved as error:
        raise Unsupported(str(error)) from error
