use serde_json::Value;
use std::{fs, process::Command};
use tempfile::TempDir;

fn check(source: &str, command: &str, name: &str) -> Value {
    let project = TempDir::new().unwrap();
    let cache = TempDir::new().unwrap();
    fs::write(project.path().join("Example.java"), source).unwrap();
    let run = |args: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
            .current_dir(project.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env("AST_INDEX_DISABLE_GC", "1")
            .env_remove("AST_INDEX_DB_PATH")
            .env_remove("KOTLIN_INDEX_DB_PATH")
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    };
    run(&["rebuild"]);
    serde_json::from_str(&run(&["--format", "json", command, name])).unwrap()
}

#[test]
fn unused_indexed_symbols_have_no_grep_usages() {
    let report = check(
        r#"class Example {
    int unusedField;
    void unusedMethod() {}
    String prose = "unusedMethod() unusedField";
    // unusedMethod() unusedField
}"#,
        "usages",
        "unusedMethod",
    );
    assert_eq!(report["items"].as_array().unwrap().len(), 0, "{report}");
    assert_eq!(report["pagination"]["total"], 0);
}

#[test]
fn local_binding_names_are_not_usages() {
    let source = r#"class Example {
    int item;
    void consume(java.util.List<String> values) {
        values.forEach(item -> {});
        values.forEach((item) -> {});
        for (String item : values) {}
        values.forEach(item -> System.out.println(item));
    }
}"#;
    let report = check(source, "refs", "item");
    let lines: Vec<_> = report["usages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["line"].as_u64().unwrap())
        .collect();
    assert_eq!(lines, [7], "{report}");
}

#[test]
fn instanceof_bindings_are_declarations_but_their_body_uses_remain() {
    let report = check(
        r#"class Example {
    void consume(Object other) {
        if (other instanceof java.util.List<?> item) {}
        if (other instanceof java.util.List<?> item) { item.clear(); }
    }
}"#,
        "refs",
        "item",
    );
    let lines: Vec<_> = report["usages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["line"].as_u64().unwrap())
        .collect();
    assert_eq!(lines, [4], "{report}");
}

#[test]
fn java_callers_require_invocations_and_keep_same_line_calls() {
    let report = check(
        r#"class Example {
    void work() {}
    Object unrelated = work.field;
    String prose = "work()";
    // work()
    Runnable ref = this::work;
    void consume() { work(); }
    void work(int x) { work(); }
    void multiline() { work
        (); }
}"#,
        "callers",
        "work",
    );
    let lines: Vec<_> = report["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["line"].as_u64().unwrap())
        .collect();
    assert_eq!(lines, [7, 8, 9], "{report}");
}

#[test]
fn search_reference_prefix_is_literal() {
    let report = check(
        r#"class Example {
    void consume() { needle_key(); needleXkey(); }
}"#,
        "search",
        "needle_key",
    );
    assert_eq!(
        report["references"],
        serde_json::json!([
            {"name": "needle_key", "usage_count": 1}
        ])
    );
    assert_eq!(report["pagination"]["references"]["total"], 1);
}
