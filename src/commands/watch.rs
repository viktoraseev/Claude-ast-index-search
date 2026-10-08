//! Watch mode — automatically update index on file changes

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use anyhow::Result;
use colored::Colorize;
use notify::RecursiveMode;
use notify_debouncer_mini::new_debouncer;

use crate::commands::{self, management::ScopedEnvVar};
use crate::{db, indexer, minified, parsers};

fn open_watch_lock(root: &Path) -> Result<std::fs::File> {
    let lock_path = db::get_db_path(root)?.with_extension("watch.lock");
    if let Some(parent) = lock_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(Into::into)
}

/// Acquire an exclusive lock for watch mode, scoped to the resolved project
/// database. The lock remains held until the returned file is dropped.
fn try_acquire_watch_lock(root: &Path) -> Result<Option<std::fs::File>> {
    use fs2::FileExt;
    let file = open_watch_lock(root)?;
    match file.try_lock_exclusive() {
        Ok(()) => {
            file.set_len(0)?;
            let mut f = &file;
            write!(f, "{}", std::process::id())?;
            Ok(Some(file))
        }
        Err(error) if db::lock_is_contended(&error) => Ok(None),
        Err(error) => Err(error.into()),
    }
}

/// Report whether this project's watch lock is currently held. This probes
/// the same lock as [`cmd_watch`], so another project's watcher cannot affect
/// the result.
fn is_watch_running(root: &Path) -> Result<bool> {
    use fs2::FileExt;
    let file = open_watch_lock(root)?;
    match file.try_lock_exclusive() {
        Ok(()) => Ok(false),
        Err(error) if db::lock_is_contended(&error) => Ok(true),
        Err(error) => Err(error.into()),
    }
}

/// Feed bounded notifications to the real watch loop for a disposable index.
/// This never overrides the normal provider without an explicit adjacent DB.
fn test_watch_events(
    root: &Path,
) -> Result<Option<Vec<notify_debouncer_mini::DebounceEventResult>>> {
    use std::io::Read;
    use std::path::{Component, PathBuf};
    let Some(control) = std::env::var_os("AST_INDEX_TEST_WATCH_EVENTS_FILE") else {
        return Ok(None);
    };
    let database = std::env::var_os("AST_INDEX_DB_PATH")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow::anyhow!("watch event control requires an explicit database"))?;
    let control = PathBuf::from(control);
    let parent = database
        .parent()
        .filter(|p| p.is_absolute())
        .ok_or_else(|| anyhow::anyhow!("watch event database must be absolute"))?;
    anyhow::ensure!(
        control.parent() == Some(parent) && root.starts_with(parent) && root != parent,
        "watch event control and disposable root must be beside the explicit database"
    );
    let mut bytes = Vec::new();
    std::fs::File::open(&control)?
        .take(65537)
        .read_to_end(&mut bytes)?;
    anyhow::ensure!(bytes.len() <= 65536, "watch event control exceeds budget");
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Events {
        database: PathBuf,
        root: PathBuf,
        mode: String,
        paths: Vec<PathBuf>,
    }
    let input: Events = serde_json::from_slice(&bytes)?;
    anyhow::ensure!(
        input.database == database && input.root == root && input.paths.len() <= 128,
        "watch event control identity or path budget is invalid"
    );
    anyhow::ensure!(
        input.paths.iter().all(|p| p
            .components()
            .all(|c| matches!(c, Component::Normal(_) | Component::CurDir))),
        "watch event path escaped root"
    );
    let events = match input.mode.as_str() {
        "disconnect" => Vec::new(),
        "backend-error" => vec![Err(notify::Error::generic(
            "fixture notification backend failed",
        ))],
        "events" => vec![Ok(input
            .paths
            .into_iter()
            .map(|path| notify_debouncer_mini::DebouncedEvent {
                path: root.join(path),
                kind: notify_debouncer_mini::DebouncedEventKind::Any,
            })
            .collect())],
        _ => anyhow::bail!("invalid watch event control mode"),
    };
    Ok(Some(events))
}

/// Check whether a notification can change the indexed source/module state.
/// Mini-debouncer notifications do not retain rename or removed-path kinds,
/// so a missing path may be a whole removed directory, even with a suffix.
fn event_needs_update(root: &Path, path: &Path) -> bool {
    let Ok(relative) = path.strip_prefix(root) else {
        return false;
    };
    if relative.components().any(|component| {
        if matches!(component, std::path::Component::ParentDir) {
            return true;
        }
        let name = component.as_os_str().to_str().unwrap_or("");
        indexer::EXCLUDED_DIRS.contains(&name) || name == ".git"
    }) {
        return false;
    }
    if path.is_dir() || !path.exists() {
        return true;
    }
    if path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            indexer::is_module_file(name)
                || matches!(
                    name,
                    ".ast-index.yaml" | ".ast-index.yml" | ".gitignore" | ".arcignore" | ".ignore"
                )
        })
    {
        return true;
    }
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(parsers::is_supported_extension)
        && !minified::skip_by_name(path)
}

/// Check registration and availability without walking source trees while idle.
fn watch_roots(root: &Path) -> Result<BTreeMap<PathBuf, bool>> {
    let conn = db::open_existing_db_leased(root)?
        .ok_or_else(|| anyhow::anyhow!("Index was cleared; run 'ast-index rebuild' first."))?;
    let mut paths = db::get_extra_roots(&conn)?;
    paths.push(root.to_string_lossy().into_owned());
    Ok(paths
        .into_iter()
        .map(|path| {
            let path = PathBuf::from(path);
            let available = path.is_dir();
            (path, available)
        })
        .collect())
}

/// Register each available owner once, including owners restored after removal.
fn sync_watch_roots(
    debouncer: &mut notify_debouncer_mini::Debouncer<notify::RecommendedWatcher>,
    previous: &BTreeMap<PathBuf, bool>,
    current: &BTreeMap<PathBuf, bool>,
) -> Result<()> {
    for (path, available) in previous {
        if *available && current.get(path) != Some(&true) {
            match debouncer.watcher().unwatch(path) {
                Ok(()) => {}
                Err(error) if matches!(error.kind, notify::ErrorKind::WatchNotFound) => {}
                Err(error) => return Err(error.into()),
            }
        }
    }
    for (path, available) in current {
        if *available && previous.get(path) != Some(&true) {
            debouncer.watcher().watch(path, RecursiveMode::Recursive)?;
        }
    }
    Ok(())
}

/// Pair registration/availability transitions with an index reconciliation.
fn refresh_watch_roots(
    provider: &mut notify_debouncer_mini::Debouncer<notify::RecommendedWatcher>,
    roots: &mut BTreeMap<PathBuf, bool>,
    current: BTreeMap<PathBuf, bool>,
    reconciliation_pending: &mut bool,
) -> Result<()> {
    if current != *roots {
        sync_watch_roots(provider, roots, &current)?;
        *roots = current;
        *reconciliation_pending = true;
    }
    Ok(())
}

/// Print a stable watcher status. Callers that only need the exit status use
/// `--quiet`; the CLI exits successfully only while this project is watched.
pub fn cmd_watch_status(root: &Path, quiet: bool, format: &str) -> Result<bool> {
    let watching = is_watch_running(root)?;
    if !quiet {
        if format == "json" {
            println!(r#"{{"watching":{watching}}}"#);
        } else if watching {
            println!("watching");
        } else {
            println!("not-watching");
        }
        std::io::stdout().flush()?;
    }
    Ok(watching)
}

/// Watch for file changes and incrementally update the index
pub fn cmd_watch(root: &Path) -> Result<()> {
    let json = super::management::lifecycle_json();
    // Held for the complete watch loop. The lease file lives outside the
    // project cache directory, so stale-cache GC cannot unlink this index
    // while the watcher is idle between SQLite connections.
    let _cache_lease = db::acquire_project_lease(root)?;
    let Some(initial) = db::open_existing_db_leased(root)? else {
        super::index_available(root, if json { "json" } else { "text" })?;
        return Ok(());
    };
    drop(initial);

    // Ensure only one watch process runs at a time
    let _lock = match try_acquire_watch_lock(root)? {
        Some(lock) => lock,
        None => {
            eprintln!("{}", "Another ast-index watch is already running.".yellow());
            if json {
                println!(
                    "{}",
                    serde_json::json!({"command": "watch", "status": "already-running"})
                );
            }
            return Ok(());
        }
    };

    let (tx, rx) = mpsc::channel();

    let injected = test_watch_events(root)?;
    let mut debouncer = new_debouncer(Duration::from_millis(500), tx.clone())?;
    let mut roots = watch_roots(root)?;
    sync_watch_roots(&mut debouncer, &BTreeMap::new(), &roots)?;
    if json {
        println!(
            "{}",
            serde_json::json!({"command": "watch", "status": "watching", "root": root})
        );
    } else {
        println!(
            "{}",
            format!("Watching for changes in {}...", root.display()).cyan()
        );
        println!("{}", "Press Ctrl+C to stop.".dimmed());
    }
    std::io::stdout().flush()?;

    let mut debouncer = if let Some(events) = injected {
        for event in events {
            tx.send(event)?;
        }
        drop(debouncer);
        None
    } else {
        Some(debouncer)
    };
    drop(tx);

    let mut reconciliation_pending = false;
    loop {
        let notification = rx.recv_timeout(Duration::from_millis(500));
        // Registrations live in the index, which can be outside all watched
        // trees. Poll only that small list and directory availability; source
        // traversal still happens only for an event or an actual scope change.
        if let Some(ref mut provider) = debouncer {
            match watch_roots(root) {
                Ok(current) => {
                    refresh_watch_roots(
                        provider,
                        &mut roots,
                        current,
                        &mut reconciliation_pending,
                    )?;
                }
                Err(error) => eprintln!("Update error: {}", error),
            }
        }
        match notification {
            Ok(Ok(events)) => {
                let changed: Vec<_> = events
                    .iter()
                    .filter(|e| roots.keys().any(|owner| event_needs_update(owner, &e.path)))
                    .collect();

                if changed.is_empty() && !reconciliation_pending {
                    continue;
                }

                let start = Instant::now();
                let file_count = changed.len();
                eprintln!(
                    "{}",
                    format!("Detected {} changed file(s), updating...", file_count).yellow()
                );

                match update_index(root) {
                    Ok((updated, deleted)) => {
                        reconciliation_pending = false;
                        if json {
                            println!(
                                "{}",
                                serde_json::json!({
                                    "command": "watch", "status": "updated",
                                    "updated": updated, "deleted": deleted
                                })
                            );
                            std::io::stdout().flush()?;
                        }
                        if updated > 0 || deleted > 0 {
                            eprintln!(
                                "{}",
                                format!(
                                    "Updated {} files, deleted {} ({:?})",
                                    updated,
                                    deleted,
                                    start.elapsed()
                                )
                                .green()
                            );
                        } else {
                            eprintln!(
                                "{}",
                                format!("No index changes ({:?})", start.elapsed()).dimmed()
                            );
                        }
                    }
                    Err(e) => {
                        reconciliation_pending = true;
                        eprintln!("{}", format!("Update error: {}", e).red());
                    }
                }
            }
            Ok(Err(err)) => {
                // A failed provider can no longer promise notification
                // delivery. Release the watcher lock and let callers retry.
                return Err(anyhow::anyhow!("Watch error: {}", err));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if reconciliation_pending {
                    reconciliation_pending = !report_scope_update(root, json)?;
                }
            }
            Err(e) => {
                return Err(anyhow::anyhow!("Channel error: {}", e));
            }
        }
        // Config or availability can change during the update itself. Register
        // and reconcile that transition even when native notifications miss it.
        if let Some(ref mut provider) = debouncer {
            if let Ok(current) = watch_roots(root) {
                refresh_watch_roots(provider, &mut roots, current, &mut reconciliation_pending)?;
            }
        }
    }
}

fn report_scope_update(root: &Path, json: bool) -> Result<bool> {
    match update_index(root) {
        Ok((updated, deleted)) => {
            if json {
                println!(
                    "{}",
                    serde_json::json!({"command": "watch", "status": "updated",
                    "updated": updated, "deleted": deleted})
                );
                std::io::stdout().flush()?;
            }
            Ok(true)
        }
        Err(error) => {
            eprintln!("Update error: {}", error);
            Ok(false)
        }
    }
}

fn update_index(root: &Path) -> Result<(usize, usize)> {
    // Watch is long-lived, so take the common mutation lock only for one
    // coalesced update batch. Readers remain concurrent through SQLite WAL.
    let _mutation_guard = db::acquire_rebuild_guard(root)?;
    let _experimental_fast_rebuild_env = ScopedEnvVar::set_bool(
        "AST_INDEX_EXPERIMENTAL_FAST_REBUILD",
        commands::try_is_experimental_fast_rebuild_enabled(root)?,
    );

    let mut conn = db::open_existing_db_leased(root)?
        .ok_or_else(|| anyhow::anyhow!("Index was cleared; run 'ast-index rebuild' first."))?;

    // Honour .ast-index.yaml so watch stays scoped to the same paths as rebuild/update.
    let config = indexer::load_config(root).unwrap_or_default();
    // Config attachment follows rebuild semantics: it adds registrations;
    // explicit subtree/remove-root commands own registration removal.
    let mut registered: std::collections::HashSet<_> =
        db::get_extra_roots(&conn)?.into_iter().collect();
    for raw in config.roots.as_deref().unwrap_or_default() {
        let path = Path::new(raw);
        let path = if path.is_absolute() {
            path.to_path_buf()
        } else {
            root.join(path)
        };
        let canonical = db::normalize_root_for_storage(&path);
        if registered.insert(canonical.clone()) {
            let name = db::allocate_subtree_name(&conn, &db::default_subtree_name(&canonical))?;
            db::insert_subtree(&conn, &name, &canonical, raw)?;
        }
    }
    let config_include = config.include.as_deref();
    let exclude_matcher: Option<ignore::gitignore::Gitignore> = config
        .exclude
        .as_deref()
        .filter(|p| !p.is_empty())
        .map(|patterns| {
            let mut gb = ignore::gitignore::GitignoreBuilder::new(root);
            for p in patterns {
                gb.add_line(None, p).ok();
            }
            gb.build().ok()
        })
        .flatten();

    let (updated, changed, deleted) = indexer::update_directory_incremental(
        &mut conn,
        root,
        false,
        config_include,
        exclude_matcher.as_ref(),
    )?;
    let _ = changed; // suppress unused
    Ok((updated, deleted))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn java_root_return_after_update_is_reconciled_without_notifications() {
        let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
        std::fs::create_dir_all(&artifacts).unwrap();
        let fixture = tempfile::tempdir_in(artifacts).unwrap();
        let root = fixture.path().join("project");
        let extra = fixture.path().join("build");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&extra).unwrap();
        std::fs::write(root.join("Left.java"), "class WatchScopeLeft {}\n").unwrap();
        std::fs::write(extra.join("Probe.java"), "class WatchScopeAttached {}\n").unwrap();
        let mut conn = rusqlite::Connection::open(fixture.path().join("index.sqlite")).unwrap();
        db::init_db(&conn).unwrap();
        // The disposable sources are under this checkout's ignored .artifacts.
        conn.execute("INSERT INTO metadata VALUES ('no_ignore', '1')", [])
            .unwrap();
        db::add_extra_root(&conn, &extra.to_string_lossy()).unwrap();
        indexer::update_directory_incremental(&mut conn, &root, false, None, None).unwrap();
        assert_eq!(
            db::search_symbols(&conn, "WatchScope*", 100).unwrap().len(),
            2
        );

        // Complete the unavailable-root scan first. Recreate the directory
        // before the bottom-of-loop poll, without depending on OS event timing.
        std::fs::remove_dir_all(&extra).unwrap();
        indexer::update_directory_incremental(&mut conn, &root, false, None, None).unwrap();
        let mut roots = BTreeMap::from([(root.clone(), true), (extra.clone(), false)]);
        let (tx, _rx) = mpsc::channel();
        let mut provider = new_debouncer(Duration::from_millis(500), tx).unwrap();
        sync_watch_roots(&mut provider, &BTreeMap::new(), &roots).unwrap();
        std::fs::create_dir(&extra).unwrap();
        std::fs::write(extra.join("Probe.java"), "class WatchScopeReturned {}\n").unwrap();
        let mut pending = false;
        let current = BTreeMap::from([(root.clone(), true), (extra.clone(), true)]);
        refresh_watch_roots(&mut provider, &mut roots, current.clone(), &mut pending).unwrap();
        // The next idle iteration has no notification. It must still scan.
        if pending {
            indexer::update_directory_incremental(&mut conn, &root, false, None, None).unwrap();
            pending = false;
        }
        let mut names: Vec<_> = db::search_symbols(&conn, "WatchScope*", 100)
            .unwrap()
            .into_iter()
            .map(|symbol| symbol.name)
            .collect();
        names.sort();
        assert_eq!(names, ["WatchScopeLeft", "WatchScopeReturned"]);

        // Idle polls must not schedule repeated walks, or clear a failed update.
        refresh_watch_roots(&mut provider, &mut roots, current.clone(), &mut pending).unwrap();
        assert!(!pending);
        pending = true;
        refresh_watch_roots(&mut provider, &mut roots, current, &mut pending).unwrap();
        assert!(pending);
    }

    #[test]
    fn java_watch_events_include_directory_tombstones_and_module_descriptors() {
        let artifacts = Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
        std::fs::create_dir_all(&artifacts).unwrap();
        let fixture = tempfile::tempdir_in(artifacts).unwrap();
        let root = fixture.path().join("build");
        std::fs::create_dir_all(root.join("src.dotted")).unwrap();
        let java = root.join("src.dotted/Probe.java");
        std::fs::write(&java, "class Probe {}\n").unwrap();
        assert!(event_needs_update(&root, &java));
        assert!(event_needs_update(&root, &root.join("src.dotted")));
        std::fs::remove_file(&java).unwrap();
        std::fs::remove_dir(root.join("src.dotted")).unwrap();
        assert!(event_needs_update(&root, &root.join("src.dotted")));
        for name in ["build.gradle", "build.gradle.kts", "pom.xml", "ya.make"] {
            let path = root.join(name);
            std::fs::write(&path, "fixture descriptor").unwrap();
            assert!(event_needs_update(&root, &path), "{name}");
        }
        std::fs::write(root.join("notes.txt"), "unrelated").unwrap();
        assert!(!event_needs_update(&root, &root.join("notes.txt")));
        assert!(!event_needs_update(&root, &root.join("target/Probe.java")));
        assert!(!event_needs_update(&root, &root.join(".git/Probe.java")));
        assert!(!event_needs_update(&root, &root.join("../outside.java")));
        assert!(!event_needs_update(
            &root,
            &fixture.path().join("outside.java")
        ));
    }
}
