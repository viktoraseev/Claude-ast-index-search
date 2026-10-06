"""Independent compiled Java fixtures for production overload traversal.

The dependency classes deliberately live outside the indexed source root.
These assertions establish CLI/source contracts, not live MCP equivalence.
"""
import subprocess
import unittest
import test_call_tree_mcp_contracts as call_tree_tests


class JavaOverloadContextTests(unittest.TestCase):
    def setUp(self):
        call_tree_tests.CallTreeMcpTests.setUp(self)
        self.dependencies = self.directory / 'dependencies'
        self.dependencies.mkdir()
        for name in ('Alpha', 'Beta'):
            (self.dependencies / (name + '.java')).write_text(
                f'package external; public class {name} {{}}')
        result = subprocess.run(['javac', '-d', str(self.dependencies),
                                 *map(str, self.dependencies.glob('*.java'))],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'synthetic dependency must compile')

    def check_source(self, source, expected):
        (self.root / 'Probe.java').write_text(source)
        result = subprocess.run(['javac', '-cp', str(self.dependencies), '-d',
                                 str(self.directory / 'compiled'), str(self.root / 'Probe.java')],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'synthetic overload fixture must compile')
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        self.fixture.cli('graph', 'build')
        for seed, names in expected.items():
            doc = self.fixture.cli('call-tree', seed, '--depth', '2', '--limit', '100',
                                   '--in-file', '.java')
            self.assertEqual([item['name'] for item in doc['items'] if item['depth'] == 2],
                             names, seed)

    def test_external_argument_identity_and_callback_element_overloads(self):
        self.check_source('''import external.Alpha;
import external.Beta;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.stream.Stream;
class Probe {
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static int pick(Alpha input) { return alphaSeed(); }
 static int pick(Beta input) { return betaSeed(); }
 static void directAlpha(Alpha input) { pick(input); }
 static void directBeta(Beta input) { pick(input); }
 static void lambdaAlpha(List<Alpha> inputs) { inputs.forEach(input -> pick(input)); }
 static void referenceBeta(List<Beta> inputs) { inputs.stream().map(Probe::pick); }
 static CompletableFuture<List<Alpha>> fetch() { return null; }
 static void futureAlpha() { fetch().thenApply(inputs -> inputs.stream().map(Probe::pick).toList()); }
 static void factoryAlpha(Alpha input) { Stream.of(input).map(Probe::pick); }
}
''', {'alphaSeed': ['directAlpha', 'lambdaAlpha', 'futureAlpha', 'factoryAlpha'],
      'betaSeed': ['directBeta', 'referenceBeta']})

    def test_library_generic_arguments_getter_results_and_lambda_target(self):
        self.check_source('''import java.util.Optional;
import java.util.function.UnaryOperator;
class Probe {
 static class Value {}
 static int wrappedSeed() { return 1; }
 static int plainSeed() { return 2; }
 static int functionalSeed() { return 3; }
 static Value getValue() { return new Value(); }
 static int pick(Optional<Value> input) { return wrappedSeed(); }
 static int pick(Value input) { return plainSeed(); }
 static int pick(UnaryOperator<Value> input) { return functionalSeed(); }
 static void wrapped(Optional<Value> input) { pick(input); }
 static void factory() { pick(Optional.of(getValue())); }
 static void plain() { pick(getValue()); }
 static void functional() { pick(input -> input); }
}
''', {'wrappedSeed': ['wrapped', 'factory'], 'plainSeed': ['plain'],
      'functionalSeed': ['functional']})

    def test_class_parameters_are_bound_to_the_selected_receiver(self):
        self.check_source('''import java.util.function.UnaryOperator;
class Probe {
 static class Value {}
 static class Child extends Value {}
 static int valueSeed() { return 1; }
 static int operationSeed() { return 2; }
 static class Holder<T extends Value> {
  int update(T value) { return valueSeed(); }
  int update(UnaryOperator<T> operation) { return operationSeed(); }
 }
 static void value(Holder<Child> holder, Child child) { holder.update(child); }
 static void operation(Holder<Child> holder) { holder.update(child -> child); }
}
''', {'valueSeed': ['value'], 'operationSeed': ['operation']})

    def test_collection_wildcards_do_not_borrow_string_overloads(self):
        self.check_source('''import java.util.Collection;
import java.util.List;
class Probe {
 static int collectionSeed() { return 1; }
 static int stringSeed() { return 2; }
 static int require(Collection<?> input) { return collectionSeed(); }
 static int require(String input) { return stringSeed(); }
 static void collection(List<String> values) { require(values); }
 static void string(String value) { require(value); }
}
''', {'collectionSeed': ['collection'], 'stringSeed': ['string']})

    def test_source_type_named_stream_keeps_its_own_factory_return_type(self):
        self.check_source('''import external.Alpha;
import external.Beta;
class Probe {
 static class Stream { static Alpha of(Alpha value) { return value; } }
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static int pick(Alpha input) { return alphaSeed(); }
 static int pick(Beta input) { return betaSeed(); }
 static void custom(Alpha input) { pick(Stream.of(input)); }
}
''', {'alphaSeed': ['custom'], 'betaSeed': []})


if __name__ == '__main__':
    import unittest
    unittest.main()
