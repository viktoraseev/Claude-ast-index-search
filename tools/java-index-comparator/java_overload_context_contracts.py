"""Finite Java overload contexts already required by compiled regression contracts.

Sources are authored, including deliberately unindexed nominal dependencies.
No native output supplies expected callers; this is not MCP equivalence.
"""
from pathlib import Path
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURE = 'call-tree:java-overload-contexts'
FEATURES = {FEATURE}
SUBJECT = 'disposable-java-overload-contexts-v1'
REASON = ('independent source/javac/CLI: nominal and generic overload signatures, '
          'callback/reference contexts, class receiver parameters, wildcard collection '
          'versus String, source factory precedence and unindexed identity guards; not MCP equivalence')
CASES = (
    ('external_argument_identity_and_callback_element_overloads', """import external.Alpha;
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
""", {'alphaSeed': ['directAlpha', 'lambdaAlpha', 'futureAlpha', 'factoryAlpha'], 'betaSeed': ['directBeta', 'referenceBeta']}),
    ('library_generic_arguments_getter_results_and_lambda_target', """import java.util.Optional;
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
""", {'wrappedSeed': ['wrapped', 'factory'], 'plainSeed': ['plain'], 'functionalSeed': ['functional']}),
    ('class_parameters_are_bound_to_the_selected_receiver', """import java.util.function.UnaryOperator;
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
""", {'valueSeed': ['value'], 'operationSeed': ['operation']}),
    ('collection_wildcards_do_not_borrow_string_overloads', """import java.util.Collection;
import java.util.List;
class Probe {
 static int collectionSeed() { return 1; }
 static int stringSeed() { return 2; }
 static int require(Collection<?> input) { return collectionSeed(); }
 static int require(String input) { return stringSeed(); }
 static void collection(List<String> values) { require(values); }
 static void string(String value) { require(value); }
}
""", {'collectionSeed': ['collection'], 'stringSeed': ['string']}),
    ('source_type_named_stream_keeps_its_own_factory_return_type', """import external.Alpha;
import external.Beta;
class Probe {
 static class Stream { static Alpha of(Alpha value) { return value; } }
 static int alphaSeed() { return 1; }
 static int betaSeed() { return 2; }
 static int pick(Alpha input) { return alphaSeed(); }
 static int pick(Beta input) { return betaSeed(); }
 static void custom(Alpha input) { pick(Stream.of(input)); }
}
""", {'alphaSeed': ['custom'], 'betaSeed': []}),
 )


def plan_contexts(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': SUBJECT}), FEATURE, SUBJECT))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('overload context fixtures must stay inside repository .artifacts')
    runner = Runner(binary, Path(tempfile.mkdtemp(prefix='java-overload-contexts-', dir=base)))
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    dependencies = runner.directory / 'dependencies'
    dependencies.mkdir()
    for name in ('Alpha', 'Beta'):
        (dependencies / (name + '.java')).write_text(f'package external; public class {name} {{}}')
    with (runner.directory / 'dependency-javac.log').open('wb') as log:
        result = subprocess.run(['javac', '-proc:none', '-d', str(dependencies),
                                 *map(str, dependencies.glob('*.java'))], stdout=log, stderr=log, timeout=30)
    if result.returncode:
        raise ToolError('authored dependency fixture does not compile; see private log')
    expected, actual = {}, {}
    for name, source, seeds in CASES:
        (runner.root / 'Probe.java').write_text(source)
        with (runner.directory / (name + '-javac.log')).open('wb') as log:
            result = subprocess.run(['javac', '-proc:none', '-cp', str(dependencies), '-d',
                                     str(runner.directory / 'classes'), str(runner.root / 'Probe.java')],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored overload fixture does not compile; see private log')
        runner.command('rebuild', '--force', '--max-files', 0)
        runner.json('graph', 'build')
        for seed, callers in seeds.items():
            doc = runner.json('call-tree', seed, '--depth', 2, '--limit', 100, '--in-file', '.java')
            key = name + ':' + seed
            expected[key] = callers
            actual[key] = [row['name'] for row in doc['items'] if row['depth'] == 2]
    return expected, actual
