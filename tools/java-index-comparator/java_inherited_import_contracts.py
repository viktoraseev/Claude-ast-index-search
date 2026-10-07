"""Inherited Java imports retain declaring modules; independent of MCP."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURES = {'unused-deps:java-inherited-imports'}
REASON = ('independent source/state and javac: inherited Java member type/static imports, '
          'canonical import guards, hiding/diamonds, accessibility and attached classpath '
          'declaring ownership, occurrence-scoped protected subclass types/static members and '
          'enclosing-subclass/sibling/import guards, inherited lexical instance fields/methods '
          'and interface defaults, occurrence-owned local subclass inheritance and lexical superclass '
          'binding/hiding with static-context boundaries, nominal value-qualified receiver declaration sites, '
          'inherited member ownership and protected qualifier guards in default/strict JSON/text; not MCP equivalence')


def plan_imports(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-inherited-import-ownership'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,'inherited import ownership fixture')=0",
                          ('; separate inherited import ownership fixture executes public nested/static '
                           'member aliases, canonical/access/hiding/diamond guards and attached '
                           'classpath provenance; protected subclass lexical access, external/platform '
                           'lookup, overload/receiver dispatch and classpath-order ambiguity remain '
                           'pending; not MCP equivalence', parent))

        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix'):
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,'occurrence-scoped protected subclass fixture')=0",
                          ('; separate executed occurrence-scoped protected subclass fixture covers '
                           'lexical/canonical/alias member types, inherited static fields/methods, '
                           'enclosing subclass access including local-class captures, sibling isolation, hiding and import guards '
                           'with attached declaring ownership and JSON/text/refresh controls; '
                           'value receiver dispatch, instance members, local subclass superclass hiding, external/platform lookup '
                           'and classpath-order ambiguity remain pending; not MCP equivalence', parent))
        for parent in ('unused-deps:semantic-resolution', 'global:scope-command-matrix'):
            note = ('; separate executed lexical instance ownership fixture covers inherited public/protected/package '
                    'fields and methods, interface defaults/diamonds, enclosing captures and static-context guards; '
                    'value-qualified receivers, local subclass superclass hiding, overload signature lookup, '
                    'external/platform and classpath-order resolution remain pending; not MCP equivalence')
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (note, parent, note))
            note = ('; separate executed nominal value member ownership fixture covers parameters/locals/fields, '
                    'new/cast/var, captures/references, declaration-site and same-line binding, local hiding, '
                    'public/interface/hidden declaring owners and private/package/protected qualifier guards '
                    'with attached classpath provenance and JSON/text/strict/refresh/option controls; '
                    'field/return chains, generic projections/inference, overload signatures and classpath-order '
                    'ambiguity remain pending; independent source/javac/CLI, not MCP equivalence')
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (note, parent, note))
            note = ('; separate executed local subclass ownership fixture covers occurrence-owned fields/methods, '
                    'protected/static/deep member types, nested/lambda captures, lexical superclass chains, '
                    'same-line/block/method/declaration-point binding and inherited precedence over imports '
                    'and outer owners with attached declaring provenance, JSON/text/refresh/option controls; '
                    'value-qualified receivers, overload signatures, external/platform and classpath-order '
                    'resolution remain pending; not MCP equivalence')
            state.execute("UPDATE coverage SET reason=reason || ? WHERE feature=? AND status='pending' "
                          "AND instr(reason,?)=0", (note, parent, note))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('inherited import fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='inherited-imports-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.environment.update(AST_INDEX_ROOT=str(runner.root), AST_INDEX_DB_PATH=str(directory / 'index.sqlite'))
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got

    def write(owner, path, source):
        destination = directory / owner / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source + '\n')
        return destination

    sources = {
        'base/Parent.java': '''package shared; public class Parent {
            public static class Nested { public static class Deep {} }
            public class Instance { public int INST=1; } protected static class Guarded { public static class Deep {} }
            private static class Hidden {} static class PackageOnly {}
            public static int VALUE=1; public static int read(){return 1;}
            protected static int SECRET=1; protected static int secret(){return 1;} public int instance(){return 1;}
            public int OPEN=1; protected int HUSH=1; protected int hush(){return 1;}
            public Object VALUEOBJ=new Object();
            int PACKAGE=1; int packageCall(){return 1;} private int PRIVATE=1; private int privateCall(){return 1;}
        }''',
        'base/Port.java': '''package shared; public interface Port {
            class Token {} int FLAG=1; static int own(){return 1;} default int defaultCall(){return 1;}
        }''',
        'base/Other.java': 'package shared; public interface Other { class Token {} int FLAG=2; }',
        'base/HiddenBase.java': 'package shared; class HiddenBase { public static class Exported {} public static int EXPORTED=1; }',
        'lib/Child.java': 'package shared; public class Child extends Parent {}',
        'lib/VALUEOBJ.java': 'package shared; public class VALUEOBJ {}',
        'lib/HiddenChild.java': 'package shared; public class HiddenChild extends HiddenBase {}',
        'lib/Left.java': 'package shared; public interface Left extends Port {}',
        'lib/Right.java': 'package shared; public interface Right extends Port {}',
        'lib/Diamond.java': 'package shared; public class Diamond implements Left, Right {}',
        'lib/Ambiguous.java': 'package shared; public class Ambiguous implements Port, Other {}',
        'lib/Hider.java': 'package shared; public class Hider extends Parent { private static class Nested {} public static int VALUE=2; }',
        'lib/PrivateToken.java': 'package shared; public class PrivateToken { private static class Token {} }',
        'lib/AccessibleToken.java': 'package shared; public class AccessibleToken extends PrivateToken implements Port {}',
        'lib/PrivateField.java': 'package shared; public class PrivateField extends Parent { private static int VALUE=2; }',
        'base/CrossParent.java': 'package shared; public class CrossParent { static class CrossType {} static int CROSS=1; int PACKAGE=1; int packageCall(){return 1;} }',
        'lib/Bridge.java': 'package other; public class Bridge extends shared.CrossParent {}',
        'lib/Return.java': 'package shared; public class Return extends other.Bridge {}',
        'lib/LocalBase.java': '''package shared; public class LocalBase {
            public static class Nested { public static class Deep {} }
            protected static class Guarded {}
            public static int VALUE=2; public static int read(){return 2;}
            protected static int SECRET=2; protected static int secret(){return 2;}
            public int OPEN=2; protected int HUSH=2; protected int hush(){return 2;}
        }''',
    }
    cases = [
        ('value-parameter', 'class Use { int run(shared.Child x){return x.OPEN+x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-local', 'class Use { int run(){shared.Child x=null; return x.OPEN+x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-field', 'class Use { shared.Child x; int run(){return this.x.OPEN+this.x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-new-cast', 'class Use { int run(Object x){return new shared.Child().OPEN+((shared.Child)x).instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-inferred', 'class Use { int run(){var x=new shared.Child(); return x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-capture', 'class Use { void run(shared.Child x){class Local {int read(){return x.OPEN+x.instance();}}} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-lambda', 'class Use { java.util.function.IntSupplier run(shared.Child x){return () -> x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-reference', 'class Use { java.util.function.IntSupplier run(shared.Child x){return x::instance;} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-local-type', 'class Use { int run(){class Local extends shared.Child {} Local x=null; return x.OPEN+x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-later-shadow', 'import shared.Child; class Use { int run(Child x){class Child extends shared.LocalBase {} return x.instance();} }', True, {'base': ['Parent'], 'lib': ['Child', 'LocalBase']}),
        ('value-sibling-sites', 'class Use { int a(shared.Child x){return x.instance();} int b(shared.LocalBase x){return x.OPEN;} }', True, {'base': ['Parent'], 'lib': ['Child', 'LocalBase']}),
        ('value-hidden-owner', 'class Use { int run(shared.HiddenChild x){return x.EXPORTED;} }', True, {'base': ['HiddenBase'], 'lib': ['HiddenChild']}),
        ('value-default-diamond', 'class Use { int run(shared.Diamond x){return x.defaultCall();} }', True, {'base': ['Port'], 'lib': ['Diamond']}),
        ('value-private-guard', 'class Use { int run(shared.Child x){return x.PRIVATE+x.privateCall();} }', False, {'lib': ['Child']}),
        ('value-protected-guard', 'class Use extends shared.Child { int run(shared.Child x){return x.HUSH+x.hush();} }', False, {'lib': ['Child']}),
        ('value-protected-subclass', 'class Use extends shared.Child { int run(Use x){return x.HUSH+x.hush();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('value-protected-static', 'class Use extends shared.Child { int run(shared.Child x){return x.SECRET+x.secret();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('same-package-value', 'class Use { int run(Child x){return x.PACKAGE+x.packageCall();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('same-package-value-crossing', 'class Use { int run(Return x){return x.PACKAGE+x.packageCall();} }', False, {'lib': ['Return']}),
        ('value-package-guard', 'class Use { int run(shared.Child x){return x.PACKAGE+x.packageCall();} }', False, {'lib': ['Child']}),
        ('value-missing-member', 'class Use { int run(shared.Child x){return x.missing();} }', False, {'lib': ['Child']}),
        ('value-own-hiding', 'class Use { int run(){class Local extends shared.Child {public int OPEN=2; public int instance(){return 2;}} Local x=null; return x.OPEN+x.instance();} }', True, {'lib': ['Child']}),
        ('local-public-instance', 'class Use { void use(){ class Local extends shared.Child { int run(){return OPEN+instance();} } } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('local-protected-instance', 'class Use { void use(){ class Local extends shared.Child { int run(){return HUSH+hush();} } } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('local-static-parent-members', 'class Use { void use(){ class Local extends shared.Child { int run(){return SECRET+secret();} } } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('local-protected-type', 'class Use { void use(){ class Local extends shared.Child { Guarded field; } } }', True, {'base': ['Guarded'], 'lib': ['Child']}),
        ('local-protected-deep-type', 'class Use { void use(){ class Local extends shared.Child { Guarded.Deep field; } } }', True, {'base': ['Deep'], 'lib': ['Child']}),
        ('local-static-enclosing', 'class Use { static void use(){ class Local extends shared.Child { int run(){return HUSH+hush();} } } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('local-nested-capture', 'class Use { void use(){ class Local extends shared.Child { class Inner { int run(){return HUSH+hush();} Guarded field; } } } }', True, {'base': ['Guarded', 'Parent'], 'lib': ['Child']}),
        ('local-lambda-capture', 'class Use { void use(){ class Local extends shared.Child { java.util.function.IntSupplier run(){return () -> HUSH+hush();} } } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('local-import-type-hiding', 'import shared.Parent.*; class Use { void use(){ class Local extends shared.LocalBase { Nested.Deep field; } } }', True, {'lib': ['Deep', 'LocalBase']}),
        ('local-import-static-hiding', 'import static shared.Parent.*; class Use { void use(){ class Local extends shared.LocalBase { int run(){return VALUE+read();} } } }', True, {'lib': ['LocalBase']}),
        ('local-outer-type-hiding', 'class Use extends shared.Child { void use(){ class Local extends shared.LocalBase { Nested.Deep field; } } }', True, {'lib': ['Child', 'Deep', 'LocalBase']}),
        ('local-outer-instance-hiding', 'class Use extends shared.Child { void use(){ class Local extends shared.LocalBase { int run(){return HUSH+hush();} } } }', True, {'lib': ['Child', 'LocalBase']}),
        ('local-same-name-siblings', 'class Use { void use(){ { class Local extends shared.Child { int run(){return HUSH+hush();} } } { class Local extends shared.LocalBase { int run(){return HUSH+hush();} } } } }', True, {'base': ['Parent'], 'lib': ['Child', 'LocalBase']}),
        ('local-static-context-guard', 'class Use { void use(){ class Local extends shared.Child { static int run(){return OPEN+instance();} } } }', False, {'lib': ['Child']}),
        ('local-sibling-guard', 'class Use { void use(){ class Local extends shared.Child {} } int sibling(){return HUSH+hush();} }', False, {'lib': ['Child']}),
        ('local-private-guard', 'class Use { void use(){ class Local extends shared.Child { int run(){return PRIVATE+privateCall();} } } }', False, {'lib': ['Child']}),
        ('local-own-member-hiding', 'class Use { void use(){ class Local extends shared.Child { int HUSH=2; protected int hush(){return 2;} int run(){return HUSH+hush();} } } }', True, {'lib': ['Child']}),
        ('local-superclass-chain', 'class Use { void use(){ class Base extends shared.Child {} class Local extends Base { int run(){return HUSH+hush();} Guarded.Deep field; } } }', True, {'base': ['Deep', 'Parent'], 'lib': ['Child']}),
        ('local-superclass-import-hiding', 'import shared.*; class Use { void use(){ class Child extends LocalBase {} class Local extends Child { int run(){return HUSH+hush();} Nested.Deep field; } } }', True, {'lib': ['Deep', 'LocalBase']}),
        ('local-header-outer-parent', 'class Use extends shared.Child { void use(){ class Local extends Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested'], 'lib': ['Child']}),
        ('local-qualified-parent-alias', 'class Use { void use(){ class Local extends shared.Child.Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested']}),
        ('local-imported-parent-alias', 'import shared.Child; class Use { void use(){ class Local extends Child.Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested'], 'lib': ['Child']}),
        ('local-static-parent-alias', 'import static shared.Child.Nested; class Use { void use(){ class Local extends Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested']}),
        ('local-wildcard-parent-alias', 'import static shared.Child.*; class Use { void use(){ class Local extends Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested']}),
        ('local-lexical-parent-alias', 'class Use { void use(){ class Base extends shared.Child {} class Local extends Base.Nested { Deep field; } } }', True, {'base': ['Deep', 'Nested'], 'lib': ['Child']}),
        ('local-canonical-instance-parent', 'import shared.Parent.Instance; class Use { void use(){ class Local extends Instance { Local(shared.Parent p){p.super();} int run(){return INST;} } } }', True, {'base': ['Instance', 'Parent']}),
        ('local-invalid-static-instance-parent', 'import static shared.Child.Instance; class Use { void use(){ class Local extends Instance { Local(shared.Parent p){p.super();} int run(){return INST;} } } }', False, {'base': ['Parent']}),
        ('local-method-boundaries', 'class Use { void first(){ class Local extends shared.Child { int run(){return HUSH+hush();} } } void second(){ class Local extends shared.LocalBase { int run(){return HUSH+hush();} } } }', True, {'base': ['Parent'], 'lib': ['Child', 'LocalBase']}),
        ('local-superclass-later-declaration', 'import shared.*; class Use { void use(){ class Local extends Child { int run(){return HUSH+hush();} } class Child extends LocalBase {} } }', True, {'base': ['Parent'], 'lib': ['Child', 'LocalBase']}),
        ('local-default-diamond', 'class Use { void use(){ class Local extends shared.Diamond { int run(){return defaultCall();} Token field; } } }', True, {'base': ['Port', 'Token'], 'lib': ['Diamond']}),
        # Private types are not inherited: javac binds the on-demand import.
        ('local-private-type-hiding', 'import shared.Parent.*; class Use { void use(){ class Local extends shared.Hider { Nested field; } } }', True, {'base': ['Nested'], 'lib': ['Hider']}),
        ('local-ambiguous-type-hiding', 'import shared.Port.*; class Use { void use(){ class Local extends shared.Ambiguous { Token field; } } }', False, {'lib': ['Ambiguous']}),
        ('local-self-parent-guard', 'import shared.*; class Use { void use(){ class Child extends Child { int run(){return HUSH+hush();} } } }', False, {}),
        ('instance-public-method', 'class Use extends shared.Child { int use(){return instance();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-public-field', 'class Use extends shared.Child { int use(){return OPEN;} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-value-type-collision', 'import shared.*; class Use extends shared.Child { String use(){return VALUEOBJ.toString();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-protected', 'class Use extends shared.Child { int use(){return HUSH+hush();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-inner', 'class Use extends shared.Child { class Inner { int use(){return HUSH+hush();} } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-local-capture', 'class Use extends shared.Child { void use(){class Local { int use(){return HUSH+hush();} }} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-lambda', 'class Use extends shared.Child { java.util.function.IntSupplier use(){return () -> HUSH+hush();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-anonymous-capture', 'class Use extends shared.Child { java.util.function.IntSupplier use(){return new java.util.function.IntSupplier(){public int getAsInt(){return HUSH+hush();}};} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-initializers', 'class Use extends shared.Child { int value=HUSH+hush(); { value=OPEN+instance(); } Use(){value=HUSH+hush();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-static-method-guard', 'class Use extends shared.Child { static int use(){return OPEN+instance();} }', False, {'lib': ['Child']}),
        ('instance-static-field-guard', 'class Use extends shared.Child { static int value=OPEN+instance(); }', False, {'lib': ['Child']}),
        ('instance-static-block-guard', 'class Use extends shared.Child { static { int value=OPEN+instance(); } }', False, {'lib': ['Child']}),
        ('instance-static-lambda-guard', 'class Use extends shared.Child { static java.util.function.IntSupplier use(){return () -> OPEN+instance();} }', False, {'lib': ['Child']}),
        ('instance-static-nested-guard', 'class Use extends shared.Child { static class Inner { int use(){return OPEN+instance();} } }', False, {'lib': ['Child']}),
        ('instance-static-local-guard', 'class Use extends shared.Child { static void use(){class Local { int use(){return OPEN+instance();} }} }', False, {'lib': ['Child']}),
        ('instance-inner-static-guard', 'class Use extends shared.Child { class Inner { static int use(){return OPEN+instance();} } }', False, {'lib': ['Child']}),
        ('instance-static-subclass', 'class Use { static class Inner extends shared.Child { int use(){return OPEN+instance();} } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('instance-private-guard', 'class Use extends shared.Child { int use(){return PRIVATE+privateCall();} }', False, {'lib': ['Child']}),
        ('instance-package-guard', 'class Use extends shared.Child { int use(){return PACKAGE+packageCall();} }', False, {'lib': ['Child']}),
        ('same-package-instance', 'class Use extends Child { int use(){return PACKAGE+packageCall();} }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('same-package-instance-crossing', 'class Use extends Return { int use(){return PACKAGE+packageCall();} }', False, {'lib': ['Return']}),
        ('instance-import-guard', 'import static shared.Parent.instance; class Use { int use(){return instance();} }', False, {}),
        ('instance-shadow', 'class Use extends shared.Child { int HUSH=2; protected int hush(){return 2;} int use(){return HUSH+hush();} }', True, {'lib': ['Child']}),
        ('instance-default-diamond', 'class Use extends shared.Diamond { int use(){return defaultCall();} }', True, {'base': ['Port'], 'lib': ['Diamond']}),
        ('protected-lexical-type', 'class Use extends shared.Child { Guarded x; }', True, {'base': ['Guarded'], 'lib': ['Child']}),
        ('protected-qualified-type', 'class Use extends shared.Child { shared.Parent.Guarded x; }', True, {'base': ['Guarded'], 'lib': ['Child']}),
        ('protected-alias-type', 'class Use extends shared.Child { shared.Child.Guarded x; }', True, {'base': ['Guarded'], 'lib': ['Child']}),
        ('protected-enclosing-type', 'class Use extends shared.Child { static class Inner { Guarded x; } }', True, {'base': ['Guarded'], 'lib': ['Child']}),
        ('protected-lexical-field', 'class Use extends shared.Child { int x=SECRET; }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-lexical-method', 'class Use extends shared.Child { int x=secret(); }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-enclosing-member', 'class Use extends shared.Child { static class Inner { int x=SECRET+secret(); } }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-sibling-type', 'class Use extends shared.Child {} class Peer { shared.Child.Guarded x; }', False, {'lib': ['Child']}),
        ('protected-sibling-member', 'class Use extends shared.Child {} class Peer { int x=SECRET+secret(); }', False, {'lib': ['Child']}),
        ('protected-invalid-import', 'import static shared.Parent.Guarded; class Use extends shared.Child { Guarded x; }', False, {'lib': ['Child']}),
        ('protected-deep-type', 'class Use extends shared.Child { Guarded.Deep x; }', True, {'base': ['Deep'], 'lib': ['Child']}),
        ('protected-qualified-deep', 'class Use extends shared.Child { shared.Parent.Guarded.Deep x; }', True, {'base': ['Deep'], 'lib': ['Child']}),
        ('protected-local-enclosing', 'class Use extends shared.Child { void use(){ class Local { Guarded x; int y=SECRET+secret(); } } }', True, {'base': ['Guarded', 'Parent'], 'lib': ['Child']}),
        ('protected-type-shadow', 'class Use extends shared.Child { private static class Guarded {} Guarded x; }', True, {'lib': ['Child']}),
        ('protected-value-shadow', 'class Use extends shared.Child { private int SECRET=2; public static int secret(){return 2;} int x=SECRET+secret(); }', True, {'lib': ['Child']}),
        ('protected-import-wildcard', 'import static shared.Child.*; class Use extends shared.Child { Guarded x; int y=SECRET+secret(); }', True, {'base': ['Guarded', 'Parent'], 'lib': ['Child']}),
        ('protected-invalid-field-import', 'import static shared.Parent.SECRET; class Use extends shared.Child { int x=SECRET; }', False, {'lib': ['Child']}),
        ('protected-invalid-method-import', 'import static shared.Parent.secret; class Use extends shared.Child { int x=secret(); }', False, {'lib': ['Child']}),
        ('protected-qualified-field', 'class Use extends shared.Child { int x=shared.Child.SECRET; }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-qualified-method', 'class Use extends shared.Child { int x=shared.Child.secret(); }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-imported-qualifier', 'import shared.Child; class Use extends Child { int x=Child.SECRET+Child.secret(); }', True, {'base': ['Parent'], 'lib': ['Child']}),
        ('protected-sibling-qualified-member', 'class Use extends shared.Child {} class Peer { int x=shared.Child.SECRET+shared.Child.secret(); }', False, {'lib': ['Child']}),
        ('protected-missing-member', 'class Use extends shared.Child { int x=shared.Child.MISSING+shared.Child.missing(); }', False, {'lib': ['Child']}),
        ('member-types', 'import shared.Child.*; class Use { Nested x; Instance y; Nested.Deep z; }', False, {}),
        ('static-types', 'import static shared.Child.*; class Use { Nested x; Nested.Deep y; }', True, {'base': ['Deep', 'Nested']}),
        ('qualified-alias', 'class Use { shared.Child.Nested x; shared.Child.Nested.Deep y; }', True, {'base': ['Deep', 'Nested']}),
        ('qualified-instance-alias', 'class Use { shared.Child.Instance x; }', True, {'base': ['Instance']}),
        ('static-field', 'import static shared.Child.*; class Use { int x=VALUE; }', True, {'base': ['Parent']}),
        ('static-method', 'import static shared.Child.read; class Use { int x=read(); }', True, {'base': ['Parent']}),
        ('static-wildcard-method', 'import static shared.Child.*; class Use { int x=read(); }', True, {'base': ['Parent']}),
        ('hidden-owner-type', 'import static shared.HiddenChild.*; class Use { Exported x; }', True, {'base': ['Exported']}),
        ('hidden-owner-field', 'import static shared.HiddenChild.*; class Use { int x=EXPORTED; }', True, {'base': ['HiddenBase']}),
        ('diamond-type', 'import static shared.Diamond.*; class Use { Token x; }', True, {'base': ['Token']}),
        ('diamond-field', 'import static shared.Diamond.*; class Use { int x=FLAG; }', True, {'base': ['Port']}),
        ('ambiguous-type', 'import shared.Ambiguous.*; class Use { Token x; }', False, {}),
        ('ambiguous-field', 'import static shared.Ambiguous.*; class Use { int x=FLAG; }', False, {}),
        ('private-hiding', 'import shared.Hider.*; class Use { Nested x; }', False, {}),
        ('field-hiding', 'import static shared.Hider.*; class Use { int x=VALUE; }', True, {'lib': ['Hider']}),
        ('private-field-hiding', 'import static shared.PrivateField.*; class Use { int x=VALUE; }', False, {}),
        ('private-not-inherited', 'import static shared.AccessibleToken.*; class Use { Token x; }', True, {'base': ['Token']}),
        ('private-type', 'import shared.Child.*; class Use { Hidden x; }', False, {}),
        ('package-type', 'import shared.Child.*; class Use { PackageOnly x; }', False, {}),
        ('protected-type', 'import shared.Child.*; class Use { Guarded x; }', False, {}),
        ('protected-subclass-import', 'import static shared.Child.Guarded; class Use extends shared.Child { Guarded x; }', False, {'lib': ['Child']}),
        ('protected-field', 'import static shared.Child.*; class Use { int x=SECRET; }', False, {}),
        ('nonstatic-type', 'import static shared.Child.*; class Use { Instance x; }', False, {}),
        ('nonstatic-method', 'import static shared.Child.instance; class Use { int x=instance(); }', False, {}),
        ('interface-static-not-inherited', 'import static shared.Diamond.*; class Use { int x=own(); }', False, {}),
        ('noncanonical-single-type', 'import shared.Child.Nested; class Use { Nested x; }', False, {}),
        ('inherited-static-single-type', 'import static shared.Child.Nested; class Use { Nested x; }', True, {'base': ['Nested']}),
        ('canonical-single-type', 'import shared.Parent.Nested; class Use { Nested x; }', True, {'base': ['Nested']}),
        ('same-package-protected', 'import static shared.Child.*; class Use { Guarded x; }', True, {'base': ['Guarded']}),
        ('same-package-package', 'import static shared.Child.*; class Use { PackageOnly x; }', True, {'base': ['PackageOnly']}),
        ('same-package-qualified', 'class Use { Child.Nested x; }', True, {'base': ['Nested']}),
        ('same-package-crossing-type', 'import static shared.Return.*; class Use { CrossType x; }', False, {}),
        ('same-package-crossing-field', 'import static shared.Return.*; class Use { int x=CROSS; }', False, {}),
    ]
    # The primary names intentionally conflict with attached accessibility and
    # inheritance. Only the selected classpath can provide declaring metadata.
    for owner in ('project', 'attached'):
        for path, source in sources.items():
            if owner == 'project' and path == 'base/Parent.java':
                source = 'package shared; public class Parent {}'
            write(owner, path, source)
        write(owner, 'base/build.gradle', 'plugins {}')
        write(owner, 'lib/build.gradle', 'dependencies { api(project(":base")) }')
        write(owner, 'inventory.txt', 'bounded full inventory sentinel')
    for label, source, _, _ in cases:
        package = 'shared' if label.startswith('same-package-') else 'consumer'
        write('attached', label + '/Use.java', f'package {package}; ' + source)
        write('attached', label + '/build.gradle', 'dependencies { implementation(project(":lib")); implementation(project(":base")) }')
    for owner in ('project', 'attached'):
        state = connect(directory / (owner + '-inventory.sqlite'))
        try:
            state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
            mobile_contracts.inventory(state, directory / owner)
            counts = dict(state.execute('SELECT extension,count(*) FROM file_inventory GROUP BY extension'))
            extra = len(cases) if owner == 'attached' else 0
            want = {'.java': len(sources) + extra, '.gradle': 2 + extra, '.txt': 1}
            if counts != want:
                raise ToolError('inherited import full inventory incomplete')
            record('inventory:' + owner, want, counts)
        finally:
            state.close()
    javac = shutil.which('javac')
    if not javac:
        raise ToolError('inherited import checks require javac')
    classes = directory / 'classes'

    def compile(label, paths, classpath=None):
        args = [javac, '-proc:none', '-d', str(directory / (label + '-classes')),
                '-sourcepath', str(directory / 'empty-sourcepath')]
        if classpath:
            args += ['-cp', str(classpath)]
        with (directory / (label + '.javac.stdout.log')).open('wb') as stdout, \
                (directory / (label + '.javac.stderr.log')).open('wb') as stderr:
            return subprocess.run([*args, *map(str, paths)], cwd=directory,
                                  env={**os.environ, 'CLASSPATH': ''}, stdout=stdout,
                                  stderr=stderr, timeout=30).returncode == 0

    record('javac:library', True, compile('library', [directory / 'attached' / p for p in sources]))
    classes = directory / 'library-classes'
    for label, _, valid, _ in cases:
        record('javac:' + label, valid, compile(label, [directory / 'attached' / label / 'Use.java'], classes))
    (runner.root / '.git').mkdir()
    runner.command('rebuild', '--force', '--max-files', '0')
    runner.json('subtree', 'add', 'attached', '../attached')
    runner.command('rebuild', '--force', '--max-files', '0')
    for label, _, _, used in cases:
        for flags, scope in (([], 'all'), (['--subtree', 'attached'], 'attached')):
            for mode in ([], ['--strict']):
                args = [*flags, 'unused-deps', 'attached::' + label, '--verbose', *mode]
                key = scope + ':' + label + ':' + ('strict' if mode else 'default')
                document = runner.json(*args)
                transitive = not mode and 'base' in used and 'lib' not in used
                want = [('attached::' + owner, 'direct' if owner in used else
                         'transitive' if transitive and owner == 'lib' else 'unused',
                         len(used.get(owner, [])), used.get(owner, [])) for owner in ('base', 'lib')]
                record(key, want, sorted((r['name'], r['category'], r['usage']['direct'], r['examples']['direct'])
                                        for r in document.get('items', [])))
                count = len(used) + int(transitive)
                record(key + ':summary', {'unused': 2-count, 'used': count, 'total': 2,
                       'exported': 0, 'direct': len(used), 'transitive': int(transitive),
                       'xml': 0, 'resources': 0}, document.get('summary'))
                record(key + ':transitive', len(used.get('base', [])) if transitive else 0,
                       next(r['usage']['transitive'] for r in document['items'] if r['name'] == 'attached::lib'))
                _, text = runner.command(*args)
                record(key + ':text', True, f'Total: {2-count} unused, 0 exported, {count} used of 2 dependencies' in text)
    for flags in (['--no-transitive'], ['--no-xml'], ['--no-resources'],
                  ['--no-transitive', '--no-xml', '--no-resources']):
        document = runner.json('unused-deps', 'attached::value-parameter', '--verbose', *flags)
        record('value-option:' + ','.join(flags),
               [('attached::base', 'direct', ['Parent']), ('attached::lib', 'direct', ['Child'])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
        document = runner.json('unused-deps', 'attached::static-field', '--verbose', *flags)
        transitive = '--no-transitive' not in flags
        record('option:' + ','.join(flags), [('attached::base', 'direct', ['Parent']),
               ('attached::lib', 'transitive' if transitive else 'unused', [])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
    for flags in (['--no-transitive'], ['--no-xml'], ['--no-resources'],
                  ['--no-transitive', '--no-xml', '--no-resources']):
        document = runner.json('unused-deps', 'attached::instance-protected', '--verbose', *flags)
        record('instance-option:' + ','.join(flags),
               [('attached::base', 'direct', ['Parent']), ('attached::lib', 'direct', ['Child'])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
        document = runner.json('unused-deps', 'attached::local-outer-instance-hiding', '--verbose', *flags)
        record('local-option:' + ','.join(flags),
               [('attached::base', 'unused', []), ('attached::lib', 'direct', ['Child', 'LocalBase'])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
    document = runner.json('unused-deps', 'attached::local-protected-instance')
    record('local-nonverbose', [('attached::base', 'direct', 1, False),
                               ('attached::lib', 'direct', 1, False)],
           [(r['name'], r['category'], r['usage']['direct'], 'examples' in r) for r in document['items']])
    _, text = runner.command('unused-deps', 'attached::local-protected-instance')
    record('local-nonverbose-text', True, 'Total: 0 unused, 0 exported, 2 used of 2 dependencies' in text)
    document = runner.json('unused-deps', 'attached::value-parameter')
    record('value-nonverbose', [('attached::base', 'direct', 1, False),
                               ('attached::lib', 'direct', 1, False)],
           [(r['name'], r['category'], r['usage']['direct'], 'examples' in r) for r in document['items']])
    _, text = runner.command('unused-deps', 'attached::value-parameter')
    record('value-nonverbose-text', True, 'Total: 0 unused, 0 exported, 2 used of 2 dependencies' in text)
    document = runner.json('unused-deps', 'attached::instance-protected')
    record('instance-nonverbose', [('attached::base', 'direct', 1, False),
                                  ('attached::lib', 'direct', 1, False)],
           [(r['name'], r['category'], r['usage']['direct'], 'examples' in r) for r in document['items']])
    _, text = runner.command('unused-deps', 'attached::instance-protected')
    record('instance-nonverbose-text', True, 'Total: 0 unused, 0 exported, 2 used of 2 dependencies' in text)
    for flags in (['--no-transitive'], ['--no-xml'], ['--no-resources'],
                  ['--no-transitive', '--no-xml', '--no-resources']):
        document = runner.json('unused-deps', 'attached::protected-qualified-field', '--verbose', *flags)
        record('protected-option:' + ','.join(flags),
               [('attached::base', 'direct', ['Parent']), ('attached::lib', 'direct', ['Child'])],
               [(r['name'], r['category'], r['examples']['direct']) for r in document['items']])
    document = runner.json('unused-deps', 'attached::protected-lexical-type')
    record('protected-nonverbose', [('attached::base', 'direct', 1, False),
                                  ('attached::lib', 'direct', 1, False)],
           [(r['name'], r['category'], r['usage']['direct'], 'examples' in r) for r in document['items']])
    _, text = runner.command('unused-deps', 'attached::protected-lexical-type')
    record('protected-nonverbose-text', True, 'Total: 0 unused, 0 exported, 2 used of 2 dependencies' in text)
    record('local-selection', 'missing_module', runner.json('--local', 'unused-deps', 'attached::member-types').get('empty_reason'))
    record('cwd-selection', 'missing_module', runner.json('unused-deps', 'attached::member-types', cwd=runner.root / 'lib').get('empty_reason'))
    for args in (('rebuild', '--type', 'modules'), ('update',)):
        runner.command(*args)
        record('refresh:' + args[0], ['Exported'], runner.json('unused-deps', 'attached::hidden-owner-type', '--verbose')['items'][0]['examples']['direct'])
        for label, want in [('protected-lexical-type', ['Guarded']),
                            ('protected-qualified-field', ['Parent']),
                            ('protected-deep-type', ['Deep']),
                            ('instance-protected', ['Parent']),
                            ('instance-default-diamond', ['Port']),
                            ('instance-static-nested-guard', []),
                            ('local-protected-instance', ['Parent']),
                            ('value-parameter', ['Parent']),
                            ('value-reference', ['Parent']),
                            ('value-protected-guard', []),
                            ('value-protected-subclass', ['Parent']),
                            ('local-import-type-hiding', []),
                            ('local-self-parent-guard', [])]:
            record('protected-refresh:' + args[0] + ':' + label, want,
                   runner.json('unused-deps', 'attached::' + label, '--verbose')['items'][0]['examples']['direct'])
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
