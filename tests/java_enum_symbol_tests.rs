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
    CLOSED(2) { public int code() { return 2; } };
    State() {}
    State(int value) {}
    public int code() { return 0; }
}
"#,
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
}
