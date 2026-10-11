"""Execute retained inference obligations against production, with javac guards."""
import os
from pathlib import Path
import tempfile
import unittest

from common import canonical_json, connect
import java_dependency_gap_contracts as contracts

GUARDS = {
    'target-bound-guard': ('<T extends String> T make(){return null;} shared.Child pick(shared.Child x){return x;}',
                          'b.pick(b.make()).instance()', False),
    'target-overload-guard': ('<T> T make(){return null;} shared.Child pick(shared.Child x){return x;} '
                             'shared.Child pick(String x){return null;}',
                             'b.pick(b.make()).instance()', False),
    'target-witness-guard': ('<T> T make(){return null;} shared.Child pick(shared.Child x){return x;}',
                            'b.pick(b.<String>make()).instance()', False),
    'target-receiver-guard': ('<T> T make(){return null;}', 'b.make().instance()', False),
    'target-fixed-guard': ('Object make(){return null;} shared.Child pick(shared.Child x){return x;}',
                          'b.pick(b.make()).instance()', False),
    'target-parameterized': ('<T> T make(){return null;} shared.Child pick(Carrier<shared.Child> x){return null;}',
                             'b.pick(b.make()).instance()', True),
    'intersection-bound-guard': ('interface Marker {} static class X extends shared.Child implements Marker {} '
                                 'static class Y extends shared.Child {} '
                                 '<T extends shared.Child & Marker> T pick(T a,T c){return a;}',
                                 'b.pick(new Box.X(),new Box.Y()).instance()', False),
    'capture-fixed-guard': ('shared.Child pick(Carrier<shared.Child> x){return null;}',
                           'b.pick((Carrier<? extends shared.Child>)null).instance()', False),
    'capture-repeated-guard': ('<T extends shared.Child> T pick(Carrier<T> a,Carrier<T> c){return null;}',
                              'var value=(Carrier<? extends shared.Child>)null; return b.pick(value,value).instance()', False),
    'capture-write-guard': ('<T extends shared.Child> T pick(Carrier<T> a,T value){return value;}',
                           'b.pick((Carrier<? extends shared.Child>)null,c).instance()', False),
    'raw-invariant-guard': ('shared.Child pick(Carrier<shared.Child> value){return null;}',
                           'b.pick(new Carrier<String>()).instance()', False),
    'raw-unrelated-guard': ('shared.Child pick(Carrier<shared.Child> value){return null;}',
                           'b.pick(new Object()).instance()', False),
}


class RetainedInferenceTests(unittest.TestCase):
    def test_mixed_inventory_cannot_skip_the_java_inference_contract(self):
        from audit import SCHEMA
        import mobile_contracts
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            (root / 'Probe.java').write_text('class Probe {}')
            (root / 'AndroidManifest.xml').write_text('<manifest/>')
            state = connect(Path(temporary) / 'inventory.sqlite')
            try:
                state.executescript(SCHEMA)
                mobile_contracts.inventory(state, root)
                contracts.plan_contracts(state, root)
                coverage = state.execute('SELECT status FROM coverage WHERE feature=?', (contracts.FEATURE,)).fetchone()
                check = state.execute('SELECT status,verdict FROM checks WHERE feature=?', (contracts.FEATURE,)).fetchone()
                self.assertEqual(coverage['status'], 'implemented')
                self.assertEqual(tuple(check), ('pending', None))
                self.assertEqual(state.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 2)
            finally:
                state.close()

    def test_retained_dependency_inference(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        expected, actual = contracts.exercise(binary, base)
        failed = [key for key in expected if canonical_json(expected[key]) != canonical_json(actual.get(key))]
        self.assertEqual(failed, [])

    def test_inference_does_not_invent_compatible_calls(self):
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        base = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        expected, actual = contracts.runner.exercise(binary, base, cases=GUARDS, refresh=False)
        failed = [key for key in expected if canonical_json(expected[key]) != canonical_json(actual.get(key))]
        self.assertEqual(failed, [])


if __name__ == '__main__':
    unittest.main()
