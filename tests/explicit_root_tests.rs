//! Exact Java source boundaries apply even to commands without an index cache.
use std::fs;
use std::path::Path;
use std::process::{Command, Output};

fn invoke(directory: &Path, cwd: &Path, root: &Path, arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .args(arguments)
        .current_dir(cwd)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_DB_PATH", directory.join("index.sqlite"))
        .env("AST_INDEX_CACHE_DIR", directory.join("cache"))
        .env("AST_INDEX_DISABLE_GC", "1")
        .env_remove("AST_INDEX_SUBTREE")
        .env_remove("AST_INDEX_LOCAL_SCOPE")
        .env_remove("AST_INDEX_WALK_UP")
        .output()
        .unwrap()
}

#[test]
fn explicit_root_pins_markerless_java_source_and_rejects_invalid_boundaries() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let directory = tempfile::tempdir_in(artifacts).unwrap();
    let parent = directory.path().join("parent");
    let target = parent.join("java-only");
    let invoker = directory.path().join("invoker");
    fs::create_dir_all(&target).unwrap();
    fs::create_dir_all(invoker.join(".git")).unwrap();
    fs::write(
        parent.join("pom.xml"),
        "<project><artifactId>parent</artifactId></project>",
    )
    .unwrap();
    fs::write(parent.join("ParentOnly.java"), "class ParentOnly {}\n").unwrap();
    fs::write(target.join("TargetOnly.java"), "class TargetOnly {}\n").unwrap();
    fs::write(invoker.join("InvokerOnly.java"), "class InvokerOnly {}\n").unwrap();

    for (cwd, root) in [
        (&target, target.as_path()),
        (&parent, Path::new("java-only")),
    ] {
        let result = invoke(
            directory.path(),
            cwd,
            root,
            &["--format", "json", "detect-stacks"],
        );
        assert!(result.status.success());
        let value: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(value["stacks"], serde_json::json!([]));
        assert_eq!(value["scan_truncated"], false);
    }
    assert!(
        invoke(directory.path(), &invoker, &target, &["rebuild", "--force"])
            .status
            .success()
    );
    let result = invoke(
        directory.path(),
        &invoker,
        &target,
        &[
            "--format",
            "json",
            "--walk-up",
            "class",
            "--pattern",
            "*Only",
        ],
    );
    assert!(result.status.success());
    let value: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(value["items"].as_array().unwrap().len(), 1);
    assert_eq!(value["items"][0]["name"], "TargetOnly");
    assert_eq!(value["items"][0]["path"], "TargetOnly.java");
    assert_eq!(value["pagination"]["total"], 1);

    for invalid in [target.join("missing"), target.join("TargetOnly.java")] {
        let result = invoke(directory.path(), &target, &invalid, &["detect-stacks"]);
        assert!(!result.status.success());
        assert!(result.stdout.is_empty());
        assert!(invoke(directory.path(), &target, &invalid, &["version"])
            .status
            .success());
    }
    assert_eq!(
        fs::read_to_string(target.join("TargetOnly.java")).unwrap(),
        "class TargetOnly {}\n"
    );
}
