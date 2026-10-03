#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tempfile::TempDir;

#[test]
fn ast_grep_falls_back_when_sg_version_probe_fails() {
    let directory = TempDir::new().unwrap();
    let project = directory.path().join("project");
    fs::create_dir_all(project.join(".git")).unwrap();
    fs::write(
        project.join("Probe.java"),
        "class Probe { void probe() {} }\n",
    )
    .unwrap();
    let scripts = directory.path().join("bin");
    fs::create_dir(&scripts).unwrap();
    for (name, source) in [
        ("sg", "#!/bin/sh\nexit 7\n"),
        (
            "ast-grep",
            "#!/bin/sh\nif [ \"$1\" = '--version' ]; then exit 0; fi\nprintf '[]\\n'\n",
        ),
    ] {
        let path = scripts.join(name);
        fs::write(&path, source).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .args(["agrep", "probe($$$)", "--lang", "java", "--json"])
        .current_dir(&project)
        .env("PATH", &scripts)
        .env("AST_INDEX_CACHE_DIR", directory.path().join("cache"))
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(String::from_utf8(output.stdout).unwrap().trim(), "[]");
}
