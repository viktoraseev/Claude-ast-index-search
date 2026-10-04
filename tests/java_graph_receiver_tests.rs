//! Java receiver types must disambiguate same-name methods across classes.
//! Expected targets come from these authored sources, never native DB rows.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn run(root: &Path, cache: &Path, arguments: &[&str]) -> Output {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let mut command = Command::new(binary);
    for (key, _) in std::env::vars() {
        if key.starts_with("AST_INDEX_") || key.starts_with("KOTLIN_INDEX_") {
            command.env_remove(key);
        }
    }
    let output = command
        .current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("AST_INDEX_THREADS", "2")
        .env("NO_COLOR", "1")
        .args(arguments)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "Java graph fixture command failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
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
