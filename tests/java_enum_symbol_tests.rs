use serde_json::Value;
use std::{fs, process::Command};
use tempfile::TempDir;

#[test]
fn enum_constants_are_navigable_with_qualified_names_and_bodies() {
    let project = TempDir::new().unwrap();
    let cache = TempDir::new().unwrap();
    fs::write(
        project.path().join("State.java"),
        r#"package example.audit;
public enum State {
    OPEN,
    CLOSED(2) { int detail; class Helper {} public int code() { return 2; } };
    State() {}
    State(int value) {}
    public int code() { return 0; }
}
"#,
    )
    .unwrap();
    fs::write(
        project.path().join("Inline.java"),
        "package example.audit;\nenum Inline { FIRST { void code() {} }; void code() {} }\n",
    )
    .unwrap();
    let run = |args: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
            .current_dir(project.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env_remove("AST_INDEX_DB_PATH")
            .env_remove("KOTLIN_INDEX_DB_PATH")
            .env("NO_COLOR", "1")
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        output.stdout
    };
    run(&["rebuild"]);
    for (name, line) in [("OPEN", 3), ("CLOSED", 4)] {
        let result: Value = serde_json::from_slice(&run(&[
            "--format",
            "json",
            "symbol",
            name,
            "--with-content",
        ]))
        .unwrap();
        let items = result["items"].as_array().unwrap();
        assert_eq!(items.len(), 1, "missing enum constant {name}");
        assert_eq!(items[0]["kind"], "constant");
        assert_eq!(items[0]["line"], line);
        assert_eq!(
            items[0]["qualified_name"],
            format!("example.audit.State.{name}")
        );
        assert!(items[0]["content"].as_str().unwrap().contains(name));
    }
    let types: Value =
        serde_json::from_slice(&run(&["--format", "json", "class", "OPEN"])).unwrap();
    assert!(types["items"].as_array().unwrap().is_empty());

    let methods: Value =
        serde_json::from_slice(&run(&["--format", "json", "symbol", "code"])).unwrap();
    let methods = methods["items"].as_array().unwrap();
    assert_eq!(methods.len(), 4);
    let anonymous = methods.iter().find(|item| item["line"] == 4).unwrap();
    assert!(
        anonymous["qualified_name"].is_null(),
        "enum constant body must not borrow the enum's method identity: {anonymous:?}"
    );
    let named = methods.iter().find(|item| item["line"] == 7).unwrap();
    assert_eq!(named["qualified_name"], "example.audit.State.code");
    let inline: Vec<_> = methods
        .iter()
        .filter(|item| item["path"] == "Inline.java")
        .collect();
    assert_eq!(inline.len(), 2);
    assert!(inline.iter().all(|item| item["line"] == 2));
    assert_eq!(
        inline
            .iter()
            .filter(|item| item["qualified_name"].is_null())
            .count(),
        1
    );
    assert_eq!(
        inline
            .iter()
            .filter(|item| item["qualified_name"] == "example.audit.Inline.code")
            .count(),
        1
    );
    for (command, name) in [("symbol", "detail"), ("class", "Helper")] {
        let result: Value =
            serde_json::from_slice(&run(&["--format", "json", command, name])).unwrap();
        let items = result["items"].as_array().unwrap();
        assert_eq!(items.len(), 1);
        assert!(
            items[0]["qualified_name"].is_null(),
            "anonymous descendant {name} acquired a named owner"
        );
    }
}
