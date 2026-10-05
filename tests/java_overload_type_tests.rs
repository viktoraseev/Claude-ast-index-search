//! JDK26/javap establish distinct same-arity overload targets for these sources.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCES: &[(&str, &str)] = &[
    ("Target.java", "package fixture;\nclass Target {\n int pick(int value) { return value; }\n int pick(String value) { return value.length(); }\n}\n"),
    ("Probe.java", "package fixture;\nclass Probe {\n int integer(Target target) { return target.pick(1); }\n int string(Target target) { return target.pick(\"x\"); }\n}\n"),
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
            assert!(status.success(), "Java overload fixture command failed");
            return fs::read(stdout).unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("Java overload fixture exceeded its execution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn same_arity_java_overloads_use_argument_types() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    for (name, source) in SOURCES {
        fs::write(root.path().join(name), source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    for (name, line) in [("integer", 3), ("string", 4)] {
        let seed = format!("fixture.Probe.{name}");
        let doc: serde_json::Value = serde_json::from_slice(&run(
            root.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                &seed,
                "--limit",
                "100",
                "--include-ambiguous",
            ],
        ))
        .unwrap();
        assert_eq!(doc["matched"].as_array().unwrap().len(), 1);
        for (key, value) in [("name", name), ("path", "Probe.java"), ("kind", "function")] {
            assert_eq!(doc["matched"][0][key], value);
        }
        assert_eq!(doc["matched"][0]["line"], line);
        let calls: Vec<_> = doc["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == "pick")
            .collect();
        assert_eq!(
            calls.len(),
            1,
            "same arity must not erase argument type evidence"
        );
        assert_eq!(calls[0]["other"]["path"], "Target.java");
        assert_eq!(calls[0]["other"]["line"], line);
        assert_eq!(calls[0]["other"]["kind"], "function");
        assert_eq!(calls[0]["confidence"], "scoped");
    }
}

#[test]
fn scalar_evidence_reaches_each_java_invocation_path_without_guessing_unknown_types() {
    let target = r#"package fixture;
class Target {
 static Target instance() { return new Target(); }
 static int pick(int value) { return value; }
 static int pick(String value) { return value.length(); }
 static int wide(long value) { return 1; }
 static int wide(String value) { return 2; }
 static int flag(boolean value) { return 1; }
 static int flag(int value) { return 2; }
 static int floating(double value) { return 1; }
 static int floating(boolean value) { return 2; }
 static int boxed(Integer value) { return 1; }
 static int boxed(String value) { return 2; }
 static int many(int... value) { return 1; }
 static int many(String... value) { return 2; }
 static int unknown(Object value) { return 1; }
 static int unknown(String value) { return 2; }
}
"#;
    let probe = r#"package fixture;
import static fixture.Target.pick;
class Probe extends Target {
 int imported() { return pick(1); }
 int direct(Target target, int value) { return target.pick(value); }
 int field(Target target) { return target.pick(number); }
 int number = 1;
 int chain() { return Target.instance().pick("x"); }
 int widen(Target target) { return target.wide('x'); }
 int booleanLiteral(Target target) { return target.flag(true); }
 int doubleLiteral(Target target) { return target.floating(1.0); }
 int floatLiteral(Target target) { return target.floating(1.0f); }
 int boxing(Target target) { return target.boxed(1); }
 int expanded(Target target) { return target.many(1, 2); }
 int explicitArray(Target target) { return target.many(new int[] {1}); }
 int nullValue(Target target) { return target.pick(null); }
 int conservative(Target target) { return target.unknown(null); }
 int collision(Target target) { return target.pick(1) + target.pick("x"); }
 int unknownArray(Target target) { return target.many(array()); }
 int[] array() { return new int[] {1}; }
 int unknownReference(Target target) { return target.unknown(reference()); }
 Object reference() { return new Object(); }
}
"#;
    let bare = r#"package fixture;
class Bare extends Target {
 int inherited() { return pick(1); }
 int qualified() { return Target.pick("x"); }
}
"#;
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    for (name, source) in [
        ("Target.java", target),
        ("Probe.java", probe),
        ("Bare.java", bare),
    ] {
        fs::write(root.path().join(name), source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    let cases: &[(&str, &str, &[&str])] = &[
        ("Probe.imported", "pick", &["pick(int value)"]),
        ("Probe.direct", "pick", &["pick(int value)"]),
        ("Probe.field", "pick", &["pick(int value)"]),
        ("Probe.chain", "pick", &["pick(String value)"]),
        ("Probe.widen", "wide", &["wide(long value)"]),
        ("Probe.booleanLiteral", "flag", &["flag(boolean value)"]),
        (
            "Probe.doubleLiteral",
            "floating",
            &["floating(double value)"],
        ),
        (
            "Probe.floatLiteral",
            "floating",
            &["floating(double value)"],
        ),
        ("Probe.boxing", "boxed", &["boxed(Integer value)"]),
        ("Probe.expanded", "many", &["many(int... value)"]),
        ("Probe.explicitArray", "many", &["many(int... value)"]),
        ("Probe.nullValue", "pick", &["pick(String value)"]),
        ("Probe.conservative", "unknown", &["unknown(String value)"]),
        // Missing return-type evidence and distinct calls sharing one row
        // still retain ambiguity; richer known types cannot hide these guards.
        (
            "Probe.unknownArray",
            "many",
            &["many(int... value)", "many(String... value)"],
        ),
        (
            "Probe.unknownReference",
            "unknown",
            &["unknown(Object value)", "unknown(String value)"],
        ),
        (
            "Probe.collision",
            "pick",
            &["pick(int value)", "pick(String value)"],
        ),
        ("Bare.inherited", "pick", &["pick(int value)"]),
        ("Bare.qualified", "pick", &["pick(String value)"]),
    ];
    for (seed, method, signatures) in cases {
        let doc: serde_json::Value = serde_json::from_slice(&run(
            root.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                &format!("fixture.{seed}"),
                "--limit",
                "100",
                "--include-ambiguous",
            ],
        ))
        .unwrap();
        let calls: Vec<_> = doc["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == *method)
            .collect();
        let mut actual: Vec<_> = calls
            .iter()
            .map(|edge| {
                assert_eq!(edge["other"]["path"], "Target.java", "{seed}");
                assert_eq!(edge["other"]["kind"], "function", "{seed}");
                if signatures.len() > 1 {
                    assert_eq!(edge["confidence"], "ambiguous", "{seed}");
                } else {
                    assert_ne!(edge["confidence"], "ambiguous", "{seed}");
                }
                edge["other"]["line"].as_u64().unwrap()
            })
            .collect();
        let mut expected: Vec<_> = signatures
            .iter()
            .map(|signature| {
                target
                    .lines()
                    .position(|line| line.contains(signature))
                    .unwrap() as u64
                    + 1
            })
            .collect();
        actual.sort_unstable();
        expected.sort_unstable();
        assert_eq!(
            actual, expected,
            "{seed}: scalar evidence and conservative guards"
        );
    }
}
