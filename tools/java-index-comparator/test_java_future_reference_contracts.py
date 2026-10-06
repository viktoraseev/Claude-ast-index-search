"""Compiled source/CLI checks for future method-reference overloads, not MCP truth."""
import unittest

import test_java_overload_context_contracts as context_tests


class JavaFutureReferenceTests(unittest.TestCase):
    setUp = context_tests.JavaOverloadContextTests.setUp
    check_source = context_tests.JavaOverloadContextTests.check_source

    def test_future_callbacks_preserve_element_identity_and_executor_arity(self):
        self.check_source('''import external.Alpha;
import external.Beta;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
class Probe {
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static CompletableFuture<Integer> pick(Alpha input) { alphaSeed(); return null; }
 static CompletableFuture<Integer> pick(Beta input) { betaSeed(); return null; }
 static CompletableFuture<Alpha> alpha() { return null; }
 static CompletableFuture<Beta> beta() { return null; }
 static class Producer { CompletableFuture<Alpha> fetch(String key) { return null; } }
 static class Key { String value() { return null; } }
 static Producer producer;
 static void directAlpha() { alpha().thenApply(Probe::pick); }
 static void directBeta() { beta().thenApply(Probe::pick); }
 static void nestedAlpha(List<Alpha> values) { values.stream().map(value -> alpha().thenApply(Probe::pick)).toList(); }
 static void futureListAlpha(List<CompletableFuture<Alpha>> values) { values.stream().map(value -> value.thenApply(Probe::pick)).toList(); }
 static void boundAlpha(List<String> keys) { keys.stream().map(producer::fetch).map(value -> value.thenApply(Probe::pick)).toList(); }
 static void chainedAlpha(List<Key> keys) {
  List<String> ids = keys.stream().filter(key -> key.value() != null).map(Key::value).distinct().toList();
  ids.stream().map(producer::fetch).map(value -> value.thenApply(Probe::pick)).toList();
 }
 static void composeAlpha() { alpha().thenCompose(Probe::pick); }
 static void acceptBeta() { beta().thenAccept(Probe::pick); }
 static void asyncAlpha() { alpha().thenApplyAsync(Probe::pick); }
 static void executorBeta(Executor executor) { beta().thenApplyAsync(Probe::pick, executor); }
}
''', {'alphaSeed': ['directAlpha', 'nestedAlpha', 'futureListAlpha', 'boundAlpha', 'chainedAlpha', 'composeAlpha', 'asyncAlpha'],
      'betaSeed': ['directBeta', 'acceptBeta', 'executorBeta']})

    def test_source_receiver_with_future_name_cannot_borrow_jdk_element_context(self):
        self.check_source('''import external.Alpha;
import external.Beta;
import java.util.function.Function;
class Probe {
 static class CompletableFuture<T> {
  void thenApply(Function<Beta, Integer> action) {}
 }
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static int pick(Alpha input) { return alphaSeed(); }
 static int pick(Beta input) { return betaSeed(); }
 static java.util.concurrent.CompletableFuture<Alpha> alpha() { return null; }
 static void realAlpha() { alpha().thenApply(Probe::pick); }
 static void custom(CompletableFuture<Alpha> value) { value.thenApply(Probe::pick); }
}
''', {'alphaSeed': ['realAlpha']})


if __name__ == '__main__':
    unittest.main()
