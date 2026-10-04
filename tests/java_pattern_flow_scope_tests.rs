//! Valid Java distinguishes pattern-bound receivers from same-named SDK locals.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn run(root: &Path, cache: &Path, arguments: &[&str]) -> String {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let mut command = Command::new(binary);
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("AST_INDEX_")
            || key.to_string_lossy().starts_with("KOTLIN_INDEX_")
        {
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
        "authored Java pattern command failed"
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn pattern_flow_does_not_lend_project_types_to_else_or_following_sdk_locals() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        r#"class Item {
 public String toString() { return "item"; }
}
class Probe {
 void branch(Object value) { if (value instanceof Item item) { item.toString(); } }
 void conjunction(Object value) { if (value instanceof Item item && item.toString().isEmpty()) {} }
 void alternative(Object value) { if (value instanceof Item item) {} else { String item = "sdk"; item.toString(); } }
 void outside(Object value) { if (value instanceof Item item) {} String item = "sdk"; item.toString(); }
 void guard(Object value) { if (!(value instanceof Item item)) return; item.toString(); }
 void throwing(Object value) { if (!(value instanceof Item item)) { throw new IllegalArgumentException(); } item.toString(); }
 void incomplete(Object value) { if (!(value instanceof Item item)) { System.out.println("continue"); } String item = "sdk"; item.toString(); }
 void combined(Object left, Object right) { if (!(left instanceof Item item) || !(right instanceof Item other)) return; item.toString(); other.toString(); }
}
"#,
    )
    .unwrap();
    run(project.path(), cache.path(), &["rebuild", "--force"]);
    run(project.path(), cache.path(), &["graph", "build"]);
    let output = run(
        project.path(),
        cache.path(),
        &["call-tree", "toString", "--depth", "1"],
    );
    assert_eq!(
        output,
        "Call tree for 'toString':\n  toString\n    ← branch (Probe.java:5)\n    ← conjunction (Probe.java:6)\n    ← guard (Probe.java:9)\n    ← throwing (Probe.java:10)\n    ← combined (Probe.java:12)\n"
    );
}
