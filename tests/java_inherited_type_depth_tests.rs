//! Legal inherited member types must not disappear at a traversal depth budget.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCE: &str = r#"class Base { public static class Member {} }
class L0 extends Base {}
class L1 extends L0 {}
class L2 extends L1 {}
class L3 extends L2 {}
class L4 extends L3 {}
class L5 extends L4 {}
class L6 extends L5 {}
class L7 extends L6 {}
class L8 extends L7 {}
class L9 extends L8 {}
class L10 extends L9 {}
class L11 extends L10 {}
class L12 extends L11 {}
class Probe extends L12 { Member use(Member value) { return value; } }
"#;

const DIAMOND_WIDTH: usize = 4;
const DIAMOND_DEPTH: usize = 9;

fn diamond_source() -> String {
    let mut source = String::from("interface Base { class Member {} }\n");
    for level in 0..DIAMOND_DEPTH {
        let parents = if level == 0 {
            "Base".to_string()
        } else {
            (0..DIAMOND_WIDTH)
                .map(|branch| format!("D{}_{}", level - 1, branch))
                .collect::<Vec<_>>()
                .join(", ")
        };
        for branch in 0..DIAMOND_WIDTH {
            source.push_str(&format!(
                "interface D{level}_{branch} extends {parents} {{}}\n"
            ));
        }
    }
    let parents = (0..DIAMOND_WIDTH)
        .map(|branch| format!("D{}_{}", DIAMOND_DEPTH - 1, branch))
        .collect::<Vec<_>>()
        .join(", ");
    source.push_str(&format!(
        "class Probe implements {parents} {{ Member use(Member value) {{ return value; }} }}\n"
    ));
    source
}

fn command(root: &Path, cache: &Path, args: &[&str]) -> Command {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_ast-index").into());
    let mut cmd = Command::new(binary);
    for (key, _) in std::env::vars() {
        if key.starts_with("AST_INDEX_") || key.starts_with("KOTLIN_INDEX_") {
            cmd.env_remove(key);
        }
    }
    cmd.current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("AST_INDEX_THREADS", "2")
        .env("NO_COLOR", "1")
        .args(args);
    cmd
}

fn run_bounded(root: &Path, cache: &Path, args: &[&str]) -> Vec<u8> {
    let stdout = root.join("stdout.log");
    let stderr = root.join("stderr.log");
    let mut child = command(root, cache, args)
        .stdout(Stdio::from(fs::File::create(&stdout).unwrap()))
        .stderr(Stdio::from(fs::File::create(&stderr).unwrap()))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success(), "inherited-type fixture command failed");
            return fs::read(stdout).unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("inherited-type fixture exceeded its execution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn inherited_member_type_survives_a_legal_fourteen_level_chain() {
    check_member(SOURCE);
}

#[test]
fn inherited_diamond_deduplicates_declaring_identity_within_a_bounded_budget() {
    check_member(&diamond_source());
}

fn check_member(source: &str) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    fs::write(root.path().join("Probe.java"), source).unwrap();
    run_bounded(root.path(), cache.path(), &["rebuild", "--force"]);
    run_bounded(root.path(), cache.path(), &["graph", "build"]);
    let output = run_bounded(
        root.path(),
        cache.path(),
        &[
            "--format",
            "json",
            "graph",
            "dependencies",
            "Probe.use",
            "--limit",
            "100",
        ],
    );
    let document: serde_json::Value = serde_json::from_slice(&output).unwrap();
    let items = document["items"].as_array().unwrap();
    assert_eq!(document["pagination"]["total"], 1);
    assert_eq!(items.len(), 1);
    let target = &items[0]["other"];
    assert_eq!(target["name"], "Member");
    assert_eq!(target["path"], "Probe.java");
    assert_eq!(target["line"], 1);
    assert_eq!(target["kind"], "class");
    assert_eq!(items[0]["confidence"], "scoped");
}
