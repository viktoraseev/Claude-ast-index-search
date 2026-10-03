#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tempfile::TempDir;

#[test]
fn linked_worktree_installs_hooks_in_the_common_git_directory() {
    let directory = TempDir::new().unwrap();
    let common = directory.path().join("common/.git");
    let metadata = common.join("worktrees/linked");
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("commondir"), "../..\n").unwrap();
    let project = directory.path().join("linked");
    fs::create_dir(&project).unwrap();
    fs::write(
        project.join(".git"),
        "gitdir: ../common/.git/worktrees/linked\n",
    )
    .unwrap();
    fs::write(project.join("Linked.java"), "public class Linked {}\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .arg("install-git-hooks")
        .current_dir(&project)
        .env("AST_INDEX_ROOT", &project)
        .env("AST_INDEX_CACHE_DIR", directory.path().join("cache"))
        .output()
        .unwrap();
    assert!(output.status.success());
    for event in ["post-checkout", "post-merge", "post-rewrite"] {
        assert!(common.join("hooks").join(event).is_file());
    }
    assert!(!metadata.join("hooks").exists());
}

#[test]
fn claude_installer_reports_plugin_failure_but_allows_marketplace_warning() {
    for (marketplace, plugin, succeeds) in [(0, 0, true), (7, 0, true), (0, 7, false)] {
        let directory = TempDir::new().unwrap();
        let project = directory.path().join("project");
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::write(project.join("Probe.java"), "public class Probe {}\n").unwrap();
        let scripts = directory.path().join("bin");
        fs::create_dir(&scripts).unwrap();
        let stub = scripts.join("claude");
        fs::write(
            &stub,
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$AUDIT_CLAUDE_LOG\"\n\
             case \"$*\" in\n\
             'plugin marketplace add defendend/Claude-ast-index-search') exit \"$AUDIT_MARKETPLACE_STATUS\" ;;\n\
             'plugin install ast-index') exit \"$AUDIT_PLUGIN_STATUS\" ;;\n\
             esac\nexit 99\n",
        )
        .unwrap();
        fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
        let log = directory.path().join("calls.log");
        let mut paths = vec![scripts];
        paths.extend(std::env::split_paths(
            &std::env::var_os("PATH").unwrap_or_default(),
        ));
        let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
            .arg("install-claude-plugin")
            .current_dir(&project)
            .env("PATH", std::env::join_paths(paths).unwrap())
            .env("AST_INDEX_ROOT", &project)
            .env("AST_INDEX_CACHE_DIR", directory.path().join("cache"))
            .env("AUDIT_CLAUDE_LOG", &log)
            .env("AUDIT_MARKETPLACE_STATUS", marketplace.to_string())
            .env("AUDIT_PLUGIN_STATUS", plugin.to_string())
            .output()
            .unwrap();
        assert_eq!(output.status.success(), succeeds);
        assert_eq!(
            fs::read_to_string(log).unwrap().lines().collect::<Vec<_>>(),
            [
                "plugin marketplace add defendend/Claude-ast-index-search",
                "plugin install ast-index"
            ]
        );
        assert_eq!(
            fs::read_to_string(project.join("Probe.java")).unwrap(),
            "public class Probe {}\n"
        );
    }
}
