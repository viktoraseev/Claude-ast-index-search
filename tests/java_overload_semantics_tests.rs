//! Independent javac bytecode proves these source-selected overload targets.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SOURCES: &[(&str, &str)] = &[
    (
        "Target.java",
        r#"package fixture;
class Target {
 int array(int[] value) { return 1; }
 int array(java.lang.String[] value) { return 2; }
 int nullable(java.lang.Object value) { return 1; }
 int nullable(java.lang.String value) { return 2; }
 int subtype(Parent value) { return 1; }
 int subtype(Child value) { return 2; }
 int expression(int value) { return 1; }
 int expression(long value) { return 2; }
 int cast(int value) { return 1; }
 int cast(long value) { return 2; }
 int variable(int value) { return 1; }
 int variable(long value) { return 2; }
 int arity(int value) { return 1; }
 int arity(int... value) { return 2; }
 int boxed(int value) { return 1; }
 int boxed(java.lang.Integer value) { return 2; }
 int rank(int[] value) { return 1; }
 int rank(java.lang.Object value) { return 2; }
 int rankField(int[] value) { return 1; }
 int rankField(java.lang.Object value) { return 2; }
 int genericVariable(Box<java.lang.Integer> value) { return 1; }
 int genericVariable(java.lang.Object value) { return 2; }
 int genericField(Box<java.lang.Integer> value) { return 1; }
 int genericField(java.lang.Object value) { return 2; }
}
"#,
    ),
    (
        "Types.java",
        "package fixture;\nclass Parent {}\nclass Child extends Parent {}\nclass Box<T> {}\nclass Holder { int[][] matrix; Box<java.lang.String> value; }\n",
    ),
    (
        "Probe.java",
        r#"package fixture;
class Probe {
 int array(Target target) { return target.array(new int[] {1}); }
 int nullable(Target target) { return target.nullable(null); }
 int subtype(Target target) { return target.subtype(new Child()); }
 int expression(Target target) { return target.expression(1 + 2); }
 int cast(Target target) { return target.cast((short) 1); }
 int variable(Target target, int value) { return target.variable(value); }
 int arity(Target target) { return target.arity(1); }
 int boxed(Target target, java.lang.Integer value) { return target.boxed(value); }
 int rank(Target target, int[][] value) { int[][] local = value; var inferred = new int[1][1]; target.rank(local); target.rank(inferred); target.rank(this.matrix); return target.rank(value); }
 int[][] matrix;
 int rankField(Target target, Holder holder) { return target.rankField(holder.matrix); }
 int genericVariable(Target target, Box<java.lang.String> value) { return target.genericVariable(value); }
 int genericField(Target target, Holder holder) { return target.genericField(holder.value); }
}
"#,
    ),
];

fn run(root: &Path, cache: &Path, arguments: &[&str]) -> Vec<u8> {
    let binary = std::env::var_os("AST_INDEX_TEST_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_ast-index").into());
    let mut command = Command::new(binary);
    for (name, _) in std::env::vars() {
        if name.starts_with("AST_INDEX_") || name.starts_with("KOTLIN_INDEX_") {
            command.env_remove(name);
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
        .args(arguments)
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
fn java_known_argument_types_and_applicability_phases_select_exact_overloads() {
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
    let mut failures = Vec::new();
    for (name, source_line, target_line) in [
        ("array", 3, 3),
        ("nullable", 4, 6),
        ("subtype", 5, 8),
        ("expression", 6, 9),
        ("cast", 7, 11),
        ("variable", 8, 13),
        ("arity", 9, 15),
        ("boxed", 10, 18),
        ("rank", 11, 20),
        ("rankField", 13, 22),
        ("genericVariable", 14, 24),
        ("genericField", 15, 26),
    ] {
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
                "--include-ambiguous",
                "--limit",
                "100",
            ],
        ))
        .unwrap();
        let matched = document["matched"].as_array().unwrap();
        assert_eq!(matched.len(), 1, "source seed must be unique");
        assert_eq!(matched[0]["name"], name);
        assert_eq!(matched[0]["path"], "Probe.java");
        assert_eq!(matched[0]["line"], source_line);
        assert_eq!(matched[0]["kind"], "function");
        let calls: Vec<_> = document["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == name)
            .collect();
        let exact = calls.len() == 1
            && calls[0]["confidence"] == "scoped"
            && calls[0]["other"]["path"] == "Target.java"
            && calls[0]["other"]["line"] == target_line
            && calls[0]["other"]["kind"] == "function";
        if !exact {
            failures.push(name);
        }
    }
    assert!(
        failures.is_empty(),
        "compiler-confirmed overload targets not selected: {failures:?}"
    );
}

#[test]
fn java_overload_phases_preserve_arrays_and_unproven_candidates() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    fs::write(
        root.path().join("More.java"),
        r#"package fixture;
class More {
 int dimensions(int[][] value) { return 1; }
 int dimensions(java.lang.String[][] value) { return 2; }
 int postfix(int value[]) { return 1; }
 int postfix(java.lang.String value[]) { return 2; }
 int covariant(java.lang.Object[] value) { return 1; }
 int covariant(java.lang.String[] value) { return 2; }
 int spread(long... value) { return 1; }
 int spread(int... value) { return 2; }
 int phase(java.lang.Integer value) { return 1; }
 int phase(long value) { return 2; }
 int unbox(long value) { return 1; }
 int unbox(double value) { return 2; }
 int unknown(int value) { return 1; }
 int unknown(long value) { return 2; }
 int crossed(int first, long second) { return 1; }
 int crossed(long first, int second) { return 2; }
 int collide(int value) { return 1; }
 int collide(java.lang.String value) { return 2; }
 int external(java.lang.Object value) { return 1; }
 int external(java.util.List<?> value) { return 2; }
 int reject(int value) { return 1; }
 int projectArray(Other value) { return 1; }
 int projectArray(java.lang.Object value) { return 2; }
 int loose(java.lang.Integer value) { return 1; }
 int loose(int... value) { return 2; }
}
class Other {}
"#,
    )
    .unwrap();
    // Crossed is deliberately ambiguous Java; its graph must retain both
    // declarations. Unknown return/library types and colliding line rows must
    // likewise never turn incomplete syntax evidence into a scoped edge.
    fs::write(
        root.path().join("MoreProbe.java"),
        r#"package fixture;
class MoreProbe {
 int dimensions(More target) { return target.dimensions(new int[1][1]); }
 int postfix(More target) { return target.postfix(new int[] {1}); }
 int covariant(More target) { return target.covariant(new java.lang.String[] {}); }
 int spread(More target) { return target.spread(); }
 int phase(More target) { return target.phase(1); }
 int unbox(More target, java.lang.Integer value) { return target.unbox(value); }
 int unknown(More target) { return target.unknown(value()); }
 int crossed(More target) { return target.crossed(1, 2); }
 void collide(More target) { target.collide(1); target.collide("value"); }
 int external(More target) { return target.external(new java.util.ArrayList<>()); }
 int reject(More target) { return target.reject("value"); }
 int projectArray(More target) { return target.projectArray(new int[] {}); }
 int loose(More target) { return target.loose(1); }
 int value() { return 1; }
}
"#,
    )
    .unwrap();
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    for (name, lines) in [
        ("dimensions", vec![3]),
        ("postfix", vec![5]),
        ("covariant", vec![8]),
        ("spread", vec![10]),
        ("phase", vec![12]),
        ("unbox", vec![13]),
        ("unknown", vec![15, 16]),
        ("crossed", vec![17, 18]),
        ("collide", vec![19, 20]),
        ("external", vec![21, 22]),
        ("reject", vec![]),
        ("projectArray", vec![25]),
        ("loose", vec![26]),
    ] {
        let seed = format!("fixture.MoreProbe.{name}");
        let document: serde_json::Value = serde_json::from_slice(&run(
            root.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                &seed,
                "--include-ambiguous",
                "--limit",
                "100",
            ],
        ))
        .unwrap();
        let calls: Vec<_> = document["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == name)
            .collect();
        let mut actual: Vec<_> = calls
            .iter()
            .map(|edge| {
                assert_eq!(edge["other"]["path"], "More.java");
                assert_eq!(
                    edge["confidence"],
                    if lines.len() == 1 {
                        "scoped"
                    } else {
                        "ambiguous"
                    },
                    "{name}"
                );
                edge["other"]["line"].as_i64().unwrap()
            })
            .collect();
        actual.sort_unstable();
        assert_eq!(actual, lines, "{name}");
    }
}

#[test]
fn field_types_and_invariant_generics_keep_exact_and_unknown_targets() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let root = tempfile::tempdir_in(&artifacts).unwrap();
    let cache = tempfile::tempdir_in(&artifacts).unwrap();
    fs::create_dir(root.path().join(".git")).unwrap();
    fs::write(root.path().join("Generic.java"), r#"package fixture;
class Generic<T> {}
class Parent {}
class Child extends Parent {}
class Fields { int values[][]; Generic<java.lang.String> same; Generic<Generic<java.lang.String>> nested; }
class Choices {
 int same(Generic<java.lang.String> value) { return 1; }
 int same(java.lang.Object value) { return 2; }
 int invariant(Generic<Parent> value) { return 1; }
 int invariant(java.lang.Object value) { return 2; }
 int nested(Generic<Generic<java.lang.Integer>> value) { return 1; }
 int nested(java.lang.Object value) { return 2; }
 int rank(int[] value) { return 1; }
 int rank(java.lang.Object value) { return 2; }
 int unknown(Generic<java.lang.Integer> value) { return 1; }
 int unknown(java.lang.Object value) { return 2; }
}
class GenericProbe {
 int same(Choices target, Fields fields) { return target.same(fields.same); }
 int invariant(Choices target, Generic<Child> value) { return target.invariant(value); }
 int nested(Choices target, Fields fields) { return target.nested(fields.nested); }
 int rank(Choices target, Fields fields) { return target.rank(fields.values); }
 int wildcard(Choices target, Generic<?> value) { return target.unknown(value); }
 <T> int variable(Choices target, Generic<T> value) { return target.unknown(value); }
 int raw(Choices target, Generic value) { return target.unknown(value); }
}
"#).unwrap();
    // The field's short type belongs to its declaring package, even though a
    // same-named type exists in the caller's package.
    for (path, source) in [
        (
            "foreign/Value.java",
            "package foreign; public class Value {}\n",
        ),
        (
            "foreign/Holder.java",
            "package foreign; public class Holder { public Value value; }\n",
        ),
        ("fixture/Value.java", "package fixture; class Value {}\n"),
        (
            "fixture/Scoped.java",
            r#"package fixture;
class Scoped {
 int choose(foreign.Value value) { return 1; }
 int choose(Value value) { return 2; }
 int call(foreign.Holder holder) { return choose(holder.value); }
}
"#,
        ),
    ] {
        let destination = root.path().join(path);
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        fs::write(destination, source).unwrap();
    }
    run(root.path(), cache.path(), &["rebuild", "--force"]);
    run(root.path(), cache.path(), &["graph", "build"]);
    for (seed, name, path, lines) in [
        ("fixture.GenericProbe.same", "same", "Generic.java", vec![7]),
        (
            "fixture.GenericProbe.invariant",
            "invariant",
            "Generic.java",
            vec![10],
        ),
        (
            "fixture.GenericProbe.nested",
            "nested",
            "Generic.java",
            vec![12],
        ),
        (
            "fixture.GenericProbe.rank",
            "rank",
            "Generic.java",
            vec![14],
        ),
        (
            "fixture.GenericProbe.wildcard",
            "unknown",
            "Generic.java",
            vec![15, 16],
        ),
        (
            "fixture.GenericProbe.variable",
            "unknown",
            "Generic.java",
            vec![15, 16],
        ),
        (
            "fixture.GenericProbe.raw",
            "unknown",
            "Generic.java",
            vec![15, 16],
        ),
        (
            "fixture.Scoped.call",
            "choose",
            "fixture/Scoped.java",
            vec![3],
        ),
    ] {
        let document: serde_json::Value = serde_json::from_slice(&run(
            root.path(),
            cache.path(),
            &[
                "--format",
                "json",
                "graph",
                "dependencies",
                seed,
                "--include-ambiguous",
                "--limit",
                "100",
            ],
        ))
        .unwrap();
        assert_eq!(document["matched"].as_array().unwrap().len(), 1, "{seed}");
        let mut actual = Vec::new();
        for edge in document["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|edge| edge["other"]["name"] == name)
        {
            assert_eq!(edge["other"]["path"], path, "{seed}");
            assert_eq!(edge["other"]["kind"], "function", "{seed}");
            let confidence = if lines.len() != 1 {
                "ambiguous"
            } else if seed == "fixture.Scoped.call" {
                "local"
            } else {
                "scoped"
            };
            assert_eq!(edge["confidence"], confidence, "{seed}");
            actual.push(edge["other"]["line"].as_i64().unwrap());
        }
        actual.sort_unstable();
        assert_eq!(actual, lines, "{seed}");
    }
}
