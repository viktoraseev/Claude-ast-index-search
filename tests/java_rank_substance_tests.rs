//! Ranked Java substance depends on members, not the number of body lines.
use std::fs;
use std::path::Path;
use std::process::Command;

fn run(directory: &Path, root: &Path, args: &[&str]) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .args(args)
        .current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", directory.join("cache"))
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("NO_COLOR", "1")
        .env_remove("AST_INDEX_DB_PATH")
        .env_remove("AST_INDEX_SUBTREE")
        .env_remove("AST_INDEX_LOCAL_SCOPE")
        .env_remove("AST_INDEX_WALK_UP")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn proven_reads_java_members_and_implicit_record_state() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let temporary = tempfile::tempdir_in(artifacts).unwrap();
    let root = temporary.path().join("project");
    fs::create_dir(&root).unwrap();
    // Two same-line declarations additionally exercise the compact per-file
    // cache's identity; a line-only key would conflate them.
    fs::write(
        root.join("Bodies.java"),
        r#"class BodyFull { int value; } class BodyEmpty {}
class BodyComments {
    // No members here
    /* Several lines
       still no members */
}
class BodyInitializer { static { System.nanoTime(); } }
class BodyNested { class Inner {} }
interface BodyAbstract { int run(); }
record BodyRecord(int value) {}
enum BodyEnum { FIRST }
class BodyConstructor { BodyConstructor() {} }
class BodyMethod { int run() { return 1; } }
@Deprecated
class BodyAnnotated { int value; }
"#,
    )
    .unwrap();
    let git = |args: &[&str]| {
        let mut command = Command::new("git");
        // Never inherit a repository/worktree override or invoke real hooks.
        for (key, _) in std::env::vars().filter(|(key, _)| key.starts_with("GIT_")) {
            command.env_remove(key);
        }
        let output = command
            .current_dir(&root)
            .args([
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    };
    git(&["init", "-q", "--template=", "--initial-branch=main"]);
    git(&["add", "Bodies.java"]);
    git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    ]);
    run(temporary.path(), &root, &["rebuild", "--force"]);
    run(
        temporary.path(),
        &root,
        &["hotspots", "--collect", "--full"],
    );
    run(temporary.path(), &root, &["graph", "build"]);
    let report: serde_json::Value = serde_json::from_str(&run(
        temporary.path(),
        &root,
        &[
            "--format", "json", "search", "Body", "--fuzzy", "--rank", "proven", "--limit", "100",
        ],
    ))
    .unwrap();
    for name in [
        "BodyFull",
        "BodyEmpty",
        "BodyComments",
        "BodyInitializer",
        "BodyNested",
        "BodyAbstract",
        "BodyRecord",
        "BodyEnum",
        "BodyConstructor",
        "BodyMethod",
        "BodyAnnotated",
    ] {
        let row = report["symbols"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["name"] == name && row["kind"] != "function")
            .unwrap();
        let empty = matches!(name, "BodyEmpty" | "BodyComments");
        assert_eq!(
            row["rank"]["proven"]["stub"],
            if empty {
                serde_json::json!("empty_class_body")
            } else {
                serde_json::Value::Null
            },
            "{name}"
        );
        let factor = row["rank"]["components"]
            .as_array()
            .unwrap()
            .iter()
            .find(|component| component["name"] == "substance")
            .unwrap();
        assert_eq!(factor["value"], if empty { 0.5 } else { 1.0 }, "{name}");
    }
}
