use std::fs;
use std::path::Path;
use std::process::Command;

use serde_json::{json, Value};

fn run(root: &Path, cache: &Path, args: &[&str]) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_ast-index"))
        .current_dir(root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("NO_COLOR", "1")
        .env_remove("AST_INDEX_ROOT")
        .env_remove("AST_INDEX_DB_PATH")
        .env_remove("KOTLIN_INDEX_DB_PATH")
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "disposable caller command failed");
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn java_deep_caller_expansion_preserves_selected_declaration_identity() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let cases = [
        (
            "distinct owners",
            r#"class Target {
    void leaf() {}
}

class Caller {
    void step() { new Target().leaf(); }
}

class Decoy {
    void step() {}
}

class Top {
    void good() { new Caller().step(); }
    void wrong() { new Decoy().step(); }
}
"#,
            6,
            14,
        ),
        (
            "distinct overloads",
            r#"class Target {
    void leaf() {}
}

class Caller {
    void step(int value) { new Target().leaf(); }
    void step(String value) {}
}

class Top {
    void good() { new Caller().step(1); }
    void wrong() { new Caller().step("wrong"); }
}
"#,
            6,
            11,
        ),
        (
            "same-line overloads",
            r#"class Target {
    void leaf() {}
}

class Caller {
    void step(int value) { new Target().leaf(); } void step(String value) {}
}

class Top {
    void good() { new Caller().step(1); }
    void wrong() { new Caller().step("wrong"); }
}
"#,
            6,
            10,
        ),
    ];
    let mut failures = Vec::new();
    for (label, source, step_line, good_line) in cases {
        let project = tempfile::tempdir_in(&artifacts).unwrap();
        let cache = tempfile::tempdir_in(&artifacts).unwrap();
        fs::create_dir(project.path().join(".git")).unwrap();
        fs::write(project.path().join("Probe.java"), source).unwrap();
        run(project.path(), cache.path(), &["rebuild", "--force"]);
        run(project.path(), cache.path(), &["graph", "build"]);
        let tree: Value = serde_json::from_str(&run(
            project.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "call-tree",
                "leaf",
                "--depth",
                "2",
                "--limit",
                "100",
            ],
        ))
        .unwrap();
        let expected = json!([
            {"depth": 1, "name": "step", "path": "Probe.java", "line": step_line, "status": "shown"},
            {"depth": 2, "name": "good", "path": "Probe.java", "line": good_line, "status": "shown"},
        ]);
        if tree["items"] != expected || tree["count"] != 2 {
            failures.push(label);
        }
    }
    assert!(
        failures.is_empty(),
        "deep caller identity failed: {failures:?}"
    );
}

#[test]
fn java_caller_formats_preserve_source_sites_and_tree_states() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(project.path().join(".git")).unwrap();
    let path = "Probe \"λ\".java";
    fs::write(
        project.path().join(path),
        r#"class Probe {
    void leaf() {}
    void alpha() { leaf(); leaf(); beta(); }
    void beta() { leaf(); alpha(); }
    void top() { alpha(); }
    // leaf() is prose
    String text = "leaf()";
}
"#,
    )
    .unwrap();
    let node = |depth, name, line, status| json!({"depth": depth, "name": name, "path": path, "line": line, "status": status});
    // Authored call chain: leaf <- alpha <- beta <- alpha (cycle), plus top.
    let expected = json!([
        node(1, "alpha", 3, "shown"),
        node(2, "beta", 4, "shown"),
        node(3, "alpha", 3, "recursive"),
        node(2, "top", 5, "shown"),
        node(1, "beta", 4, "expanded_above"),
    ]);
    for state in ["unindexed", "indexed", "fresh"] {
        match state {
            "indexed" => {
                run(project.path(), cache.path(), &["rebuild", "--force"]);
            }
            "fresh" => {
                run(project.path(), cache.path(), &["graph", "build"]);
            }
            _ => {}
        }
        let tree: Value = serde_json::from_str(&run(
            project.path(),
            cache.path(),
            &["--format", "json", "call-tree", "leaf", "--depth", "3"],
        ))
        .unwrap();
        assert_eq!(tree["schema_version"], 2);
        assert_eq!(tree["function"], "leaf");
        assert_eq!(tree["max_depth"], 3);
        assert_eq!(tree["limit_per_level"], 10);
        assert_eq!(tree["count"], 5);
        assert_eq!(tree["items"], expected, "tree changed in {state}");
        for budget in [["--limit", "0"], ["--depth", "0"], ["--in-file", "absent"]] {
            let mut args = vec!["--format", "json", "call-tree", "leaf"];
            args.extend(budget);
            let empty: Value =
                serde_json::from_str(&run(project.path(), cache.path(), &args)).unwrap();
            assert_eq!(empty["items"], json!([]));
            assert_eq!(empty["count"], 0);
        }
        // Callers reports invocation lines, while the tree reports owning declarations.
        if state != "unindexed" {
            let callers: Value = serde_json::from_str(&run(
                project.path(),
                cache.path(),
                &["--format", "json", "callers", "leaf"],
            ))
            .unwrap();
            assert_eq!(callers["pagination"]["total"], 2);
            assert_eq!(
                callers["items"],
                json!([
                    {"path": path, "line": 3, "content": "    void alpha() { leaf(); leaf(); beta(); }"},
                    {"path": path, "line": 4, "content": "    void beta() { leaf(); alpha(); }"},
                ])
            );
        }
    }
}
