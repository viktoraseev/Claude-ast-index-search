use serde_json::Value;
use std::{fs, process::Command};
use tempfile::TempDir;

#[test]
fn interface_constants_and_annotation_elements_are_indexed_and_qualified() {
    let project = TempDir::new().unwrap();
    let cache = TempDir::new().unwrap();
    fs::write(
        project.path().join("Contract.java"),
        "package example.audit;\n\
         public interface Contract {\n\
             int FIRST = 1, SECOND = 2;\n\
             String LABEL = \"label\";\n\
             interface Nested { long LIMIT = 10; }\n\
             @interface Option {\n\
                 String value() default \"default\";\n\
                 int count();\n\
                 int DEFAULT_COUNT = 1;\n\
             }\n\
         }\n",
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
    let result: Value = serde_json::from_slice(&run(&[
        "--format",
        "json",
        "symbol",
        "--pattern",
        "*",
        "--in-file",
        "Contract.java",
        "--limit",
        "100",
    ]))
    .unwrap();
    let items = result["items"].as_array().unwrap();
    for (name, kind, line, owner) in [
        ("FIRST", "property", 3, "Contract"),
        ("SECOND", "property", 3, "Contract"),
        ("LABEL", "property", 4, "Contract"),
        ("LIMIT", "property", 5, "Contract.Nested"),
        ("value", "function", 7, "Contract.Option"),
        ("count", "function", 8, "Contract.Option"),
        ("DEFAULT_COUNT", "property", 9, "Contract.Option"),
    ] {
        let matches: Vec<_> = items.iter().filter(|item| item["name"] == name).collect();
        assert_eq!(matches.len(), 1, "missing or duplicated {name}: {items:?}");
        assert_eq!(matches[0]["kind"], kind);
        assert_eq!(matches[0]["line"], line);
        assert_eq!(
            matches[0]["qualified_name"],
            format!("example.audit.{owner}.{name}")
        );
    }
}
