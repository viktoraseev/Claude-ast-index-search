use ast_index::{db, indexer};
use rusqlite::Connection;
use std::fs;
use tempfile::TempDir;

const JAVA: &str = "package example.audit;
public class Portfolio {
    private int balance;
    public Portfolio() {}
    public int total() { return balance; }
    public interface Reader { int read(); }
    public enum Status { OPEN, CLOSED }
    public record Position(int amount) {}
    public static class Nested { void run() {} }
    void locals() {
        class Local {}
        Runnable anonymous = new Runnable() { public void run() {} };
    }
}
";

#[test]
fn java_index_preserves_package_and_declaring_type_names() {
    let project = TempDir::new().unwrap();
    fs::write(project.path().join("Portfolio.java"), JAVA).unwrap();
    let mut conn = Connection::open_in_memory().unwrap();
    db::init_db(&conn).unwrap();
    indexer::index_directory(&mut conn, project.path(), false, false).unwrap();
    let mut query = conn.prepare("SELECT kind, name, qualified_name FROM symbols WHERE qualified_name IS NOT NULL ORDER BY qualified_name,kind").unwrap();
    let actual = query
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    let mut expected = [
        ("class", "Portfolio", "example.audit.Portfolio"),
        ("function", "Portfolio", "example.audit.Portfolio.Portfolio"),
        ("property", "balance", "example.audit.Portfolio.balance"),
        ("function", "total", "example.audit.Portfolio.total"),
        ("interface", "Reader", "example.audit.Portfolio.Reader"),
        ("function", "read", "example.audit.Portfolio.Reader.read"),
        ("enum", "Status", "example.audit.Portfolio.Status"),
        ("constant", "OPEN", "example.audit.Portfolio.Status.OPEN"),
        (
            "constant",
            "CLOSED",
            "example.audit.Portfolio.Status.CLOSED",
        ),
        ("class", "Position", "example.audit.Portfolio.Position"),
        (
            "property",
            "amount",
            "example.audit.Portfolio.Position.amount",
        ),
        (
            "function",
            "amount",
            "example.audit.Portfolio.Position.amount",
        ),
        ("class", "Nested", "example.audit.Portfolio.Nested"),
        ("function", "run", "example.audit.Portfolio.Nested.run"),
        ("function", "locals", "example.audit.Portfolio.locals"),
    ]
    .map(|(kind, name, qualified)| (kind.to_string(), name.to_string(), qualified.to_string()));
    expected.sort_by(|a, b| (&a.2, &a.0).cmp(&(&b.2, &b.0)));
    assert_eq!(actual, expected);
}

#[test]
fn default_package_types_keep_their_unqualified_identity() {
    let names = ast_index::parsers::treesitter::java::collect_qualified_names(
        "class Example { int value; }",
    )
    .unwrap();
    assert_eq!(
        names
            .get(&("class".into(), 1, "Example".into()))
            .map(String::as_str),
        Some("Example")
    );
    assert_eq!(
        names
            .get(&("property".into(), 1, "value".into()))
            .map(String::as_str),
        Some("Example.value")
    );
}
