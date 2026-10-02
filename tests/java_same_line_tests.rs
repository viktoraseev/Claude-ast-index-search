use ast_index::{db, indexer};
use rusqlite::Connection;
use std::fs;
use tempfile::TempDir;

#[test]
fn same_line_declarations_survive_indexing() {
    let project = TempDir::new().unwrap();
    fs::write(
        project.path().join("Example.java"),
        r#"class Example { Example() {} Example(int x) {} void run() {} void run(int x) {} }
class Left { int value; } class Right { int value; }
"#,
    )
    .unwrap();
    let mut conn = Connection::open_in_memory().unwrap();
    db::init_db(&conn).unwrap();
    indexer::index_directory(&mut conn, project.path(), false, false).unwrap();
    for (name, kind) in [
        ("Example", "function"),
        ("run", "function"),
        ("value", "property"),
    ] {
        let count: i64 = conn
            .query_row(
                "SELECT count(*) FROM symbols WHERE name = ?1 AND kind = ?2",
                [name, kind],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 2, "lost same-line {name} declarations");
    }
}

#[test]
fn same_line_members_keep_their_own_declaring_type() {
    let project = TempDir::new().unwrap();
    fs::write(
        project.path().join("Example.java"),
        r#"package example;
class Left { int value; void run() {} } class Right { int value; void run() {} }
class Outer { void work() { class Local { void run() {} } } void run() {} }
record First(int size) { int size(int n) { return n; } } record Second(int size) {}
"#,
    )
    .unwrap();
    let mut conn = Connection::open_in_memory().unwrap();
    db::init_db(&conn).unwrap();
    indexer::index_directory(&mut conn, project.path(), false, false).unwrap();
    let mut query = conn.prepare("SELECT name, kind, qualified_name FROM symbols WHERE name IN ('value', 'run', 'size') ORDER BY name, kind, id").unwrap();
    let actual = query
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    let expected = [
        ("run", "function", Some("example.Left.run")),
        ("run", "function", Some("example.Right.run")),
        ("run", "function", None),
        ("run", "function", Some("example.Outer.run")),
        ("size", "function", Some("example.First.size")),
        ("size", "function", Some("example.First.size")),
        ("size", "function", Some("example.Second.size")),
        ("size", "property", Some("example.First.size")),
        ("size", "property", Some("example.Second.size")),
        ("value", "property", Some("example.Left.value")),
        ("value", "property", Some("example.Right.value")),
    ]
    .map(|(name, kind, qualified)| {
        (
            name.to_string(),
            kind.to_string(),
            qualified.map(str::to_string),
        )
    });
    assert_eq!(actual, expected);
}
