use std::fs;
use std::path::Path;
use std::process::{Command, Output};
use tempfile::TempDir;

fn run(project: &Path, cache: &Path, database: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .current_dir(project)
        .env("AST_INDEX_DB_PATH", database)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .args(args)
        .output()
        .unwrap()
}

fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn clear_removes_the_actual_database_and_preserves_same_stem_neighbors() {
    for name in ["custom.sqlite", "custom", "custom.db"] {
        let temp = TempDir::new().unwrap();
        let project = temp.path().join("project");
        let cache = temp.path().join("cache");
        fs::create_dir(&project).unwrap();
        fs::write(project.join("Alpha.java"), "class Alpha {}\n").unwrap();
        let database = temp.path().join(name);
        success(run(&project, &cache, &database, &["rebuild"]));
        assert!(database.is_file());
        assert!(
            success(run(&project, &cache, &database, &["class", "Alpha"])).contains("Alpha.java:1")
        );
        // A same-stem .db file is not a sidecar of custom.sqlite/custom.
        let neighbor = temp.path().join("custom.db");
        if neighbor != database {
            fs::write(&neighbor, b"unrelated neighbor").unwrap();
        }
        success(run(&project, &cache, &database, &["clear"]));
        assert!(!database.exists(), "clear left the actual database {name}");
        assert!(
            success(run(&project, &cache, &database, &["class", "Alpha"]))
                .contains("Index not found.")
        );
        if neighbor != database {
            assert_eq!(fs::read(&neighbor).unwrap(), b"unrelated neighbor");
        }
    }
}

#[test]
fn rebuild_and_restore_keep_non_db_filenames_and_real_sidecars() {
    let temp = TempDir::new().unwrap();
    let project = temp.path().join("project");
    let cache = temp.path().join("cache");
    fs::create_dir(&project).unwrap();
    let source = project.join("Alpha.java");
    fs::write(&source, "class Alpha {}\n").unwrap();
    let database = temp.path().join("index.sqlite");
    success(run(&project, &cache, &database, &["rebuild"]));
    let backup = temp.path().join("backup.sqlite");
    // Use SQLite backup semantics without depending on its optional backup API.
    let connection = rusqlite::Connection::open(&database).unwrap();
    connection
        .execute("VACUUM INTO ?1", [&backup.to_string_lossy().to_string()])
        .unwrap();
    drop(connection);
    fs::write(&source, "class Beta {}\n").unwrap();
    success(run(&project, &cache, &database, &["rebuild"]));
    let rebuilt = success(run(
        &project,
        &cache,
        &database,
        &["class", "--pattern", "*"],
    ));
    assert!(rebuilt.contains("Beta") && !rebuilt.contains("Alpha [class]"));
    success(run(
        &project,
        &cache,
        &database,
        &["restore", backup.to_str().unwrap()],
    ));
    assert!(
        success(run(&project, &cache, &database, &["class", "Alpha"])).contains("Alpha.java:1")
    );
    for suffix in [
        ".swap",
        ".swap-wal",
        ".swap-shm",
        ".swap-pending",
        ".publish-state-v1",
        ".publish-commit-v1",
    ] {
        assert!(!temp.path().join(format!("index.sqlite{suffix}")).exists());
    }
}
