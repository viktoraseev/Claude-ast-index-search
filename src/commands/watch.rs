//! Watch mode — automatically update index on file changes

use std::io::Write;
use std::path::Path;
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
        .is_some_and(indexer::is_module_file)
    {
        return true;
    }
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(parsers::is_supported_extension)
        && !minified::skip_by_name(path)
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
    debouncer.watcher().watch(root, RecursiveMode::Recursive)?;
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

    let _debouncer = if let Some(events) = injected {
        for event in events {
            tx.send(event)?;
        }
        drop(debouncer);
        None
    } else {
        Some(debouncer)
    };
    drop(tx);

    loop {
        match rx.recv() {
            Ok(Ok(events)) => {
                let changed: Vec<_> = events
                    .iter()
                    .filter(|e| event_needs_update(root, &e.path))
                    .collect();

                if changed.is_empty() {
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
                        eprintln!("{}", format!("Update error: {}", e).red());
                    }
                }
            }
            Ok(Err(err)) => {
                // A failed provider can no longer promise notification
                // delivery. Release the watcher lock and let callers retry.
                return Err(anyhow::anyhow!("Watch error: {}", err));
            }
            Err(e) => {
                return Err(anyhow::anyhow!("Channel error: {}", e));
            }
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
