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
