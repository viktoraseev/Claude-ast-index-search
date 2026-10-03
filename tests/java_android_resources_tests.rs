use ast_index::{db, indexer};
use rusqlite::Connection;
use std::fs;
use std::path::Path;

fn write(root: &Path, path: &str, content: &str) {
    let file = root.join(path);
    fs::create_dir_all(file.parent().unwrap()).unwrap();
    fs::write(file, content).unwrap();
}

#[test]
fn java_resource_ownership_preserves_modules_variants_and_unassigned_layouts() {
    let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let temporary = tempfile::tempdir_in(artifacts).unwrap();
    let root = temporary.path().join("project");
    fs::create_dir_all(root.join(".git")).unwrap();
    for module in ["app", "library"] {
        write(&root, &format!("{module}/build.gradle"), "");
        write(
            &root,
            &format!("{module}/src/main/res/values/strings.xml"),
            r#"<resources><string name="shared">Hello</string></resources>"#,
        );
    }
    write(
        &root,
        "app/src/main/res/values-fr/strings.xml",
        r#"<resources><string name="shared">Bonjour</string></resources>"#,
    );
    write(
        &root,
        "app/src/main/java/Use.java",
        "class Use { int value = R.string.shared; }",
    );
    write(
        &root,
        "app_extra/src/main/res/layout/screen.xml",
        "<fixture.Widget />",
    );
    let mut conn = Connection::open(temporary.path().join("index.sqlite")).unwrap();
    db::init_db(&conn).unwrap();
    let walk =
        indexer::index_directory_scoped(&mut conn, &root, &root, false, false, None).unwrap();
    indexer::sync_modules_from_files(&conn, &root, &walk.module_files).unwrap();
    indexer::index_xml_usages(&mut conn, &root, &walk.xml_layout_files, false).unwrap();
    indexer::index_resources(&mut conn, &root, &walk.res_files, false).unwrap();

    let mut query = conn
        .prepare(
            "SELECT m.name, count(*) FROM resource_usages u
             JOIN resources r ON r.id=u.resource_id
             JOIN modules m ON m.id=r.module_id GROUP BY m.name",
        )
        .unwrap();
    let used: Vec<(String, i64)> = query
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(used, [("app".into(), 1)]);
    let unassigned: i64 = conn
        .query_row(
            "SELECT count(*) FROM xml_usages WHERE module_id IS NULL AND class_name='fixture.Widget'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(unassigned, 1);
    let variants: i64 = conn
        .query_row(
            "SELECT count(*) FROM resources r JOIN modules m ON r.module_id=m.id
             WHERE m.name='app' AND r.type='string' AND r.name='shared'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(variants, 2);
}
