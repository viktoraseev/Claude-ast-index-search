use std::{fs, process::Command};
use tempfile::TempDir;

#[test]
fn hierarchy_accepts_enum_targets_and_reports_their_interfaces() {
    let directory = TempDir::new().unwrap();
    let project = directory.path().join("project");
    fs::create_dir(&project).unwrap();
    fs::write(
        project.join("Mode.java"),
        r#"interface Named {}
enum Mode implements Named { FIRST, SECOND }
"#,
    )
    .unwrap();
    let run = |args: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
            .current_dir(&project)
            .env("AST_INDEX_CACHE_DIR", directory.path().join("cache"))
            .env("AST_INDEX_DB_PATH", directory.path().join("index.sqlite"))
            .env("NO_COLOR", "1")
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{:?}", output.stderr);
        String::from_utf8(output.stdout).unwrap()
    };
    run(&["rebuild"]);
    let output = run(&["hierarchy", "Mode"]);
    assert!(output.contains("Hierarchy for 'Mode':"), "{output}");
    assert!(output.contains("Named (implements)"), "{output}");
}
