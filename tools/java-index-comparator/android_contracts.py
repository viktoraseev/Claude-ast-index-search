"""Java Android ownership contracts on disposable source, never MCP truth.

Target absence requires the full private inventory, including ignored and
foreign files. Applicable targets retain a separate pending source contract;
the disposable fixture cannot establish equivalence for their resource syntax.
"""
import hashlib
from pathlib import Path
import re
import tempfile

from common import ToolError, canonical_json, stable_id
from root_contracts import Runner
import mobile_contracts


FEATURES = {'xml-usages', 'resource-usages'}
REASON = ('independent source/state: disposable Java Android resource ownership, '
          'module filters, unused definitions and rendered limits; not MCP equivalence')
SCHEMA = '''CREATE TABLE IF NOT EXISTS android_applicability(
    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, marker INTEGER NOT NULL
);'''
RES_DIRS = {'values', 'layout', 'drawable', 'menu', 'navigation', 'mipmap',
            'anim', 'animator', 'color', 'font', 'interpolator', 'raw', 'transition', 'xml'}


def source_traits(path, extension):
    parts = path.parts
    resource = any(parts[i] == 'res' and parts[i + 1].split('-')[0] in RES_DIRS
                   for i in range(len(parts) - 2))
    relevant = resource or path.name == 'AndroidManifest.xml' or extension in {
        '.java', '.xml', '.gradle', '.kts', '.kt', '.properties', '.toml'}
    return resource, relevant


def verify_absence_evidence(state):
    """Read-only proof check; current file contents are bound by inventory SHA."""
    metadata = dict(state.execute('SELECT key,value FROM metadata'))
    inventory_digest = hashlib.sha256()
    count = size = 0
    for row in state.execute('SELECT * FROM file_inventory ORDER BY rowid'):
        inventory_digest.update((canonical_json(tuple(row)) + '\n').encode())
        path = Path(row['path'])
        resource, relevant = source_traits(path, row['extension'])
        if row['kind'] == 'link-directory':
            raise ToolError('Android absence evidence contains an unfollowed directory link')
        if not relevant:
            continue
        count += 1
        size += row['size']
        if resource or path.name == 'AndroidManifest.xml' or row['kind'] != 'file' or \
                count > 100000 or size > 512 * 1024 * 1024 or row['size'] > 4 * 1024 * 1024:
            raise ToolError('Android absence evidence is inapplicable or exceeds its scan budget')
        proof = state.execute('SELECT sha256,marker FROM android_applicability WHERE path=?',
                              (row['path'],)).fetchone()
        if proof is None or proof['marker'] != 0 or not row['sha256'] or proof['sha256'] != row['sha256']:
            raise ToolError('Android absence evidence is missing or contradicts the inventory')
    if inventory_digest.hexdigest() != metadata.get('inventory_sha256'):
        raise ToolError('Android absence evidence has an incomplete recorded inventory')
    if state.execute('SELECT count(*) FROM android_applicability').fetchone()[0] != count:
        raise ToolError('Android absence evidence has an unexpected source population')
    digest = hashlib.sha256()
    for row in state.execute('SELECT * FROM android_applicability ORDER BY path'):
        digest.update(canonical_json((row['path'], row['sha256'], bool(row['marker']))).encode())
    if digest.hexdigest() != metadata.get('android_applicability_sha256'):
        raise ToolError('Android absence evidence fingerprint is missing or changed')


def applicability(state, root):
    with state:
        state.execute('DELETE FROM android_applicability')
        state.execute("DELETE FROM metadata WHERE key='android_applicability_sha256'")
    if not state.execute("SELECT 1 FROM metadata WHERE key='inventory_sha256'").fetchone():
        return 'pending', 'full file-type inventory is required'
    if state.execute("SELECT 1 FROM file_inventory WHERE kind='link-directory' LIMIT 1").fetchone():
        return 'pending', 'unfollowed directory links prevent Android absence evidence'
    digest = hashlib.sha256()
    count = size = markers = 0
    with state:
        for row in state.execute('SELECT * FROM file_inventory ORDER BY path'):
            path = Path(row['path'])
            resource, relevant = source_traits(path, row['extension'])
            if not relevant:
                continue
            if row['kind'] != 'file':
                return 'pending', 'unfollowed relevant file link prevents Android absence evidence'
            count += 1
            size += row['size']
            if count > 100000 or size > 512 * 1024 * 1024 or row['size'] > 4 * 1024 * 1024:
                return 'pending', 'Android applicability source budget exhausted'
            source = root / path
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (row['size'], row['modified']):
                raise ToolError('Android applicability source changed after inventory')
            # Foreign source is inspected only for framework presence, never
            # compared with a non-Java parser or treated as passing coverage.
            with source.open('rb') as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ToolError('Android applicability source grew beyond its budget')
            content = '' if resource and row['extension'] not in {'.xml', '.java'} else data.decode(errors='replace')
            marker = resource or path.name == 'AndroidManifest.xml' or bool(re.search(
                r'com\.android\.|android\.R\b|\bandroid\.[A-Za-z]|schemas\.android\.com/apk/res|'
                r'\bR(?:\s|/\*[\s\S]*?\*/|//[^\n]*)*\.|'
                r'\bimport\s+(?:static\s+)?[\w$.]+\.R(?:\.[\w$]+)*(?:\.\*)?\s*;', content))
            fingerprint = hashlib.sha256(data).hexdigest()
            if row['sha256'] and row['sha256'] != fingerprint:
                raise ToolError('Android applicability fingerprint changed after inventory')
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (row['size'], row['modified']):
                raise ToolError('Android applicability source changed while reading')
            markers += bool(marker)
            state.execute('INSERT INTO android_applicability VALUES (?,?,?)',
                          (path.as_posix(), fingerprint, int(marker)))
            digest.update(canonical_json((path.as_posix(), fingerprint, bool(marker))).encode())
        state.execute("INSERT OR REPLACE INTO metadata VALUES ('android_applicability_sha256',?)",
                      (digest.hexdigest(),))
    if markers:
        return 'pending', 'Android markers are present; target resource syntax/ownership oracle remains unresolved'
    return 'inapplicable', 'independent full inventory and bounded framework scan: no Android resources, manifests or source/build markers; empty CLI checked separately'


def plan_android(state, root):
    if root is None:
        return
    if not state.execute("SELECT 1 FROM metadata WHERE key='inventory_sha256'").fetchone():
        mobile_contracts.inventory(state, root)
    status, reason = applicability(state, root)
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-android'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
            target = feature + ':target'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (target, status, reason))
            if status == 'inapplicable':
                state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                              (stable_id({'feature': feature, 'subject': 'target-absence'}), feature, 'target-absence'))
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                      ('android:syntax-resolution', 'pending',
                       'Java compiler visibility/shadowing, merged dependency R classes, computed/manifest namespaces and attached-root resource resolution, XML namespace/entity resolution and additional definition types remain unresolved; disposable Java lexical/import/literal-namespace and XML syntax fixtures do not establish target Android syntax equivalence'))


def observation(text):
    """Retain rendered counts and caps as well as every visible location."""
    return {'locations': sorted((m[1], int(m[2])) for m in re.finditer(r'^  (.+):(\d+)$', text, re.MULTILINE)),
            'views': sorted((m[1], m[2] or '') for m in re.finditer(r'^    <(.+?) \.\.\.(?: \((.*?)\))? />$', text, re.MULTILINE)),
            'xml_groups': sorted(re.findall(r'\n([^\n]+):\n  [^\n]+:\d+\n    <', text)),
            'groups': sorted((m[1], int(m[2])) for m in re.finditer(r'^(Kotlin/Java|XML) \((\d+)\):$', text, re.MULTILINE)),
            'total': int(m[1]) if (m := re.search(r'^Total: (\d+) usages$', text, re.MULTILINE)) else 0,
            'xml_count': int(m[1]) if (m := re.search(r'^XML usages of .+ \((\d+)\):$', text, re.MULTILINE)) else None,
            'unused': sorted(re.findall(r'^  ⚠ @([^\s]+)$', text, re.MULTILINE)),
            'unused_total': int(m[1]) if (m := re.search(r'^Total unused: (\d+) resources$', text, re.MULTILINE)) else None,
            'omitted': sorted(int(n) for n in re.findall(r'^  \.\.\. and (\d+) more$', text, re.MULTILINE))}


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Android fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-android-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()

    def write(path, content):
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    write('Sentinel.java', 'package fixture; class Widget {}\n')
    for module in ('app', 'library'):
        write(module + '/build.gradle', "plugins { id 'com.android.library' }\n")
        write(module + '/src/main/res/values/strings.xml',
              '<resources>\n<string name="shared">Hello</string>\n<string name="unused">Unused</string>\n</resources>\n')
    write('app/src/main/res/values-fr/strings.xml', '<resources>\n<string name="shared">Bonjour</string>\n</resources>\n')
    code_path = 'app/src/main/java/Use.java'
    write(code_path, 'class Use {\n void use() {\n' + '  consume(R.string.shared);\n' * 12 + ' }\n}\n')
    orphan_code = 'app_extra/src/main/java/Other.java'
    write(orphan_code, 'class Other {\n int value = R.string.shared;\n}\n')
    # Independent unowned definition prevents ambiguous foreign fallback.
    write('app_extra/src/main/res/values/strings.xml', '<resources>\n<string name="shared">Other</string>\n</resources>\n')
    layouts = [module + '/src/main/res/layout/screen.xml' for module in ('app', 'library', 'app_extra')]
    for path in layouts:
        write(path, '<fixture.Widget xmlns:android="http://schemas.android.com/apk/res/android" android:id="@+id/widget" />\n')
    source_views = {path: ('fixture.Widget', 'widget') for path in layouts}
    runner.command('rebuild', '--force', '--max-files', 0)
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))

    def sample(feature, key, args, **wanted):
        _, output = runner.command(feature, *args)
        empty = observation('')
        if feature == 'xml-usages':
            wanted['views'] = sorted(source_views[p] for p, _ in wanted.get('locations', []))
            wanted['xml_groups'] = sorted({p.split('/')[0] if p.split('/')[0] in {'app', 'library'}
                                           else '(unassigned)' for p, _ in wanted.get('locations', [])})
        expected[feature][key] = {**empty, **wanted}
        actual[feature][key] = observation(output)

    sample('xml-usages', 'unowned-and-modules', ['Widget'],
           locations=[(p, 1) for p in sorted(layouts)], xml_count=3)
    sample('xml-usages', 'exact-module', ['Widget', '--module', 'app'], locations=[(layouts[0], 1)], xml_count=1)
    sample('xml-usages', 'missing-module', ['Widget', '--module', 'missing'], xml_count=0)
    sample('xml-usages', 'missing-class', ['Absent'], xml_count=0)
    sample('xml-usages', 'other-module', ['Widget', '--module', 'library'],
           locations=[(layouts[1], 1)], xml_count=1)
    calls = [(code_path, n) for n in range(3, 13)]
    for spelling in ('@string/shared', 'R.string.shared', 'shared'):
        sample('resource-usages', 'reference:' + spelling, [spelling],
               locations=calls, groups=[('Kotlin/Java', 13)], total=13, omitted=[3])
    sample('resource-usages', 'exact-module', ['shared', '--module', 'app'],
           locations=calls, groups=[('Kotlin/Java', 12)], total=12, omitted=[2])
    sample('resource-usages', 'unused-library', ['--unused', '--module', 'library'],
           unused=['layout/screen', 'string/shared', 'string/unused'], unused_total=3)
    sample('resource-usages', 'unused-app-configurations', ['--unused', '--module', 'app'],
           unused=['layout/screen', 'string/unused'], unused_total=2)
    sample('resource-usages', 'type-filter', ['shared', '--type', 'color'])
    sample('resource-usages', 'unused-type-filter', ['--unused', '--module', 'app', '--type', 'color'], unused_total=0)
    sample('resource-usages', 'missing', ['absent'])
    # A generated fixture tests the 100-result cap without storing large source.
    write(layouts[0], '<fixture.Widget />\n' * 102)
    source_views[layouts[0]] = ('fixture.Widget', '')
    runner.command('rebuild', '--force', '--max-files', 0)
    all_locations = sorted([(layouts[0], n) for n in range(1, 103)] + [(p, 1) for p in layouts[1:]])
    sample('xml-usages', 'global-cap', ['Widget'], locations=all_locations[:100], xml_count=100)
    sample('xml-usages', 'filtered-not-capped', ['Widget', '--module', 'app'],
           locations=[(layouts[0], n) for n in range(1, 103)], xml_count=102)
    write(code_path, 'class Use {\n void use() {\n' + '  consume(R.string.shared);\n' * 102 + ' }\n}\n')
    runner.command('rebuild', '--force', '--max-files', 0)
    # Keep the original case keys and exact location sample. The display cap
    # must not truncate the authored total (including the unassigned site).
    for key, args, count in (('global-cap', ['shared'], 103),
                             ('module-cap', ['shared', '--module', 'app'], 102)):
        sample('resource-usages', key, args, locations=calls,
               groups=[('Kotlin/Java', count)], total=count, omitted=[count - len(calls)])
    return expected, actual


def verify_absence(fixture, feature):
    status, _ = applicability(fixture.state, fixture.root)
    if status != 'inapplicable':
        raise ToolError('target Android absence is no longer established')
    args = [feature, '__audit_absent_android__']
    from audit import run_command
    output = run_command([str(fixture.binary), *args], fixture.root, fixture.environment)
    message = 'XML usages not indexed.' if feature == 'xml-usages' else 'Resources not indexed.'
    return {'absence': True}, {'absence': output.strip() == message + " Run 'ast-index rebuild' first."}
