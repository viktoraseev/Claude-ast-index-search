//! Enum constants must select their constructor overload, not merely their enum.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

#[test]
fn enum_constants_keep_their_distinct_string_and_int_constructor_dependencies() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        "enum Probe {\n FIRST(\"value\"),\n SECOND(2);\n Probe(String value) {}\n Probe(int value) {}\n}\n",
    )
    .unwrap();
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let run = |args: &[&str]| {
        let mut command = Command::new(&binary);
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("AST_INDEX_")
                || key.to_string_lossy().starts_with("KOTLIN_INDEX_")
            {
                command.env_remove(key);
            }
        }
        let output = command
            .current_dir(project.path())
            .env("AST_INDEX_ROOT", project.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env("AST_INDEX_DISABLE_GC", "1")
            .env("AST_INDEX_THREADS", "2")
            .env("NO_COLOR", "1")
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "authored Java enum command failed");
        output.stdout
    };
    run(&["rebuild", "--force"]);
    run(&["graph", "build"]);
    for (constant, selected, rejected) in [("Probe.FIRST", 4, 5), ("Probe.SECOND", 5, 4)] {
        let document: Value = serde_json::from_slice(&run(&[
            "--format",
            "json",
            "graph",
            "dependencies",
            constant,
        ]))
        .unwrap();
        let items = document["items"].as_array().unwrap();
        assert!(items.iter().any(|row| row["other"]["kind"] == "enum"));
        assert!(
            items.iter().any(|row| row["other"]["kind"] == "function"
                && row["other"]["name"] == "Probe"
                && row["other"]["line"] == selected),
            "the selected enum constructor dependency must be preserved"
        );
        assert!(
            !items.iter().any(|row| row["other"]["kind"] == "function"
                && row["other"]["name"] == "Probe"
                && row["other"]["line"] == rejected),
            "an enum constant must not call the incompatible overload"
        );
    }
}
