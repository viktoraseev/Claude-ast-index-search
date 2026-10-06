//! Java receiver types must disambiguate same-name methods across classes.
//! Expected targets come from these authored sources, never native DB rows.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

#[test]
fn record_constructor_overload_keeps_declared_variable_argument_types() {
    let source = r#"import java.math.BigDecimal;
record Code(String value) {}
record Amount(BigDecimal value) {}
enum Mode { FIRST }
record Item(Code code, Amount amount, Mode mode) {
 Item(Code code, BigDecimal amount, Mode mode) { this(code, new Amount(amount), mode); }
}
class Probe {
 void parameter(Mode mode) { new Item(new Code("x"), new BigDecimal("1"), mode); }
 void local() { Mode mode = Mode.FIRST; new Item(new Code("x"), new BigDecimal("1"), mode); }
 Mode mode;
 void field() { new Item(new Code("x"), new BigDecimal("1"), this.mode); }
 Amount amount;
 void canonical(Code code, Mode mode) { new Item(code, amount, mode); }
 void top(Mode mode) { parameter(mode); local(); field(); }
}
"#;
    check_direct_callers(
        source,
        "Item",
        &[("parameter", 9), ("local", 10), ("field", 12)],
    );

    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(project.path().join("Probe.java"), source).unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["--format", "json", "call-tree", "Item", "--depth", "2"],
    );
    let tree: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(tree["count"], 6);
    let branches: Vec<_> = tree["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| {
            (
                item["depth"].as_u64().unwrap(),
                item["name"].as_str().unwrap(),
                item["line"].as_u64().unwrap(),
                item["status"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        branches,
        vec![
            (1, "parameter", 9, "shown"),
            (2, "top", 15, "shown"),
            (1, "local", 10, "shown"),
            (2, "top", 15, "shown"),
            (1, "field", 12, "shown"),
            (2, "top", 15, "shown"),
        ]
    );
}

#[test]
fn collector_callbacks_use_stream_entries_before_key_value_projection() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.stream.*;
record Item(int leaf) {}
class Probe {
 void collect(Map<String,Item> items) {
  items.entrySet().stream().collect(Collectors.toMap(entry -> entry.getKey(), entry -> entry.getValue().leaf()));
 }
}
"#,
        "leaf",
        &[("collect", 5)],
    );
}

#[test]
fn stream_extrema_keep_both_comparator_inputs_and_optional_result() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.stream.*;
record Item(int leaf) {}
class Probe {
 void compare(Stream<Item> items) { items.max((left, right) -> left.leaf() - right.leaf()); }
 void result(Stream<Item> items) { items.max(Comparator.comparingInt(Item::leaf)).map(item -> item.leaf()); }
}
"#,
        "leaf",
        &[("compare", 5), ("result", 6)],
    );
}

#[test]
fn callback_parameters_bind_qualified_helpers_and_constructors() {
    check_direct_callers(
        r#"import java.util.function.*;
record Item(int leaf) {}
class Helper {
 Helper(Consumer<Item> callback) {}
 void visit(UnaryOperator<Item> callback) {}
}
class Probe {
 void qualified(Helper helper) { helper.visit(item -> { item.leaf(); return item; }); }
 void constructed() { new Helper(item -> item.leaf()); }
 void external(java.util.concurrent.atomic.AtomicReference<Item> helper) { helper.getAndUpdate(item -> { item.leaf(); return item; }); }
}
"#,
        "leaf",
        &[("qualified", 8), ("constructed", 9), ("external", 10)],
    );
}

#[test]
fn anonymous_method_captures_the_enclosing_method_parameter() {
    check_direct_callers(
        r#"record Item(int leaf) {}
class Probe {
 Runnable capture(Item item) {
  return new Runnable() {
   public void run() { item.leaf(); }
  };
 }
}
"#,
        "leaf",
        &[("run", 5)],
    );
}

#[test]
fn callback_overloads_and_class_literal_constructor_inputs_are_typed() {
    check_direct_callers(
        r#"import java.util.function.*;
interface Marker {} record Item(int leaf) implements Marker {}
class Helper<T extends Marker> {
 Helper(Class<T> type, Consumer<T> callback) {}
 void visit(T value) {}
 void visit(UnaryOperator<T> callback) {}
}
class Probe {
 void overloaded(Helper<Item> helper) { helper.visit(item -> { item.leaf(); return item; }); }
 void constructed() { new Helper<>(Item.class, item -> item.leaf()); }
}
"#,
        "leaf",
        &[("overloaded", 9), ("constructed", 10)],
    );
}

#[test]
fn stream_lambda_projections_and_contextual_comparators_keep_result_types() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.stream.*;
record Item(int leaf) { int key() { return 1; } static Item wrap(Item item) { return item; } }
class Probe {
 void factory(List<Item> items) {
  var map = items.stream().collect(Collectors.toMap(Item::key, item -> Item.wrap(item)));
  map.values().forEach(item -> item.leaf());
 }
 void mapped(List<Item> items) { items.stream().map(Item::wrap).distinct().map(item -> Item.wrap(item)).forEach(item -> item.leaf()); }
 void comparator(List<Item> items) { items.stream().max(Comparator.comparingInt(item -> item.leaf())); }
}
"#,
        "leaf",
        &[("factory", 5), ("mapped", 9), ("comparator", 10)],
    );
}

#[test]
fn anonymous_methods_capture_enclosing_instance_fields() {
    check_direct_callers(
        r#"record Item(int leaf) {}
class Probe {
 Item item;
 Runnable capture() { return new Runnable() {
  public void run() { item.leaf(); }
 }; }
}
"#,
        "leaf",
        &[("run", 5)],
    );
}

#[test]
fn anonymous_inherited_fields_and_project_comparators_preserve_shadowing() {
    check_direct_callers(
        r#"import java.util.stream.Stream;
import java.util.function.ToIntFunction;
class Item { public String toString() { return "item"; } }
class Base { String item; void run() {} }
class Comparator {
 static java.util.Comparator<Item> comparingInt(ToIntFunction<String> extractor) { return (left, right) -> 0; }
}
class Probe {
 Item item;
 Base inherited() { return new Base() { public void run() { item.toString(); } }; }
 void shadowed(Stream<Item> items) { items.max(Comparator.comparingInt(text -> text.toString().length())); }
}
"#,
        "toString",
        &[],
    );
}

fn command(root: &Path, cache: &Path, arguments: &[&str]) -> Command {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let mut command = Command::new(binary);
    for (key, _) in std::env::vars() {
        if key.starts_with("AST_INDEX_") || key.starts_with("KOTLIN_INDEX_") {
            command.env_remove(key);
        }
    }
    command
        .current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("AST_INDEX_THREADS", "2")
        .env("NO_COLOR", "1")
        .args(arguments);
    command
}

fn run(root: &Path, cache: &Path, arguments: &[&str]) -> Output {
    let output = command(root, cache, arguments).output().unwrap();
    assert!(
        output.status.success(),
        "Java graph fixture command failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

#[test]
fn graph_resolution_of_an_unknown_stream_pipeline_is_bounded() {
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    let pipeline = ".map(item -> item)".repeat(10);
    fs::write(root.path().join("Probe.java"), format!(
        "class Probe {{ void use(java.util.stream.Stream<String> values) {{ values{pipeline}.forEach(item -> item.toString()); }} }}"
    )).unwrap();
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    let stdout = fs::File::create(root.path().join("stdout.log")).unwrap();
    let stderr = fs::File::create(root.path().join("stderr.log")).unwrap();
    let mut child = command(root.path(), cache.path(), &["graph", "build"])
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("unknown Java stream pipeline exceeded bounded graph-resolution deadline");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn check_direct_callers(source: &str, query: &str, callers: &[(&str, usize)]) {
    check_direct_callers_in_files(source, query, callers, &[]);
}

#[test]
fn enum_method_references_use_functional_arity_before_overload_union() {
    check_direct_callers(
        r#"import java.util.function.*;
record Item(int leaf) {
 int leaf(int left, int right) { return left + right; }
 int direct() { return leaf(1, 2); }
}
enum Probe {
 GET(Item::leaf);
 Probe(ToIntFunction<Item> callback) {}
}
"#,
        "leaf",
        &[("direct", 4), ("GET", 7)],
    );
    check_direct_callers(
        r#"import java.util.function.*;
import lombok.Getter;
@Getter class Item {
 private int value;
 int getValue(int left, int right) { return left + right; }
 int direct() { return getValue(1, 2); }
}
enum Probe {
 GET(Item::getValue);
 Probe(ToIntFunction<Item> callback) {}
}
"#,
        "getValue",
        &[("direct", 6)],
    );
    check_direct_callers(
        r#"import java.util.function.*;
import java.util.List;
import lombok.Getter;
@Getter class Item {
 private List<String> value;
 static List<String> getValue(List<Item> items) { return List.of(); }
 List<String> direct(List<Item> items) { return getValue(items); }
}
enum Probe {
 GET(Item::getValue);
 Probe(Function<Item,List<String>> callback) {}
}
"#,
        "getValue",
        &[("direct", 7)],
    );
}

#[test]
fn contextual_method_references_keep_bound_unbound_and_static_overloads() {
    check_direct_callers(
        r#"import java.util.function.*;
class Item {
 int leaf() { return 0; }
 int leaf(int value) { return value; }
 static int leaf(int left, int right) { return left + right; }
}
class Probe {
 ToIntFunction<Item> unbound = Item::leaf;
 IntSupplier bound = new Item()::leaf;
 IntBinaryOperator staticCall = Item::leaf;
}
"#,
        "leaf",
        &[("unbound", 8), ("bound", 9), ("staticCall", 10)],
    );
}

#[test]
fn qualified_nested_type_references_keep_unbound_instance_callers() {
    check_direct_callers(
        r#"import java.util.function.*;
class Outer {
 static class Item { int leaf() { return 1; } }
}
enum Probe {
 GET(Outer.Item::leaf);
 Probe(ToIntFunction<Outer.Item> callback) {}
}
class Use {
 ToIntFunction<Outer.Item> unbound = Outer.Item::leaf;
}
"#,
        "leaf",
        &[("GET", 6), ("unbound", 10)],
    );
}

#[test]
fn qualified_field_references_keep_bound_instance_callers() {
    check_direct_callers(
        r#"import java.util.function.*;
class Outer {
 static class Item { int leaf() { return 1; } }
 static Item Item;
}
class Probe {
 IntSupplier bound = Outer.Item::leaf;
 IntSupplier captured(Outer Outer) { return Outer.Item::leaf; }
}
"#,
        "leaf",
        &[("bound", 7), ("captured", 8)],
    );
}

#[test]
fn fully_qualified_nested_type_references_resolve_the_complete_path() {
    check_direct_callers(
        r#"package fixture;
import java.util.function.*;
class Outer {
 static class Inner { static class Item { int leaf() { return 1; } } }
}
enum Probe {
 GET(fixture.Outer.Inner.Item::leaf);
 Probe(ToIntFunction<fixture.Outer.Inner.Item> callback) {}
}
"#,
        "leaf",
        &[("GET", 7)],
    );
}

#[test]
fn method_reference_edges_select_compatible_overload_lines() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        r#"import java.util.function.*;
record Item(int leaf) {
 int leaf(int value) { return value; }
 static int staticLeaf(int left, int right) { return left + right; }
}
enum Probe {
 GET(Item::leaf),
 ONE(Item::leaf, true);
 Probe(ToIntFunction<Item> callback) {}
 Probe(ToIntBiFunction<Item,Integer> callback, boolean other) {}
}
class Use {
 IntSupplier bound = new Item(0)::leaf;
 ToIntFunction<Item> unbound = Item::leaf;
 IntBinaryOperator staticCall = Item::staticLeaf;
 ToIntFunction<Item> returned() { return Item::leaf; }
 Object cast = (ToIntFunction<Item>) Item::leaf;
}
"#,
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    for (owner, name, line) in [
        ("GET", "leaf", 2),
        ("ONE", "leaf", 3),
        ("bound", "leaf", 2),
        ("unbound", "leaf", 2),
        ("staticCall", "staticLeaf", 4),
        ("returned", "leaf", 2),
        ("cast", "leaf", 2),
    ] {
        let output = run(
            project.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                owner,
                "--include-ambiguous",
            ],
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        let targets: Vec<_> = report["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|item| item["other"]["name"] == name)
            .map(|item| {
                (
                    item["other"]["line"].as_u64().unwrap(),
                    item["confidence"].as_str().unwrap(),
                )
            })
            .collect();
        assert_eq!(targets, [(line, "local")], "{owner}: {report:#}");
    }
}

#[test]
fn a_project_functional_interface_cannot_be_narrowed_as_a_jdk_interface() {
    check_direct_callers(
        r#"interface ToIntFunction<T> { int apply(T item, int left, int right); }
class Item {
 int leaf(int left, int right) { return left + right; }
}
enum Probe {
 GET(Item::leaf);
 Probe(ToIntFunction<Item> callback) {}
}
"#,
        "leaf",
        &[("GET", 6)],
    );
}

fn check_direct_callers_in_files(
    source: &str,
    query: &str,
    callers: &[(&str, usize)],
    extra: &[(&str, &str)],
) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(project.path().join("Probe.java"), source).unwrap();
    for (path, content) in extra {
        fs::write(project.path().join(path), content).unwrap();
    }
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["call-tree", query, "--depth", "1"],
    );
    let mut expected = format!("Call tree for '{query}':\n  {query}\n");
    for (name, line) in callers {
        expected.push_str(&format!("    ← {name} (Probe.java:{line})\n"));
    }
    assert_eq!(String::from_utf8(output.stdout).unwrap(), expected);
}

#[test]
fn constructor_invocations_survive_a_fresh_graph() {
    check_direct_callers(
        "class Item {\n Item(int value) {}\n}\nclass Probe {\n Item create() { return new Item(1); }\n}\n",
        "Item", &[("create", 5)],
    );
}

#[test]
fn a_method_named_like_its_class_is_not_a_constructor() {
    check_direct_callers(
        "class Item {\n void Item() {}\n}\nclass Probe {\n Item create() { return new Item(); }\n java.util.function.Supplier<Item> reference() { return Item::new; }\n}\n",
        "Item", &[],
    );
}

#[test]
fn implicit_record_accessor_return_type_binds_chained_calls() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nrecord Box(Item item) {}\nclass Probe {\n int read(Box box) { return box.item().leaf(); }\n}\n",
        "leaf", &[("read", 6)],
    );
}

#[test]
fn lambda_capture_callers_keep_enclosing_method_ownership() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nclass Probe {\n Runnable use(Item value) { return () -> value.leaf(); }\n}\n",
        "leaf", &[("use", 5)],
    );
}

#[test]
fn compact_record_constructors_use_component_arity() {
    check_direct_callers(
        "record Item(int value) {\n Item { if (value < 0) throw new IllegalArgumentException(); }\n}\nclass Probe {\n Item create() { return new Item(1); }\n}\n",
        "Item", &[("create", 5)],
    );
}

#[test]
fn record_component_fields_bind_receiver_calls_without_calling_the_accessor() {
    let source = "class Item {\n int leaf() { return 1; }\n}\nrecord Box(Item item) {\n int read() { return item.leaf(); }\n}\n";
    check_direct_callers(source, "leaf", &[("read", 5)]);
    check_direct_callers(source, "item", &[]);
}

#[test]
fn a_qualified_field_read_cannot_call_a_same_name_method() {
    check_direct_callers(
        "class Item {\n static int value;\n static int value() { return 1; }\n}\nclass Probe {\n int read() { return Item.value; }\n}\n",
        "value", &[],
    );
}

#[test]
fn lambda_value_reads_cannot_borrow_a_same_name_method() {
    check_direct_callers_in_files(
        "class Probe {\n boolean value() { return true; }\n void use() { java.util.List.of(true).forEach(value -> { if (value) {} }); }\n}\n",
        "value", &[], &[("Item.java", "class Item { static boolean value; }\n")],
    );
}

#[test]
fn lombok_generated_getter_results_keep_the_declared_field_type() {
    check_direct_callers(
        "import lombok.Getter;\nclass Item {\n int leaf() { return 1; }\n}\n@Getter class Box {\n private Item item;\n}\nclass Probe {\n int read(Box box) { return box.getItem().leaf(); }\n}\n",
        "leaf", &[("read", 9)],
    );
}

#[test]
fn unrelated_getter_annotations_and_explicit_overrides_cannot_invent_return_types() {
    check_direct_callers(
        "@interface Getter {}\nclass Item {\n public String toString() { return \"item\"; }\n}\n@Getter abstract class Box implements javax.swing.ComboBoxEditor {\n Item item;\n}\nclass Probe {\n String use(Box box) { return box.getItem().toString(); }\n}\n",
        "toString", &[],
    );
    for annotation in ["@Getter", "@lombok.Getter"] {
        check_direct_callers(
            &format!("@interface Getter {{}}\nclass Item {{\n public String toString() {{ return \"item\"; }}\n}}\n{annotation} class Box {{\n Item item;\n Object getItem() {{ return null; }}\n}}\nclass Probe {{\n String use(Box box) {{ return box.getItem().toString(); }}\n}}\n"),
            "toString", &[],
        );
    }
}

#[test]
fn implicit_canonical_constructor_does_not_borrow_an_explicit_record_overload() {
    check_direct_callers(
        "record Item(String value) {\n Item(int value) { this(String.valueOf(value)); }\n}\nclass Probe {\n Item canonical() { return new Item(\"x\"); }\n Item overloaded() { return new Item(1); }\n}\n",
        "Item", &[("overloaded", 6)],
    );
}

#[test]
fn constructor_delegation_and_references_have_callable_edges() {
    check_direct_callers(
        "class Item {\n Item(int value) {}\n}\nclass Child extends Item {\n Child() { super(1); }\n}\nclass Probe {\n java.util.function.IntFunction<Item> reference() { return Item::new; }\n}\n",
        "Item", &[("Child", 5), ("reference", 8)],
    );
}

#[test]
fn name_only_call_tree_unions_overloads_without_resolving_the_graph_ambiguity() {
    check_direct_callers(
        "class Probe {\n int leaf(int value) { return value; }\n int leaf(String value) { return value.length(); }\n int use() { return leaf(1); }\n}\n",
        "leaf", &[("use", 4)],
    );
}

#[test]
fn inferred_local_and_foreach_receivers_keep_their_declared_project_type() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nclass Probe {\n Item make() { return new Item(); }\n int inferred() { var item = make(); return item.leaf(); }\n int loop(java.util.List<Item> items) { for (Item item : items) return item.leaf(); return 0; }\n}\n",
        "leaf", &[("inferred", 6), ("loop", 7)],
    );
}

#[test]
fn array_suffix_parameters_cannot_borrow_the_element_classes_method() {
    check_direct_callers(
        "class Item {\n public String toString() { return \"item\"; }\n}\nclass Probe {\n String use(Item values[]) { return values.toString(); }\n}\n",
        "toString", &[],
    );
}

#[test]
fn java_collection_elements_and_stream_lambda_parameters_bind_real_callers() {
    check_direct_callers(
        "import java.util.List;\nclass Item {\n int leaf() { return 1; }\n}\nrecord Box(List<Item> items) {}\nclass Probe {\n int indexed(Box box) { return box.items().get(0).leaf(); }\n void streamed(Box box) { box.items().stream().filter(item -> item.leaf() > 0).forEach(item -> item.leaf()); }\n}\n",
        "leaf", &[("indexed", 7), ("streamed", 8)],
    );
}

#[test]
fn a_project_collection_name_cannot_invent_a_library_element_type() {
    check_direct_callers(
        "class Item {\n public String toString() { return \"item\"; }\n}\nclass List<T> {\n Object get(int index) { return null; }\n}\nclass Probe {\n void use(List<Item> items) { items.get(0).toString(); }\n}\n",
        "toString", &[],
    );
}

#[test]
fn enum_array_streams_bind_inferred_lambda_receivers() {
    check_direct_callers(
        "import java.util.Arrays;\nenum Item {\n ONE;\n int leaf() { return 1; }\n}\nclass Probe {\n void use() { Arrays.stream(Item.values()).forEach(item -> item.leaf()); }\n}\n",
        "leaf", &[("use", 7)],
    );
}

#[test]
fn a_fresh_graph_retains_constructor_calls_from_field_initializers() {
    check_direct_callers(
        "class Item {\n Item() {}\n}\nclass Probe {\n final Item item = new Item();\n}\n",
        "Item",
        &[("item", 5)],
    );
}

#[test]
fn future_results_and_nested_fields_bind_their_source_declared_types() {
    check_direct_callers(
        "import java.util.concurrent.*;\nclass Item {\n int leaf() { return 1; }\n}\nclass Api {\n CompletableFuture<Item> fetch() { return CompletableFuture.completedFuture(new Item()); }\n Item item;\n}\nclass Probe {\n int future(Api api) { return api.fetch().join().leaf(); }\n int field(Api api) { return api.item.leaf(); }\n}\n",
        "leaf", &[("future", 10), ("field", 11)],
    );
}

#[test]
fn generic_record_results_bind_the_receiver_type_argument() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nrecord Pair<L,R>(L left, R right) {}\nclass Probe {\n int use(Pair<String,Item> pair) { return pair.right().leaf(); }\n}\n",
        "leaf", &[("use", 6)],
    );
}

#[test]
fn generic_record_returns_cannot_borrow_a_class_named_like_the_type_parameter() {
    check_direct_callers(
        "class T {\n public String toString() { return \"t\"; }\n}\nrecord Box<T>(T value) {}\nclass Probe {\n String use(Box<Object> box) { return box.value().toString(); }\n}\n",
        "toString", &[],
    );
}

#[test]
fn optional_future_map_and_inferred_loop_elements_keep_their_declared_types() {
    check_direct_callers(
        "import java.util.*;\nimport java.util.concurrent.*;\nclass Item {\n int leaf() { return 1; }\n}\nclass Probe {\n void optional(Optional<Item> value) { value.ifPresent(item -> item.leaf()); }\n void future(CompletableFuture<Item> value) { value.thenAccept(item -> item.leaf()); }\n void map(Map<String,Item> values) { values.values().forEach(item -> item.leaf()); }\n int loop(List<Item> values) { for (var item : values) return item.leaf(); return 0; }\n}\n",
        "leaf", &[("optional", 7), ("future", 8), ("map", 9), ("loop", 10)],
    );
}

#[test]
fn atomic_reference_get_returns_the_declared_element() {
    check_direct_callers(
        "import java.util.concurrent.atomic.*;\nclass Item {\n int leaf() { return 1; }\n}\nclass Probe {\n int use(AtomicReference<Item> value) { return value.get().leaf(); }\n}\n",
        "leaf", &[("use", 6)],
    );
}

#[test]
fn superclass_and_bounded_type_parameter_calls_keep_the_declared_member() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nclass Probe extends Item {\n int inherited() { return super.leaf(); }\n <T extends Item> int bounded(T item) { return item.leaf(); }\n}\n",
        "leaf", &[("inherited", 5), ("bounded", 6)],
    );
}

#[test]
fn future_callback_result_parameters_are_distinct_from_errors() {
    check_direct_callers(
        "import java.util.concurrent.CompletableFuture;\nclass Item {\n int leaf() { return 1; }\n}\nclass Probe {\n void use(CompletableFuture<Item> value) { value.whenComplete((item, error) -> item.leaf()); }\n void async(CompletableFuture<Item> value) { value.thenAcceptAsync(item -> item.leaf()); }\n}\n",
        "leaf", &[("use", 6), ("async", 7)],
    );
    check_direct_callers(
        "import java.util.concurrent.CompletableFuture;\nclass Item {\n public String toString() { return \"item\"; }\n}\nclass Probe {\n void use(CompletableFuture<Item> value) { value.whenComplete((item, error) -> error.toString()); }\n}\n",
        "toString", &[],
    );
}

#[test]
fn nested_future_optional_and_map_entry_results_keep_element_types() {
    check_direct_callers(
        "import java.util.*;\nimport java.util.concurrent.*;\nclass Item {\n int leaf() { return 1; }\n}\nclass Probe {\n void future(CompletableFuture<List<Item>> value) { value.thenAccept(items -> items.forEach(item -> item.leaf())); }\n void optional(List<Item> items) { Optional.ofNullable(items).orElse(List.of()).stream().forEach(item -> item.leaf()); }\n void entries(Map<String,Item> items) { items.entrySet().stream().forEach(entry -> entry.getValue().leaf()); }\n int entry(Map.Entry<String,Item> item) { return item.getValue().leaf(); }\n}\n",
        "leaf", &[("future", 7), ("optional", 8), ("entries", 9), ("entry", 10)],
    );
}

#[test]
fn same_line_chains_can_bind_when_every_expression_has_the_same_target() {
    check_direct_callers(
        "class Item {\n int leaf() { return 1; }\n}\nclass Box {\n Item first() { return null; }\n Item second() { return null; }\n}\nclass Probe {\n int use(Box box) { return box.first().leaf() + box.second().leaf(); }\n}\n",
        "leaf", &[("use", 9)],
    );
}

fn check_receiver(method: &str, expected_file: &str) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("A.java"),
        "package fixture;\nclass A {\n    int leaf() { return 1; }\n}\n",
    )
    .unwrap();
    fs::write(
        project.path().join("B.java"),
        "package fixture;\nclass B {\n    int leaf() { return 2; }\n    int useA(A receiver) { return receiver.leaf(); }\n    int useB(B receiver) { return receiver.leaf(); }\n    A fieldReceiver;\n    int useField() { return fieldReceiver.leaf(); }\n    int useLocal() { A receiver = new A(); return receiver.leaf(); }\n}\n",
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "build"],
    );
    let output = run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "dependencies", method],
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["matched"].as_array().unwrap().len(), 1);
    let targets: Vec<_> = report["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|item| item["other"]["name"] == "leaf")
        .map(|item| {
            (
                item["other"]["path"].as_str().unwrap().to_owned(),
                item["other"]["line"].as_u64().unwrap(),
            )
        })
        .collect();
    assert_eq!(targets, vec![(expected_file.to_owned(), 3)]);
}

#[test]
fn parameter_receiver_does_not_borrow_the_callers_same_name_method() {
    check_receiver("fixture.B.useA", "A.java");
}

#[test]
fn parameter_receiver_can_select_the_callers_own_class() {
    check_receiver("fixture.B.useB", "B.java");
}

#[test]
fn field_receiver_uses_its_declared_type_instead_of_same_name_methods() {
    check_receiver("fixture.B.useField", "A.java");
}

#[test]
fn local_receiver_uses_its_declared_type_instead_of_same_name_methods() {
    check_receiver("fixture.B.useLocal", "A.java");
}

#[test]
fn bare_call_confidence_keeps_local_and_cross_file_inherited_edges_distinct() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Base.java"),
        "class Base {\n int inherited() { return 1; }\n}\n",
    )
    .unwrap();
    fs::write(
        project.path().join("Probe.java"),
        r#"class Probe extends Base {
 int leaf() { return 1; }
 int leaf(int value) { return value; }
 int left() { return leaf(); }
 int right() { return leaf(); }
 int entry() { return left() + right(); }
 int external() { return inherited(); }
 class Inner {
  int enclosing() { return leaf(); }
 }
}
"#,
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    for (seed, target, path, line, confidence) in [
        ("Probe.left", "leaf", "Probe.java", 2, "local"),
        ("Probe.Inner.enclosing", "leaf", "Probe.java", 2, "local"),
        ("Probe.external", "inherited", "Base.java", 2, "scoped"),
    ] {
        let output = run(
            project.path(),
            cache.path(),
            &["--format", "json", "graph", "dependencies", seed],
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        let rows = report["items"].as_array().unwrap();
        assert_eq!(rows.len(), 1, "{seed}");
        assert_eq!(rows[0]["other"]["name"], target);
        assert_eq!(rows[0]["other"]["path"], path);
        assert_eq!(rows[0]["other"]["line"], line);
        assert_eq!(rows[0]["confidence"], confidence, "{seed}");
    }
    for (start, end) in [("Probe.entry", "Probe.leaf"), ("Probe.leaf", "Probe.entry")] {
        for cap in ["0", "1", "3"] {
            let output = run(
                project.path(),
                cache.path(),
                &[
                    "--format",
                    "json",
                    "graph",
                    "path",
                    start,
                    end,
                    "--max-paths",
                    cap,
                ],
            );
            let report: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(report["shortest_paths"], 2);
            assert_eq!(report["pagination"]["total"], 2);
            let rows = report["items"].as_array().unwrap();
            assert_eq!(rows.len(), cap.parse::<usize>().unwrap().min(2));
            for row in rows {
                let hops = row.as_array().unwrap();
                assert_eq!(hops.len(), 3);
                assert_eq!(hops[0]["symbol"]["name"], "entry");
                assert_eq!(hops[0]["edge"], "local");
                assert_eq!(hops[1]["edge"], "local");
                assert_eq!(hops[2]["symbol"]["name"], "leaf");
                assert!(hops[2].get("edge").is_none());
            }
        }
    }
}

fn check_imported_receiver(imports: &str, declared: &str) {
    check_imported_receiver_arguments(imports, declared, "");
}

fn check_imported_receiver_arguments(imports: &str, declared: &str, arguments: &str) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    for package in ["left", "right", "consumer"] {
        fs::create_dir(project.path().join(package)).unwrap();
        fs::write(
            project.path().join(package).join("A.java"),
            format!("package {package};\npublic class A {{\n    public int leaf() {{ return 1; }}\n}}\n"),
        )
        .unwrap();
    }
    fs::write(
        project.path().join("consumer/B.java"),
        format!("package consumer;\n{imports}\nclass B {{\n    int use({declared} receiver) {{ return receiver.leaf({arguments}); }}\n}}\n"),
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &[
            "--format",
            "json",
            "graph",
            "dependencies",
            "consumer.B.use",
        ],
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    let targets: Vec<_> = report["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|item| item["other"]["name"] == "leaf")
        .map(|item| item["other"]["path"].as_str().unwrap())
        .collect();
    assert_eq!(targets, ["left/A.java"]);
}

#[test]
fn single_type_import_shadows_other_files_in_the_current_package() {
    check_imported_receiver("import left.A;", "A");
}

#[test]
fn fully_qualified_parameter_does_not_select_a_same_name_class() {
    check_imported_receiver("", "left.A");
}

#[test]
fn comments_are_not_method_arguments_for_overload_selection() {
    check_imported_receiver_arguments("import left.A;", "A", "/* no arguments */");
}

#[test]
fn fresh_java_call_tree_does_not_borrow_an_external_same_named_method() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        "package fixture;\nimport java.util.List;\nclass Probe {\n    int size() { return 1; }\n    int external(List<?> receiver) { return receiver.size(); }\n    int size(List<?> receiver) { return receiver.size(); }\n}\n",
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["call-tree", "size", "--depth", "1"],
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "Call tree for 'size':\n  size\n"
    );
}

#[test]
fn fresh_java_call_tree_keeps_project_callers_and_filters_before_limits() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        "package fixture;\nimport java.util.List;\nclass Probe {\n    int size() { return 1; }\n    int external(List<?> receiver) { return receiver.size(); }\n    int internal() { return size(); }\n}\n",
    )
    .unwrap();
    fs::write(
        project.path().join("Entry.java"),
        "package fixture;\nclass Entry {\n    int use(Probe receiver) { return receiver.size(); }\n}\n",
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &[
            "call-tree",
            "size",
            "--depth",
            "1",
            "--limit",
            "1",
            "--in-file",
            "Probe.java",
        ],
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "Call tree for 'size':\n  size\n    ← internal (Probe.java:6)\n"
    );
}

#[test]
fn java_call_tree_keeps_syntax_fallback_for_missing_and_stale_graphs() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    let source =
        "class Probe {\n    int leaf() { return 1; }\n    int original() { return leaf(); }\n}\n";
    fs::write(project.path().join("Probe.java"), source).unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);

    for (build, caller) in [(false, "original"), (true, "original")] {
        if build {
            run(project.path(), cache.path(), &["graph", "build"]);
        }
        let status = run(
            project.path(),
            cache.path(),
            &["--format", "json", "graph", "status"],
        );
        let status: Value = serde_json::from_slice(&status.stdout).unwrap();
        assert_eq!(status["graph"]["built"], build);
        let output = run(
            project.path(),
            cache.path(),
            &["call-tree", "leaf", "--depth", "1"],
        );
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            format!("Call tree for 'leaf':\n  leaf\n    ← {caller} (Probe.java:3)\n")
        );
    }

    fs::write(
        project.path().join("Probe.java"),
        source.replace("original", "updatedCaller"),
    )
    .unwrap();
    run(project.path(), cache.path(), &["update"]);
    let status = run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "status"],
    );
    let status: Value = serde_json::from_slice(&status.stdout).unwrap();
    assert_eq!(status["graph"]["stale"], true);
    let output = run(
        project.path(),
        cache.path(),
        &["call-tree", "leaf", "--depth", "1"],
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "Call tree for 'leaf':\n  leaf\n    ← updatedCaller (Probe.java:3)\n"
    );
}

#[test]
fn receiver_qualified_java_call_tree_survives_building_a_graph() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        "class Probe {\n int leaf() { return 1; }\n int run(Probe p) { return p.leaf(); }\n}\n",
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    for built in [false, true] {
        if built {
            run(project.path(), cache.path(), &["graph", "build"]);
        }
        let output = run(
            project.path(),
            cache.path(),
            &["call-tree", "p.leaf", "--depth", "1"],
        );
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "Call tree for 'p.leaf':\n  p.leaf\n    ← run (Probe.java:3)\n",
            "receiver-qualified call tree changed with graph built={built}"
        );
        for (args, expected) in [
            (
                vec!["--depth", "2", "--limit", "1", "--in-file", "Probe.java"],
                "Call tree for 'p.leaf':\n  p.leaf\n    ← run (Probe.java:3)\n",
            ),
            (
                vec!["--in-file", "absent.java"],
                "Call tree for 'p.leaf':\n  p.leaf\n",
            ),
            (vec!["--limit", "0"], "Call tree for 'p.leaf':\n  p.leaf\n"),
        ] {
            let mut command = vec!["call-tree", "p.leaf"];
            command.extend(args);
            let output = run(project.path(), cache.path(), &command);
            assert_eq!(String::from_utf8(output.stdout).unwrap(), expected);
        }
    }
}

#[test]
fn recursive_java_call_tree_survives_building_a_graph() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        "class Probe {\n int recursive() { return recursive(); }\n int recursive(int n) { return n; }\n int member() { return this.member(); }\n}\n",
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    for built in [false, true] {
        if built {
            run(project.path(), cache.path(), &["graph", "build"]);
        }
        let output = run(
            project.path(),
            cache.path(),
            &["call-tree", "recursive", "--depth", "2"],
        );
        assert_eq!(String::from_utf8(output.stdout).unwrap(),
            "Call tree for 'recursive':\n  recursive\n    ← recursive (Probe.java:2) (expanded above)\n",
            "recursive call tree changed with graph built={built}");
        let output = run(
            project.path(),
            cache.path(),
            &["call-tree", "member", "--depth", "2"],
        );
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "Call tree for 'member':\n  member\n    ← member (Probe.java:4) (expanded above)\n"
        );
        if built {
            // Navigation hides edges between matched seeds; inspect the
            // stored target against authored lines to distinguish overloads.
            let output = run(
                project.path(),
                cache.path(),
                &[
                    "query",
                    "SELECT source.line AS source_line, target.line AS target_line FROM symbol_edges e \
                     JOIN symbols source ON source.id=e.source_id \
                     JOIN symbols target ON target.id=e.target_id \
                     WHERE source.name='recursive' ORDER BY source.line, target.line",
                ],
            );
            let report: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(
                report["rows"],
                serde_json::json!([{"source_line": 2, "target_line": 2}])
            );
        }
    }
}

fn check_static_import_binding(import: &str, expected: &[&str]) {
    check_static_import_binding_declarations(import, expected, "");
}

fn check_static_import_binding_declarations(import: &str, expected: &[&str], declarations: &str) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Decoy.java"),
        "package fixture;\nclass Decoy {\n static Object identity() { return null; }\n}\n",
    )
    .unwrap();
    fs::write(project.path().join("Probe.java"),
        format!("package fixture;\n{import}\nclass Probe {{\n Object library() {{ return identity(); }}\n{declarations}}}\n")).unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &[
            "--format",
            "json",
            "graph",
            "dependencies",
            "fixture.Probe.library",
        ],
    );
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    let actual: Vec<_> = document["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["other"]["name"] == "identity")
        .map(|row| row["other"]["path"].as_str().unwrap())
        .collect();
    assert_eq!(actual, expected);
}

#[test]
fn external_single_static_import_does_not_borrow_a_project_function() {
    check_static_import_binding("import static java.util.function.Function.identity;", &[]);
}

#[test]
fn external_wildcard_static_import_does_not_borrow_a_project_function() {
    check_static_import_binding("import static java.util.function.Function.*;", &[]);
}

#[test]
fn project_static_import_keeps_the_real_project_function() {
    check_static_import_binding("import static fixture.Decoy.identity;", &["Decoy.java"]);
}

#[test]
fn project_wildcard_static_import_keeps_the_real_project_function() {
    check_static_import_binding("import static fixture.Decoy.*;", &["Decoy.java"]);
}

#[test]
fn own_member_shadows_an_external_static_import() {
    check_static_import_binding_declarations(
        "import static java.util.function.Function.identity;",
        &["Probe.java"],
        " static Object identity() { return null; }\n",
    );
}

fn check_bare_receiver_scope(same_file: bool, superclass: &str, expected: &[&str]) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    let decoy = "class Decoy {\n String getName() { return \"project\"; }\n}\n";
    let probe = format!(
        "class Probe extends {superclass} {{\n String library() {{ return getName(); }}\n}}\n"
    );
    fs::write(
        project.path().join("Probe.java"),
        if same_file {
            format!("{decoy}{probe}")
        } else {
            probe
        },
    )
    .unwrap();
    if !same_file {
        fs::write(project.path().join("Decoy.java"), decoy).unwrap();
    }
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "dependencies", "Probe.library"],
    );
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    let actual: Vec<_> = document["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["other"]["name"] == "getName")
        .map(|row| row["other"]["path"].as_str().unwrap())
        .collect();
    assert_eq!(actual, expected);
}

#[test]
fn bare_external_inherited_call_does_not_borrow_another_files_method() {
    check_bare_receiver_scope(false, "Thread", &[]);
}

#[test]
fn bare_external_inherited_call_does_not_borrow_a_sibling_class_method() {
    check_bare_receiver_scope(true, "Thread", &[]);
}

#[test]
fn bare_project_inherited_call_keeps_its_real_member() {
    check_bare_receiver_scope(false, "Decoy", &["Decoy.java"]);
}

fn check_bare_scope_source(source: &str, seed: &str, expected_line: u64) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(project.path().join("Probe.java"), source).unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "dependencies", seed],
    );
    let document: Value = serde_json::from_slice(&output.stdout).unwrap();
    let actual: Vec<_> = document["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["other"]["name"] == "getName")
        .map(|row| {
            (
                row["other"]["path"].as_str().unwrap(),
                row["other"]["line"].as_u64().unwrap(),
            )
        })
        .collect();
    assert_eq!(actual, [("Probe.java", expected_line)]);
    let caller = seed.rsplit('.').next().unwrap();
    let line = source
        .lines()
        .position(|line| line.contains(&format!("{caller}(")))
        .unwrap()
        + 1;
    let output = run(
        project.path(),
        cache.path(),
        &["call-tree", "getName", "--depth", "1"],
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.lines()
            .any(|row| row == format!("    ← {caller} (Probe.java:{line})")),
        "Java accessor call-tree lost its real caller: {text}"
    );
}

#[test]
fn bare_call_keeps_its_enclosing_classes_member() {
    check_bare_scope_source("class Outer {\n String getName() { return \"outer\"; }\n class Inner {\n  String library() { return getName(); }\n }\n}\n", "Outer.Inner.library", 2);
}

#[test]
fn bare_call_keeps_its_implicit_record_accessor() {
    check_bare_scope_source(
        "record Probe(String getName) {\n String library() { return getName(); }\n}\n",
        "Probe.library",
        1,
    );
}

#[test]
fn bare_call_keeps_its_explicit_record_accessor() {
    check_bare_scope_source(
        "record Probe(String getName) {\n public String getName() { return getName; }\n String library() { return getName(); }\n}\n",
        "Probe.library",
        2,
    );
}

#[test]
fn bare_call_keeps_implicit_record_accessor_beside_parameterized_overload() {
    check_bare_scope_source(
        "record Probe(String getName) {\n String getName(String suffix) { return getName + suffix; }\n String library() { return getName(); }\n}\n",
        "Probe.library",
        1,
    );
}

fn check_expression_receiver(source: &str, expected: &[&str]) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Decoy.java"),
        "class Decoy {\n String getName() { return \"project\"; }\n}\n",
    )
    .unwrap();
    fs::write(project.path().join("Probe.java"), source).unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["--format", "json", "graph", "dependencies", "Probe.use"],
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    let actual: Vec<_> = report["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["other"]["name"] == "getName")
        .map(|row| row["other"]["path"].as_str().unwrap())
        .collect();
    assert_eq!(actual, expected);
}

#[test]
fn chained_external_call_does_not_borrow_a_project_method() {
    check_expression_receiver(
        "class Probe {\n String use() { return Thread.currentThread().getName(); }\n}\n",
        &[],
    );
}

#[test]
fn external_bound_method_reference_does_not_borrow_a_project_method() {
    check_expression_receiver("class Probe {\n java.util.function.Supplier<String> use(Thread receiver) { return receiver::getName; }\n}\n", &[]);
}

#[test]
fn chained_project_factory_keeps_its_real_member() {
    check_expression_receiver("class Probe {\n Decoy make() { return new Decoy(); }\n String use() { return make().getName(); }\n}\n", &["Decoy.java"]);
}

#[test]
fn chained_project_static_factory_keeps_its_real_member() {
    check_expression_receiver("class Provider {\n static Decoy make() { return new Decoy(); }\n}\nclass Probe {\n String use() { return Provider.make().getName(); }\n}\n", &["Decoy.java"]);
}

#[test]
fn project_bound_method_reference_keeps_its_real_member() {
    check_expression_receiver("class Probe {\n java.util.function.Supplier<String> use(Decoy receiver) { return receiver::getName; }\n}\n", &["Decoy.java"]);
}

#[test]
fn project_unbound_method_reference_keeps_its_real_member() {
    check_expression_receiver("class Probe {\n java.util.function.Function<Decoy,String> use() { return Decoy::getName; }\n}\n", &["Decoy.java"]);
}

#[test]
fn typed_record_receiver_keeps_only_its_accessor_function() {
    check_bare_scope_source(
        "record Rec(String getName) {}\nclass Probe {\n String library(Rec receiver) { return receiver.getName(); }\n}\n",
        "Probe.library",
        1,
    );
}

#[test]
fn enclosing_instance_fields_bind_nested_method_and_constructor_calls() {
    check_direct_callers(
        r#"class Item {
 int leaf() { return 1; }
}
class Probe {
 Item value;
 class Nested {
  Nested() { value.leaf(); }
  int read() { return value.leaf(); }
 }
}
"#,
        "leaf",
        &[("Nested", 7), ("read", 8)],
    );
}

#[test]
fn declared_function_input_binds_a_bare_invocation_lambda() {
    check_direct_callers(
        r#"class Item {
 int leaf() { return 1; }
}
class Probe {
 Item transform(java.util.function.Function<Item,Item> operation) { return null; }
 Item read() { return transform(item -> { item.leaf(); return item; }); }
}
"#,
        "leaf",
        &[("read", 6)],
    );
}

#[test]
fn collectors_map_values_and_merge_inputs_keep_stream_element_types() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.stream.*;
record Item(int key) { int leaf() { return 1; } }
class Probe {
 void collect(List<Item> items) {
  var values = items.stream().collect(Collectors.toMap(Item::key, item -> item));
  values.values().forEach(item -> item.leaf());
 }
 void merge(List<Item> items) {
  items.stream().collect(Collectors.toMap(Item::key, item -> item, (left, right) -> { left.leaf(); return right; }));
 }
}
"#,
        "leaf",
        &[("collect", 5), ("merge", 9)],
    );
}

#[test]
fn generated_getters_substitute_the_receivers_generic_argument() {
    check_direct_callers(
        r#"import lombok.Getter;
record Item(int leaf) {}
@Getter class Box<T> {
 T value;
}
class Probe {
 @Getter Box<Item> box;
 int read() { return getBox().getValue().leaf(); }
}
"#,
        "leaf",
        &[("read", 8)],
    );
}

#[test]
fn enclosing_fields_cannot_cross_a_static_or_inherited_shadow_boundary() {
    for declaration in ["static class Nested", "class Nested extends LibraryBase"] {
        check_direct_callers(
            &format!(
                "class Item {{
 int leaf() {{ return 1; }}
}}
class Probe {{
 Item value;
 {declaration} {{
  int read() {{ return value.leaf(); }}
 }}
}}
"
            ),
            "leaf",
            &[],
        );
    }
}

#[test]
fn an_external_same_name_call_does_not_erase_a_resolved_project_call_on_the_same_line() {
    check_direct_callers(
        r#"import java.util.Optional;
class Item {
 boolean isEmpty() { return false; }
}
class Probe {
 boolean read(Optional<Item> value) { return value.isEmpty() || value.get().isEmpty(); }
}
"#,
        "isEmpty",
        &[("read", 6)],
    );
}

#[test]
fn switch_pattern_bindings_keep_the_record_receivers_type() {
    check_direct_callers(
        r#"record Item(int leaf) {}
class Probe {
 int read(Object value) { return switch (value) { case Item item -> item.leaf(); default -> 0; }; }
}
"#,
        "leaf",
        &[("read", 3)],
    );
}

#[test]
fn generated_getter_type_arguments_keep_their_declaring_nested_scope() {
    check_direct_callers_in_files(
        r#"class Probe {
 int read(Holder holder) { return holder.getBox().getValue().leaf(); }
}
"#,
        "leaf",
        &[("read", 2)],
        &[(
            "Holder.java",
            r#"import lombok.Getter;
@Getter class Box<T> { T value; }
@Getter class Holder {
 record Item(int leaf) {}
 Box<Item> box;
}
"#,
        )],
    );
}

#[test]
fn map_for_each_keeps_key_and_value_callback_types_distinct() {
    check_direct_callers(
        r#"import java.util.Map;
record Item(int leaf) {}
class Probe {
 void read(Map<String,Item> items) { items.forEach((key, value) -> value.leaf()); }
 void wrong(Map<Item,String> items) { items.forEach((key, value) -> value.leaf()); }
}
"#,
        "leaf",
        &[("read", 4)],
    );
}

#[test]
fn future_stream_collectors_keep_mapped_value_types_in_map_callbacks() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.concurrent.*;
import java.util.stream.*;
record Item(int leaf) {}
record Input(Item item) {}
class Probe {
 CompletableFuture<List<Input>> load() { return null; }
 void read() {
  load().thenApply(inputs -> {
   var values = inputs.stream().collect(Collectors.toMap(input -> new Item(0), Input::item));
   values.forEach((key, value) -> value.leaf());
   return values;
  });
 }
}
"#,
        "leaf",
        &[("read", 8)],
    );
}

#[test]
fn map_collector_static_factory_references_keep_the_returned_record_type() {
    check_direct_callers(
        r#"import java.util.*;
import java.util.stream.*;
record Input(int key) {}
record Item(int leaf) {
 static Item from(Input input) { return new Item(input.key()); }
}
class Probe {
 void read(List<Input> inputs) {
  var values = inputs.stream().collect(Collectors.toMap(Input::key, Item::from));
  values.forEach((key, value) -> value.leaf());
 }
}
"#,
        "leaf",
        &[("read", 8)],
    );
}
