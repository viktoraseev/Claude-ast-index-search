"""XML syntax used by Java Android projects; independent source, not MCP truth.

This fixture closes XML token/attribute/location contracts only. Java receiver,
static-import, namespace and dependency resolution remain pending separately.
"""
from pathlib import Path
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner
from android_contracts import observation

FEATURES = {'xml-usages:syntax', 'resource-usages:xml-syntax'}
REASON = ('independent source/state: disposable Java Android qualified class tags, '
          'literal class/android:name/android:id attributes, string/color/dimen/style '
          'definitions and unqualified XML reference locations; not MCP equivalence')
LAYOUT = '''<root xmlns:android="http://schemas.android.com/apk/res/android">
<!-- <fixture.Ghost android:id="@+id/ghost" title="@string/comment_only" /> -->
<![CDATA[<fixture.Ghost title="@string/cdata_only" />]]>
<Fixture.Widget_2 android:id="@+id/first"/><fixture.Other android:id="@+id/second"/>
<view
  class='fixture.Outer$Inner'
  android:id='@+id/inner'
  title='@string/title' />
<fragment
  android:id="@+id/fragment"
  android:name="fixture.Fragment" />
<view fakeclass="fixture.Ghost" notandroid:name="fixture.Ghost"/>
<view note="&lt;fixture.Ghost /&gt;" class="fixture.Café" />
<text title="@string/title" subtitle="@string/second"/>
<text title="@string/title"/>
</root>
'''
VALUES = '''<resources>
<!-- <string name="comment_only">Unused</string> -->
<![CDATA[<string name="cdata_only">Unused</string>]]>
<string translatable='false'
 name='title'>Title</string><string name="second">Second</string>
<string name="comment_only">Unused</string><string name="cdata_only">Unused</string>
<string name="unused">Unused</string>
<color name='accent'>#fff</color>
<dimen name='gap'>8dp</dimen>
<style parent='Base' name='Theme'/>
</resources>
'''


def plan_syntax(state, root):
    if root is None:
        return
    with state:
        for feature in sorted(FEATURES):
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            subject = 'disposable-java-android-xml-syntax-v1'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('Android syntax fixture must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='java-android-xml-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    layout = 'app/src/main/res/layout/screen.xml'
    for path, content in [('app/build.gradle', "plugins { id 'com.android.library' }"),
                          ('app/Example.java', 'package fixture; class Example {}'),
                          (layout, LAYOUT), ('app/src/main/res/values/strings.xml', VALUES)]:
        destination = runner.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    runner.command('rebuild', '--force', '--max-files', 0)
    expected, actual = ({f: {} for f in FEATURES} for _ in range(2))
    for query, rows in [('Widget_2', [(4, 'Fixture.Widget_2', 'first')]),
                        ('Other', [(4, 'fixture.Other', 'second')]),
                        ('Outer$Inner', [(6, 'fixture.Outer$Inner', 'inner')]),
                        ('Fragment', [(11, 'fixture.Fragment', 'fragment')]),
                        ('Café', [(13, 'fixture.Café', '')]), ('Ghost', [])]:
        _, output = runner.command('xml-usages', query, '--module', 'app')
        expected['xml-usages:syntax'][query] = {
            **observation(''), 'locations': sorted((layout, line) for line, _, _ in rows),
            'views': sorted((name, id) for _, name, id in rows),
            'xml_groups': ['app'] if rows else [], 'xml_count': len(rows)}
        actual['xml-usages:syntax'][query] = observation(output)
    for query, lines in [('title', [8, 14, 15]), ('second', [14]),
                         ('comment_only', []), ('cdata_only', [])]:
        _, output = runner.command('resource-usages', '@string/' + query, '--module', 'app')
        expected['resource-usages:xml-syntax'][query] = {
            **observation(''), 'locations': [(layout, line) for line in lines],
            'groups': [('XML', len(lines))] if lines else [], 'total': len(lines)}
        actual['resource-usages:xml-syntax'][query] = observation(output)
    _, output = runner.command('resource-usages', '--unused', '--module', 'app')
    expected['resource-usages:xml-syntax']['unused'] = {
        **observation(''), 'unused': sorted(['string/comment_only', 'string/cdata_only',
            'string/unused', 'color/accent', 'dimen/gap', 'style/Theme', 'layout/screen']),
        'unused_total': 7}
    actual['resource-usages:xml-syntax']['unused'] = observation(output)
    return expected, actual
