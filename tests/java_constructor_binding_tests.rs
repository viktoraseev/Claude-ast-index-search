//! A constructor's argument count is not proof of its overload's argument types.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

#[test]
fn constructor_arity_does_not_confidently_bind_the_wrong_overload() {
    check_constructor_binding(
        "class Item {\n Item(int value) {}\n Item(String value) {}\n}\nclass Probe {\n Item create() { return new Item(\"value\"); }\n}\n",
    );
}

#[test]
fn string_literal_does_not_bind_a_project_class_named_string() {
    check_constructor_binding(
        "class Item {\n Item(String value) {}\n Item(java.lang.String value) {}\n}\nclass Probe {\n Item create() { return new Item(\"value\"); }\n}\nclass String {}\n",
    );
}

fn check_constructor_binding(source: &str) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    fs::write(root.path().join("Probe.java"), source).unwrap();
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let run = |args: &[&str]| {
        let mut command = Command::new(&binary);
        for (key, _) in std::env::vars() {
            if key.starts_with("AST_INDEX_") || key.starts_with("KOTLIN_INDEX_") {
                command.env_remove(key);
            }
        }
        let result = command
            .current_dir(root.path())
            .env("AST_INDEX_ROOT", root.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env("AST_INDEX_DISABLE_GC", "1")
            .env("AST_INDEX_THREADS", "2")
            .env("NO_COLOR", "1")
            .args(args)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "authored Java constructor command failed"
        );
        result.stdout
    };
    run(&["rebuild", "--force"]);
    run(&["graph", "build"]);
    let document: Value = serde_json::from_slice(&run(&[
        "--format",
        "json",
        "graph",
        "dependencies",
        "Probe.create",
    ]))
    .unwrap();
    let items = document["items"].as_array().unwrap();
    assert!(
        items
            .iter()
            .any(|row| row["other"]["kind"] == "class" && row["other"]["name"] == "Item"),
        "constructor ambiguity must not erase the class dependency"
    );
    assert!(
        !items.iter().any(|row| row["other"]["kind"] == "function"
            && row["other"]["name"] == "Item"
            && row["other"]["line"] == 2),
        "String literal must not confidently resolve to the incompatible constructor"
    );
    assert!(
        items.iter().any(|row| row["other"]["kind"] == "function"
            && row["other"]["name"] == "Item"
            && row["other"]["line"] == 3),
        "the matching String constructor dependency must survive the fix"
    );
}
