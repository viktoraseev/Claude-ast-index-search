//! Compiler-established primitive specificity and shadowed-reference overloads.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCES: &[(&str, &str)] = &[
    ("Target.java", "package fixture;\nclass Target {\n int numeric(int value) { return 1; }\n int numeric(long value) { return 2; }\n int external(fixture.String value) { return 3; }\n int external(java.lang.Object value) { return 4; }\n}\n"),
    ("String.java", "package fixture;\nclass String {}\n"),
    ("Probe.java", "package fixture;\nclass Probe {\n int integer(Target target) { return target.numeric(1); }\n int character(Target target) { return target.numeric('x'); }\n int longValue(Target target) { return target.numeric(1L); }\n int string(Target target) { return target.external(\"x\"); }\n}\n"),
];

fn run(root: &Path, cache: &Path, args: &[&str]) -> Vec<u8> {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_ast-index").into());
    let mut command = Command::new(binary);
    for (key, _) in std::env::vars() {
        if key.starts_with("AST_INDEX_") || key.starts_with("KOTLIN_INDEX_") {
            command.env_remove(key);
        }
    }
    let stdout = root.join("stdout.log");
    let mut child = command
        .current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("AST_INDEX_THREADS", "2")
        .env("NO_COLOR", "1")
        .args(args)
        .stdout(Stdio::from(fs::File::create(&stdout).unwrap()))
        .stderr(Stdio::from(
            fs::File::create(root.join("stderr.log")).unwrap(),
        ))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success(), "Java specificity fixture command failed");
            return fs::read(stdout).unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("Java specificity fixture exceeded its execution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn java_overloads_choose_specific_primitive_and_unshadowed_literal_targets() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    for (path, source) in SOURCES {
        fs::write(root.path().join(path), source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    for (name, source_line, target_name, target_line) in [
        ("integer", 3, "numeric", 3),
        ("character", 4, "numeric", 3),
        ("longValue", 5, "numeric", 4),
        ("string", 6, "external", 6),
    ] {
        let document: serde_json::Value = serde_json::from_slice(&run(
            root.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                &format!("fixture.Probe.{name}"),
                "--limit",
                "100",
                "--include-ambiguous",
            ],
        ))
        .unwrap();
        assert_eq!(document["matched"].as_array().unwrap().len(), 1);
        assert_eq!(document["matched"][0]["name"], name);
        assert_eq!(document["matched"][0]["path"], "Probe.java");
        assert_eq!(document["matched"][0]["line"], source_line);
        assert_eq!(document["matched"][0]["kind"], "function");
        let calls: Vec<_> = document["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == target_name)
            .collect();
        assert_eq!(
            calls.len(),
            1,
            "compiler-established overload remains unresolved: {name}"
        );
        assert_eq!(calls[0]["other"]["path"], "Target.java");
        assert_eq!(calls[0]["other"]["line"], target_line);
        assert_eq!(calls[0]["other"]["kind"], "function");
        assert_eq!(calls[0]["confidence"], "scoped");
    }
}
