//! A local Java class must not shadow a package class in another method.
//! JDK26 compiles these sources; javap confirms distinct constructor bindings.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCES: &[(&str, &str)] = &[
    ("Leaf.java", "package fixture;\nclass Leaf {}\n"),
    ("Probe.java", "package fixture;\nclass Probe {\n Object local() {\n  class Leaf {}\n  return new Leaf();\n }\n Leaf outside() { return new Leaf(); }\n}\n"),
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
            assert!(status.success(), "Java local-class fixture command failed");
            return fs::read(output).unwrap();
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("Java local-class fixture exceeded its execution budget");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn local_class_binding_does_not_escape_its_method() {
    check_bindings(
        SOURCES,
        &[
            ("local", 3, vec![("Probe.java", 4)]),
            ("outside", 7, vec![("Leaf.java", 2)]),
        ],
    );
}

#[test]
fn same_named_local_classes_keep_their_own_callable_scope() {
    check_bindings(
        &[
            ("Leaf.java", "package fixture;\nclass Leaf {}\n"),
            (
                "Probe.java",
                r#"package fixture;
class Probe {
 Object first() {
  class Leaf {}
  return new Leaf();
 }
 Object second() {
  class Leaf {}
  return new Leaf();
 }
 Leaf outside(Leaf input) { return new Leaf(); }
}
"#,
            ),
        ],
        &[
            ("first", 3, vec![("Probe.java", 4)]),
            ("second", 7, vec![("Probe.java", 8)]),
            ("outside", 11, vec![("Leaf.java", 2)]),
        ],
    );
}

#[test]
fn local_class_binding_starts_at_its_declaration_and_ends_at_its_block() {
    check_bindings(
        &[
            ("Leaf.java", "package fixture;\nclass Leaf {}\n"),
            (
                "Probe.java",
                r#"package fixture;
class Probe {
 Object blocks() {
  new Leaf();
  {
   class Leaf {}
   new Leaf();
  }
  return new Leaf();
 }
}
"#,
            ),
        ],
        &[("blocks", 3, vec![("Leaf.java", 2), ("Probe.java", 6)])],
    );
}

fn check_bindings(sources: &[(&str, &str)], bindings: &[(&str, i64, Vec<(&str, i64)>)]) {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    for (relative, source) in sources {
        fs::write(root.path().join(relative), source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    for (name, line, targets) in bindings {
        let seed = format!("fixture.Probe.{name}");
        let document: serde_json::Value = serde_json::from_slice(&run(
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
        assert_eq!(document["matched"].as_array().unwrap().len(), 1);
        assert_eq!(document["matched"][0]["name"], *name);
        assert_eq!(document["matched"][0]["path"], "Probe.java");
        assert_eq!(document["matched"][0]["line"], *line);
        assert_eq!(document["matched"][0]["kind"], "function");
        assert_eq!(document["pagination"]["total"], targets.len());
        let edges = document["items"].as_array().unwrap();
        assert_eq!(edges.len(), targets.len());
        let mut actual = Vec::new();
        for edge in edges {
            assert_eq!(edge["other"]["name"], "Leaf");
            assert_eq!(edge["other"]["kind"], "class");
            assert_eq!(edge["confidence"], "scoped");
            actual.push((
                edge["other"]["path"].as_str().unwrap(),
                edge["other"]["line"].as_i64().unwrap(),
            ));
        }
        actual.sort();
        let mut expected = targets.clone();
        expected.sort();
        assert_eq!(
            actual, expected,
            "a local class escaped its lexical binding"
        );
    }
}
