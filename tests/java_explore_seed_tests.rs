//! Internal candidate contracts on authored Java; not MCP equivalence.
use ast_index::{db, indexer};
use rusqlite::Connection;
use std::fs;

#[test]
fn every_seed_pool_filters_before_its_budget_and_keeps_the_cap() {
    let artifacts = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
    fs::create_dir_all(&artifacts).unwrap();
    let project = tempfile::tempdir_in(artifacts).unwrap();
    let mut conn = Connection::open_in_memory().unwrap();
    db::init_db(&conn).unwrap();
    for (directory, count) in [("noise/pulse_seed", 220), ("selected/pulse_seed", 5)] {
        let root = project.path().join(directory);
        fs::create_dir_all(&root).unwrap();
        for n in 0..count {
            fs::write(
                root.join(format!("Pulse{n:03}.java")),
                format!("class Pulse{n:03} {{}}\n"),
            )
            .unwrap();
        }
    }
    indexer::index_directory(&mut conn, project.path(), false, false).unwrap();
    let scope = db::SearchScope {
        in_file: Some("Pulse"),
        module: Some("selected/"),
        dir_prefix: Some("selected/"),
    };
    for limit in [0, 1, 40, 200] {
        let pools = [
            db::search_symbol_seeds_scoped(&conn, "pulse*", limit, &scope).unwrap(),
            db::search_symbol_seeds_ranked_scoped(&conn, &["pulse".into()], limit, &scope).unwrap(),
            db::search_symbols_scoped(&conn, "pulse*", limit, &scope).unwrap(),
            db::search_symbols_fuzzy_scoped(&conn, "ulse", limit, &scope).unwrap(),
            db::search_symbols_in_matching_paths_scoped(&conn, &["pulse".into()], 1, limit, &scope)
                .unwrap(),
        ];
        for pool in pools {
            assert_eq!(pool.len(), limit.min(5));
            assert!(pool
                .iter()
                .all(|row| row.path.starts_with("selected/") && row.name.starts_with("Pulse")));
        }
    }
    let empty = db::SearchScope {
        in_file: Some("missing"),
        ..scope
    };
    assert!(db::search_symbol_seeds_scoped(&conn, "pulse*", 40, &empty)
        .unwrap()
        .is_empty());
    assert!(
        db::search_symbol_seeds_ranked_scoped(&conn, &["pulse".into()], 200, &empty)
            .unwrap()
            .is_empty()
    );
}
