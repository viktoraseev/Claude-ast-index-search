"""Finite retained Java obligations. Valid unresolved cases must remain failures.

These exercise source dependency ownership, not compiler equivalence or binary
classpath loading. Each positive input compiles with javac. Invalid capture
equality is a separate negative guard, never a replacement for the positive.
"""
from common import stable_id
import java_common_bound_contracts as runner

FEATURE = 'unused-deps:java-retained-inference'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-retained-dependency-inference-v1'
REASON = ('independent source/javac/CLI: retained target-dependent invocation, '
          'intersection-bound, invariant capture, generic override and raw unchecked '
          'dependency ownership obligations; not MCP equivalence')
CASES = {
    'target-invocation': ('<T> T make(){return null;} shared.Child pick(shared.Child x){return x;}',
                          'b.pick(b.make()).instance()', True),
    'intersection': ('interface Marker {} static class X extends shared.Child implements Marker {} '
                     'static class Y extends shared.Child implements Marker {} '
                     '<T extends shared.Child & Marker> T pick(T a,T c){return a;}',
                     'b.pick(new Box.X(),new Box.Y()).instance()', True),
    'capture-identity': ('<T extends shared.Child> T pick(Carrier<T> value){return null;}',
                         'b.pick((Carrier<? extends shared.Child>)null).instance()', True),
    'override-erasure': ('static class Parent<T> { T get(T value){return value;} } '
                         'static class Derived extends Parent<shared.Child> { '
                         '@Override shared.Child get(shared.Child value){return value;} }',
                         'new Box.Derived().get(c).instance()', True),
    'raw-unchecked': ('shared.Child pick(Carrier<shared.Child> value){return null;}',
                      'b.pick(new Carrier()).instance()', True),
    'capture-equality-guard': ('<T extends shared.Child> T pick(Carrier<T> a,Carrier<T> c){return null;}',
                              'b.pick((Carrier<? extends shared.Child>)null,'
                              '(Carrier<? extends shared.Child>)null).instance()', False),
}


def plan_contracts(state, root):
    if root is not None:
        with state:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    return runner.exercise(binary, base, cases=CASES, refresh=False)
