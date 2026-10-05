//! Public inherited member types can be exported by an accessible subclass.
//! JDK26 accepts the positive sources and rejects the direct hidden-owner use.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCES: &[(&str, &str)] = &[
    (
        "base/HiddenOwner.java",
        "package fixture.base;\nclass HiddenOwner {\n public static class Member {}\n}\n",
    ),
    (
        "base/Exported.java",
        "package fixture.base;\npublic class Exported extends HiddenOwner {}\n",
    ),
    (
        "client/Client.java",
        "package fixture.client;\nimport static fixture.base.Exported.Member;\nclass Client {\n Member use(Member value) { return value; }\n}\n",
    ),
    (
        "client/Direct.java",
        "package fixture.client;\nclass Direct {\n fixture.base.HiddenOwner.Member wrong(fixture.base.HiddenOwner.Member value) { return value; }\n}\n",
    ),
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
    let output = root.join("stdout.log");
    let mut child = command
        .current_dir(root)
        .env("AST_INDEX_ROOT", root)
        .env("AST_INDEX_CACHE_DIR", cache)
        .env("AST_INDEX_DISABLE_GC", "1")
        .env("AST_INDEX_THREADS", "2")
        .env("NO_COLOR", "1")
        .args(args)
        .stdout(Stdio::from(fs::File::create(&output).unwrap()))
        .stderr(Stdio::from(
            fs::File::create(root.join("stderr.log")).unwrap(),
        ))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(
                status.success(),
                "Java inherited-owner fixture command failed"
            );
            return fs::read(output).unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("Java inherited-owner fixture exceeded its execution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn public_subclass_exports_member_without_exposing_the_inaccessible_owner() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    for (relative, source) in SOURCES {
        let path = root.path().join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    let document: serde_json::Value = serde_json::from_slice(&run(
        root.path(),
        cache.path(),
        &[
            "--format",
            "json",
            "graph",
            "dependencies",
            "fixture.client.Client.use",
            "--limit",
            "100",
            "--include-ambiguous",
        ],
    ))
    .unwrap();
    assert_eq!(document["matched"].as_array().unwrap().len(), 1);
    assert_eq!(document["matched"][0]["name"], "use");
    assert_eq!(document["matched"][0]["path"], "client/Client.java");
    assert_eq!(document["matched"][0]["line"], 4);
    assert_eq!(document["pagination"]["total"], 1);
    assert_eq!(document["items"].as_array().unwrap().len(), 1);
    let edge = &document["items"][0];
    assert_eq!(edge["other"]["name"], "Member");
    assert_eq!(edge["other"]["path"], "base/HiddenOwner.java");
    assert_eq!(edge["other"]["line"], 3);
    assert_eq!(edge["other"]["kind"], "class");
    assert_eq!(edge["confidence"], "scoped");
    let negative: serde_json::Value = serde_json::from_slice(&run(
        root.path(),
        cache.path(),
        &[
            "--format",
            "json",
            "graph",
            "dependencies",
            "fixture.client.Direct.wrong",
            "--limit",
            "100",
            "--include-ambiguous",
        ],
    ))
    .unwrap();
    assert_eq!(negative["matched"].as_array().unwrap().len(), 1);
    assert_eq!(negative["pagination"]["total"], 0);
    assert!(negative["items"].as_array().unwrap().is_empty());
}
