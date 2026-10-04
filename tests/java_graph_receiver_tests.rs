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
