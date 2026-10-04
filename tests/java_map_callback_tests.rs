//! Map callback parameters must retain their distinct declared types.
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
        "Map callback fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn map_key_and_value_callbacks_do_not_borrow_each_others_project_type() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    fs::write(
        project.path().join("Probe.java"),
        r#"import java.util.Map;
class Item {
 public String toString() { return "item"; }
}
class Probe {
 void projectValue(Map<String,Item> items) { items.forEach((key, value) -> value.toString()); }
 void projectKey(Map<Item,String> items) { items.forEach((key, value) -> key.toString()); }
 void libraryValue(Map<Item,String> items) { items.forEach((key, value) -> value.toString()); }
 void libraryKey(Map<String,Item> items) { items.forEach((key, value) -> key.toString()); }
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
        "Call tree for 'toString':\n  toString\n    ← projectValue (Probe.java:6)\n    ← projectKey (Probe.java:7)\n"
    );
}
