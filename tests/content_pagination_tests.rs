use std::fs;
use std::process::Command;

use serde_json::Value;
use tempfile::TempDir;

#[test]
fn content_pages_are_source_ordered_prefixes_with_exact_totals() {
    let project = TempDir::new().unwrap();
    let cache = TempDir::new().unwrap();
    // Multiple files make worker completion order differ from source order.
    for number in (0..12).rev() {
        fs::write(
            project.path().join(format!("Example{number:02}.java")),
            format!("class Example{number:02} {{\n  // needle first\n  // needle second\n}}\n"),
        )
        .unwrap();
    }
    let run = |arguments: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
            .current_dir(project.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env("AST_INDEX_DISABLE_GC", "1")
            .env_remove("AST_INDEX_DB_PATH")
            .env_remove("KOTLIN_INDEX_DB_PATH")
            .args(arguments)
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
    let expected: Vec<Value> = (0..12)
        .flat_map(|number| {
            [(2, "// needle first"), (3, "// needle second")].map(|(line, content)| {
                serde_json::json!({"path": format!("Example{number:02}.java"), "line": line, "content": content})
            })
        })
        .collect();
    for limit in ["24", "1", "3", "0"] {
        let output: Value = serde_json::from_slice(&run(&[
            "--format", "json", "search", "needle", "--limit", limit,
        ]))
        .unwrap();
        let count: usize = limit.parse().unwrap();
        assert_eq!(
            output["content_matches"],
            serde_json::json!(&expected[..count])
        );
        assert_eq!(output["pagination"]["content_matches"]["total"], 24);
        assert_eq!(output["pagination"]["content_matches"]["returned"], count);
        assert_eq!(
            output["pagination"]["content_matches"]["truncated"],
            count < 24
        );
    }
}
