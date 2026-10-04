//! Identity Stream projections must not multiply receiver-inference work.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[test]
fn long_identity_stream_pipeline_does_not_exhaust_resolution_budget() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    let pipeline = ".map(item -> item)".repeat(160);
    fs::write(
        project.path().join("Probe.java"),
        format!("class Probe {{ void use(java.util.stream.Stream<String> values) {{ values{pipeline}.forEach(item -> item.toString()); }} }}"),
    )
    .unwrap();
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_ast-index")));
    let command = |args: &[&str]| {
        let mut command = Command::new(&binary);
        for (key, _) in std::env::vars_os() {
            if key.to_string_lossy().starts_with("AST_INDEX_")
                || key.to_string_lossy().starts_with("KOTLIN_INDEX_")
            {
                command.env_remove(key);
            }
        }
        command
            .current_dir(project.path())
            .env("AST_INDEX_ROOT", project.path())
            .env("AST_INDEX_CACHE_DIR", cache.path())
            .env("AST_INDEX_DISABLE_GC", "1")
            .env("AST_INDEX_THREADS", "2")
            .env("NO_COLOR", "1")
            .args(args);
        command
    };
    assert!(command(&["rebuild", "--force"])
        .output()
        .unwrap()
        .status
        .success());
    let stdout = fs::File::create(project.path().join("graph.stdout.log")).unwrap();
    let stderr = fs::File::create(project.path().join("graph.stderr.log")).unwrap();
    let mut child = command(&["graph", "build"])
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(
                status.success(),
                "authored Java pipeline graph build failed"
            );
            break;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("identity Java Stream projections exceeded the graph-resolution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
