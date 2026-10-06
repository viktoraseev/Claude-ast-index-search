//! Java scan errors must survive every shared search adapter.
use ast_index::commands::{
    project_source_files, search_files_filtered, search_files_in, search_files_limited,
    search_files_limited_each, search_files_page_in,
};
use std::fs;

#[test]
fn java_scan_adapters_preserve_read_and_walk_errors() {
    let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&base).unwrap();
    let fixture = tempfile::tempdir_in(base).unwrap();
    let root = fixture.path().join("project");
    fs::create_dir(&root).unwrap();
    std::env::set_var("AST_INDEX_CACHE_DIR", fixture.path().join("cache"));
    std::env::remove_var("AST_INDEX_DB_PATH");
    std::env::remove_var("KOTLIN_INDEX_DB_PATH");
    let source = root.join("Probe.java");
    fs::write(&source, b"// TODO probe \xff\n").unwrap();
    let roots = vec![root.clone()];
    assert!(search_files_in(&root, &roots, "TODO", &["java"], |_, _, _| {}).is_err());
    assert!(search_files_limited(&root, "TODO", &["java"], 100, |_, _, _| {}).is_err());
    assert!(
        search_files_page_in(&root, &roots, "TODO", &["java"], 0, |_, _, line| Some(
            line.to_owned()
        ))
        .is_err()
    );
    assert!(
        search_files_filtered(&root, "TODO", &["java"], 100, |_, _| true, |_, _, _| {}).is_err()
    );
    let files = vec![source.clone()];
    let patterns = vec![("TODO".to_owned(), "TODO".to_owned())];
    assert!(search_files_limited_each(
        &files,
        "TODO",
        &patterns,
        100,
        |_, _, _| true,
        |_, _, _, _| {}
    )
    .is_err());

    // Explicit empty pages in bounded scans perform no source read. Exact
    // total adapters above still read even when their returned page is empty.
    search_files_limited(&root, "TODO", &["java"], 0, |_, _, _| {}).unwrap();
    search_files_filtered(&root, "TODO", &["java"], 0, |_, _| true, |_, _, _| {}).unwrap();
    search_files_limited_each(
        &files,
        "TODO",
        &patterns,
        0,
        |_, _, _| true,
        |_, _, _, _| {},
    )
    .unwrap();

    fs::remove_file(&source).unwrap();
    assert!(search_files_limited_each(
        &files,
        "TODO",
        &patterns,
        100,
        |_, _, _| true,
        |_, _, _, _| {}
    )
    .is_err());
    let missing = root.join("missing");
    assert!(search_files_in(&root, &[missing], "TODO", &["java"], |_, _, _| {}).is_err());
    // A directory named like a Java file is traversed, never searched as a
    // source. Its contained sources remain visible.
    let directory = root.join("folder.java");
    fs::create_dir(&directory).unwrap();
    fs::write(directory.join("Child.java"), "// TODO child\n").unwrap();
    assert_eq!(
        project_source_files(&root, &["java"]).unwrap(),
        vec![directory.join("Child.java")]
    );
    let mut hits = Vec::new();
    search_files_limited(&root, "TODO", &["java"], 100, |_, _, line| {
        hits.push(line.to_owned())
    })
    .unwrap();
    assert_eq!(hits, ["// TODO child"]);
    fs::remove_dir_all(&root).unwrap();
    assert!(project_source_files(&root, &["java"]).is_err());
}
