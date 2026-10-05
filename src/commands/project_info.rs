//! Project insight commands
//!
//! - map: Compact project map (key types per directory)
//! - conventions: Auto-detect project conventions (architecture, frameworks, naming)

use std::collections::HashMap;
use std::path::Path;

use anyhow::Result;
use colored::Colorize;
use serde::Serialize;

use crate::db;

// ── map ──────────────────────────────────────────────────────────────

// --- Summary mode structs (default, no --module) ---

#[derive(Debug, Serialize)]
struct MapSummaryOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    project: Option<String>,
    file_count: i64,
    module_count: i64,
    showing: usize,
    total_dirs: usize,
    groups: Vec<SummaryGroup>,
}

#[derive(Debug, Serialize)]
struct SummaryGroup {
    path: String,
    file_count: i64,
    #[serde(skip_serializing_if = "HashMap::is_empty")]
    kinds: HashMap<String, i64>,
}

// --- Detailed mode structs (with --module) ---

#[derive(Debug, Serialize)]
struct MapDetailOutput {
    #[serde(skip_serializing_if = "Option::is_none")]
    project: Option<String>,
    file_count: i64,
    module_count: i64,
    groups: Vec<DetailGroup>,
}

#[derive(Debug, Serialize)]
struct DetailGroup {
    path: String,
    file_count: i64,
    symbols: Vec<MapSymbol>,
}

#[derive(Debug, Serialize)]
struct MapSymbol {
    name: String,
    kind: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    parents: Vec<String>,
    file: String,
}

/// Kind priority for sorting: lower = more important
fn kind_priority(kind: &str) -> u8 {
    match kind {
        "class" => 0,
        "interface" | "protocol" | "trait" => 1,
        "struct" => 2,
        "enum" => 3,
        "object" => 4,
        "actor" => 5,
        _ => 10,
    }
}

/// Short display label for kind
fn kind_label(kind: &str) -> &str {
    match kind {
        "class" => "cls",
        "interface" => "iface",
        "protocol" => "proto",
        "trait" => "trait",
        "struct" => "struct",
        "enum" => "enum",
        "object" => "obj",
        "actor" => "actor",
        "package" => "pkg",
        _ => kind,
    }
}

/// Truncate path to first N segments
fn dir_prefix(path: &str, depth: usize) -> String {
    let parts: Vec<&str> = path.split('/').collect();
    if parts.len() <= depth + 1 {
        parts[..parts.len().saturating_sub(1)].join("/")
    } else {
        parts[..depth].join("/")
    }
}

pub fn cmd_map(
    root: &Path,
    module: Option<&str>,
    per_dir: usize,
    limit: usize,
    format: &str,
) -> Result<()> {
    cmd_map_scoped(
        root,
        module,
        per_dir,
        limit,
        format,
        &db::SearchScope::none(),
    )
}

pub fn cmd_map_scoped(
    root: &Path,
    module: Option<&str>,
    per_dir: usize,
    limit: usize,
    format: &str,
    scope: &db::SearchScope,
) -> Result<()> {
    if !super::index_available(root, format)? {
        return Ok(());
    }

    let conn = db::open_db_leased(root)?;
    let mut stats = db::get_stats(&conn)?;
    stats.file_count = 0;
    db::visit_insight_files_scoped(&conn, scope, |_, _| {
        stats.file_count += 1;
        Ok(())
    })?;
    let resolver = super::PathResolver::try_from_conn(root, &conn)?;
    stats.module_count = count_map_modules(&conn, root, scope, &resolver)?;

    let depth = if stats.file_count > 5000 { 3 } else { 2 };

    let project = db::get_metadata_value(&conn, crate::indexer::PROJECT_LABEL_KEY)?;

    if module.is_some() {
        cmd_map_detailed(
            &conn,
            &stats,
            project.as_deref(),
            &db::SearchScope { module, ..*scope },
            &resolver,
            per_dir,
            limit,
            depth,
            format,
        )?;
    } else {
        cmd_map_summary(
            &conn,
            &stats,
            project.as_deref(),
            scope,
            &resolver,
            limit,
            depth,
            format,
        )?;
    }

    Ok(())
}

/// Count module declarations in the header's directory/root context, before
/// the detailed display selector or page limits. Explicit owners keep colliding
/// module directories distinct; legacy absolute paths remain readable.
fn count_map_modules(
    conn: &rusqlite::Connection,
    root: &Path,
    scope: &db::SearchScope,
    resolver: &super::PathResolver,
) -> Result<i64> {
    let primary = std::path::PathBuf::from(db::normalize_root_for_storage(root));
    let mut stmt = conn.prepare("SELECT path,root_path FROM modules")?;
    let mut rows = stmt.query([])?;
    let mut count = 0;
    while let Some(row) = rows.next()? {
        let stored: String = row.get(0)?;
        let owner: String = row.get(1)?;
        let absolute = if owner.is_empty() {
            primary.join(stored)
        } else {
            Path::new(&owner).join(stored)
        };
        if let Some(relative) = resolver.scoped_relative_path(&absolute) {
            // A directory selector ending in '/' must include a declaration
            // at that directory, while retaining literal case and wildcards.
            let directory = if relative.is_empty() {
                relative
            } else {
                format!("{}/", relative.trim_end_matches('/'))
            };
            if scope.matches_path(&directory) {
                count += 1;
            }
        }
    }
    Ok(count)
}

/// `Project: <label> | ` for the header of `map`, empty for an index built
/// before the label was recorded.
fn project_prefix(project: Option<&str>) -> String {
    project.map_or_else(String::new, |label| format!("Project: {label} | "))
}

/// Group within the owning root before rendering, so colliding relative paths
/// never merge. Apply directory depth to the relative path, not an absolute root.
fn map_directory(path: &str, owner: &str, depth: usize, resolver: &super::PathResolver) -> String {
    let dir = dir_prefix(path, depth);
    let raw = resolver.resolve_with_root_raw(&dir, Some(owner));
    if raw.is_empty() {
        ".".to_string()
    } else {
        format!("{}/", raw.trim_end_matches('/'))
    }
}

/// Summary mode: directories + file counts + kind counts, sorted by file_count desc
#[allow(clippy::too_many_arguments)]
fn cmd_map_summary(
    conn: &rusqlite::Connection,
    stats: &db::DbStats,
    project: Option<&str>,
    scope: &db::SearchScope,
    resolver: &super::PathResolver,
    limit: usize,
    depth: usize,
    format: &str,
) -> Result<()> {
    let mut dir_file_counts: HashMap<String, i64> = HashMap::new();
    db::visit_insight_files_scoped(conn, scope, |path, owner| {
        *dir_file_counts
            .entry(map_directory(path, owner, depth, resolver))
            .or_insert(0) += 1;
        Ok(())
    })?;
    let mut dir_kind_counts: HashMap<String, HashMap<String, i64>> = HashMap::new();
    db::visit_project_map_symbols_scoped(conn, scope, |sym| {
        *dir_kind_counts
            .entry(map_directory(&sym.path, &sym.root_path, depth, resolver))
            .or_default()
            .entry(sym.kind)
            .or_insert(0) += 1;
        Ok(())
    })?;

    // Build groups, sort by file_count desc
    let mut groups: Vec<SummaryGroup> = dir_file_counts
        .into_iter()
        .map(|(dir, fc)| {
            let kinds = dir_kind_counts.remove(&dir).unwrap_or_default();
            SummaryGroup {
                path: dir,
                file_count: fc,
                kinds,
            }
        })
        .collect();

    groups.sort_by(|a, b| b.file_count.cmp(&a.file_count).then(a.path.cmp(&b.path)));
    let total_dirs = groups.len();
    groups.truncate(limit);

    if format == "json" {
        let output = MapSummaryOutput {
            project: project.map(str::to_string),
            file_count: stats.file_count,
            module_count: stats.module_count,
            showing: groups.len(),
            total_dirs,
            groups,
        };
        println!("{}", serde_json::to_string_pretty(&output)?);
        return Ok(());
    }

    // Text output
    println!(
        "{}",
        format!(
            "{}{} files | {} modules | top {} of {} dirs",
            project_prefix(project),
            stats.file_count,
            stats.module_count,
            groups.len(),
            total_dirs
        )
        .bold()
    );
    println!();

    for g in &groups {
        // Build compact kind summary: "12 cls, 3 iface, 2 enum"
        let mut kind_pairs: Vec<(&str, i64)> =
            g.kinds.iter().map(|(k, &v)| (k.as_str(), v)).collect();
        kind_pairs.sort_by(|a, b| {
            kind_priority(a.0)
                .cmp(&kind_priority(b.0))
                .then(a.0.cmp(b.0))
        });

        let kinds_str = if kind_pairs.is_empty() {
            String::new()
        } else {
            let items: Vec<String> = kind_pairs
                .iter()
                .map(|(k, v)| format!("{} {}", v, kind_label(k)))
                .collect();
            format!(" | {}", items.join(", "))
        };

        println!(
            "  {:60} {:>5} files{}",
            g.path.cyan(),
            g.file_count,
            kinds_str,
        );
    }

    if total_dirs > groups.len() {
        println!(
            "\n{}",
            format!(
                "  ... and {} more dirs. Use --limit or --module <path> to drill down.",
                total_dirs - groups.len()
            )
            .dimmed()
        );
    }

    Ok(())
}

/// Detailed mode: symbols with inheritance per directory (when --module is used)
#[allow(clippy::too_many_arguments)]
fn cmd_map_detailed(
    conn: &rusqlite::Connection,
    stats: &db::DbStats,
    project: Option<&str>,
    scope: &db::SearchScope,
    resolver: &super::PathResolver,
    per_dir: usize,
    limit: usize,
    depth: usize,
    format: &str,
) -> Result<()> {
    // Stream declarations in presentation order, retaining only a bounded
    // per-directory slice. Parents are loaded only for the final page.
    let mut groups_map: HashMap<String, Vec<db::ProjectMapSymbol>> = HashMap::new();
    db::visit_project_map_symbols_scoped(conn, scope, |sym| {
        let dir = map_directory(&sym.path, &sym.root_path, depth, resolver);
        let group = groups_map.entry(dir).or_default();
        if group.len() < per_dir {
            group.push(sym);
        }
        Ok(())
    })?;
    let mut dir_file_counts: HashMap<String, i64> = HashMap::new();
    db::visit_insight_files_scoped(conn, scope, |path, owner| {
        *dir_file_counts
            .entry(map_directory(path, owner, depth, resolver))
            .or_insert(0) += 1;
        Ok(())
    })?;

    // Build groups sorted by file_count desc, apply limit
    let mut dir_keys: Vec<String> = groups_map.keys().cloned().collect();
    dir_keys.sort_by(|a, b| {
        let fa = dir_file_counts.get(a).copied().unwrap_or(0);
        let fb = dir_file_counts.get(b).copied().unwrap_or(0);
        fb.cmp(&fa).then(a.cmp(b))
    });
    dir_keys.truncate(limit);

    let mut groups: Vec<DetailGroup> = Vec::new();
    for dir in &dir_keys {
        let mut map_syms = Vec::new();
        for s in &groups_map[dir] {
            map_syms.push(MapSymbol {
                name: s.name.clone(),
                kind: s.kind.clone(),
                parents: db::project_map_parents(conn, s.id)?,
                file: s.path.rsplit('/').next().unwrap_or(&s.path).to_string(),
            });
        }

        let fc = dir_file_counts.get(dir).copied().unwrap_or(0);
        groups.push(DetailGroup {
            path: dir.clone(),
            file_count: fc,
            symbols: map_syms,
        });
    }

    if format == "json" {
        let output = MapDetailOutput {
            project: project.map(str::to_string),
            file_count: stats.file_count,
            module_count: stats.module_count,
            groups,
        };
        println!("{}", serde_json::to_string_pretty(&output)?);
        return Ok(());
    }

    // Text output
    println!(
        "{}",
        format!(
            "{}{} files | {} modules",
            project_prefix(project),
            stats.file_count,
            stats.module_count
        )
        .bold()
    );
    println!();

    for g in &groups {
        if g.symbols.is_empty() {
            continue;
        }
        println!("{} ({} files)", g.path.cyan(), g.file_count);
        for s in &g.symbols {
            let parents_str = if s.parents.is_empty() {
                String::new()
            } else {
                format!(" > {}", s.parents.join(", "))
            };
            println!("  {} : {}{}", s.name.yellow(), s.kind, parents_str);
        }
        println!();
    }

    Ok(())
}

// ── conventions ──────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
struct ConventionsOutput {
    architecture: Vec<String>,
    frameworks: HashMap<String, Vec<FrameworkHit>>,
    naming_patterns: Vec<NamingPattern>,
}

#[derive(Debug, Serialize)]
struct FrameworkHit {
    name: String,
    count: i64,
}

#[derive(Debug, Serialize)]
struct NamingPattern {
    suffix: String,
    count: i64,
}

/// Known suffix patterns to look for
const NAMING_SUFFIXES: &[&str] = &[
    "ViewModel",
    "Repository",
    "UseCase",
    "Service",
    "Controller",
    "Interactor",
    "Presenter",
    "Factory",
    "Mapper",
    "Provider",
    "Manager",
    "Handler",
    "Adapter",
    "Delegate",
    "Store",
    "Reducer",
    "Component",
    "Fragment",
    "Activity",
    "Screen",
    "View",
    "Widget",
    "Bloc",
    "Cubit",
    "Test",
    "Spec",
    "Module",
    "Router",
    "Navigator",
    "Middleware",
    "Interceptor",
    "Gateway",
];

/// Known import prefixes → (category, display_name). A leading `=` matches the
/// whole import name only (Go's `testing` package, not every `…/testing/…`
/// path); see [`import_matches_rule`] for the others.
const FRAMEWORK_RULES: &[(&str, &str, &str)] = &[
    // DI
    ("dagger", "DI", "Dagger"),
    ("hilt", "DI", "Hilt"),
    ("koin", "DI", "Koin"),
    ("kodein", "DI", "Kodein"),
    ("javax.inject", "DI", "javax.inject"),
    ("com.google.inject", "DI", "Guice"),
    ("org.springframework.beans", "DI", "Spring"),
    ("org.springframework.context", "DI", "Spring"),
    // Async
    ("kotlinx.coroutines", "Async", "Coroutines"),
    ("io.reactivex", "Async", "RxJava"),
    ("rx.", "Async", "Rx"),
    ("combine", "Async", "Combine"),
    ("kotlinx.coroutines.flow", "Async", "Flow"),
    // Network
    ("retrofit", "Network", "Retrofit"),
    ("okhttp", "Network", "OkHttp"),
    ("alamofire", "Network", "Alamofire"),
    ("ktor", "Network", "Ktor"),
    // DB
    ("androidx.room", "DB", "Room"),
    ("io.realm", "DB", "Realm"),
    ("app.cash.sqldelight", "DB", "SQLDelight"),
    ("coredata", "DB", "CoreData"),
    ("active_record", "DB", "ActiveRecord"),
    ("sequel", "DB", "Sequel"),
    // UI
    ("androidx.compose", "UI", "Jetpack Compose"),
    ("swiftui", "UI", "SwiftUI"),
    ("react", "UI", "React"),
    ("vue", "UI", "Vue"),
    ("svelte", "UI", "Svelte"),
    ("flutter", "UI", "Flutter"),
    // Testing
    ("org.junit", "Testing", "JUnit"),
    ("io.kotest", "Testing", "Kotest"),
    ("xctest", "Testing", "XCTest"),
    ("pytest", "Testing", "pytest"),
    ("jest", "Testing", "Jest"),
    ("rspec", "Testing", "RSpec"),
    ("=testing", "Testing", "testing"),
    ("org.mockito", "Testing", "Mockito"),
    ("io.mockk", "Testing", "MockK"),
    // Serialization
    (
        "kotlinx.serialization",
        "Serialization",
        "kotlinx.serialization",
    ),
    ("com.google.gson", "Serialization", "Gson"),
    ("com.squareup.moshi", "Serialization", "Moshi"),
    ("com.fasterxml.jackson", "Serialization", "Jackson"),
    // Web and jobs last: `rspec/rails` is RSpec, `rails_helper` is Rails.
    // Web
    ("rails", "Web", "Rails"),
    ("django", "Web", "Django"),
    ("flask", "Web", "Flask"),
    ("fastapi", "Web", "FastAPI"),
    ("express", "Web", "Express"),
    // Jobs
    ("sidekiq", "Jobs", "Sidekiq"),
    ("celery", "Jobs", "Celery"),
];

/// Architecture detection patterns (path-based)
const ARCH_PATTERNS: &[(&[&str], &str)] = &[
    (
        &["/presentation/", "/domain/", "/data/"],
        "Clean Architecture",
    ),
    (&["/feature/"], "Feature-sliced"),
    (&["/features/"], "Feature-sliced"),
    (&["/bloc/", "/state/", "/event/"], "BLoC"),
    (&["/views/", "/controllers/"], "MVC"),
    (&["/viewmodel/", "/view/", "/model/"], "MVVM"),
    (&["/presenter/"], "MVP"),
    (&["/reducers/", "/actions/", "/store/"], "Redux"),
    (&["/composables/"], "Composition API"),
    (&["/hooks/"], "Hooks pattern"),
];

/// Whether the import name `import` belongs to a [`FRAMEWORK_RULES`] prefix,
/// compared case-insensitively: the prefix starts the name or follows a `.`
/// `/` `:` `@` separator, and is not continued by a letter — unless both
/// sides are CamelCase words of one module name (`CombineExt`).
/// `androidx.hilt.navigation`, `retrofit2`, `@jest/globals`, `react-dom` and
/// `CombineExt` match; `sequel-combine` is no Swift Combine, `preact` and
/// `ReactiveSwift` no React.
fn import_matches_rule(import: &str, prefix: &str) -> bool {
    let lower = import.to_ascii_lowercase();
    if let Some(whole) = prefix.strip_prefix('=') {
        return lower == whole;
    }
    lower.match_indices(prefix).any(|(at, _)| {
        let starts_segment = lower[..at]
            .chars()
            .next_back()
            .is_none_or(|c| matches!(c, '.' | '/' | ':' | '@'));
        let camel_case = import[at..].starts_with(|c: char| c.is_ascii_uppercase());
        let ends_word = prefix.ends_with('.')
            || import[at + prefix.len()..]
                .chars()
                .next()
                .is_none_or(|c| !c.is_alphabetic() || (camel_case && c.is_uppercase()));
        starts_segment && ends_word
    })
}

pub fn cmd_conventions(root: &Path, format: &str) -> Result<()> {
    cmd_conventions_scoped(root, format, &db::SearchScope::none())
}

pub fn cmd_conventions_scoped(root: &Path, format: &str, scope: &db::SearchScope) -> Result<()> {
    if !super::index_available(root, format)? {
        return Ok(());
    }

    let conn = db::open_db_leased(root)?;

    // A. Naming patterns — suffix counts from symbols
    let mut naming: Vec<NamingPattern> = Vec::new();
    for &suffix in NAMING_SUFFIXES {
        let count = db::insight_naming_count(&conn, scope, suffix)?;
        if count >= 3 {
            naming.push(NamingPattern {
                suffix: suffix.to_string(),
                count,
            });
        }
    }
    naming.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.suffix.cmp(&b.suffix)));

    // B. Frameworks — from refs WHERE context LIKE 'import%'
    let mut fw_map: HashMap<String, HashMap<String, i64>> = HashMap::new();
    for (import_name, cnt) in db::insight_foreign_imports(&conn, scope)? {
        for &(prefix, category, display) in FRAMEWORK_RULES {
            if import_matches_rule(&import_name, prefix) {
                *fw_map
                    .entry(category.to_string())
                    .or_default()
                    .entry(display.to_string())
                    .or_insert(0) += cnt;
                break;
            }
        }
    }

    // Navigation imports use short names and omit wildcards. Framework
    // detection needs the actual package, once per import declaration.
    let resolver = super::PathResolver::try_from_conn(root, &conn)?;
    db::visit_insight_files_scoped(&conn, scope, |path, root_path| {
        if !path.ends_with(".java") {
            return Ok(());
        }
        let source_path = root.join(resolver.resolve_with_root_raw(path, Some(root_path)));
        use std::io::Read;
        let mut source = String::new();
        std::fs::File::open(source_path)?
            .take(4 * 1024 * 1024 + 1)
            .read_to_string(&mut source)?;
        anyhow::ensure!(
            source.len() <= 4 * 1024 * 1024,
            "Java profiling source exceeds size limit"
        );
        for import_name in crate::parsers::treesitter::java::import_names(&source)? {
            for &(prefix, category, display) in FRAMEWORK_RULES {
                if import_matches_rule(&import_name, prefix) {
                    *fw_map
                        .entry(category.to_string())
                        .or_default()
                        .entry(display.to_string())
                        .or_insert(0) += 1;
                    break;
                }
            }
        }
        Ok(())
    })?;

    // Convert to sorted output
    let mut frameworks: HashMap<String, Vec<FrameworkHit>> = HashMap::new();
    for (cat, hits) in &fw_map {
        let mut sorted: Vec<FrameworkHit> = hits
            .iter()
            .map(|(name, &count)| FrameworkHit {
                name: name.clone(),
                count,
            })
            .collect();
        sorted.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.name.cmp(&b.name)));
        frameworks.insert(cat.clone(), sorted);
    }

    // C. Architecture detection from file paths
    let mut arch: Vec<String> = Vec::new();
    {
        let mut found = std::collections::HashSet::new();
        db::visit_insight_files_scoped(&conn, scope, |path, _| {
            let path = format!("/{}/", path.to_lowercase());
            for &(markers, _) in ARCH_PATTERNS {
                for marker in markers {
                    if path.contains(marker) {
                        found.insert(*marker);
                    }
                }
            }
            Ok(())
        })?;

        for &(markers, label) in ARCH_PATTERNS {
            if arch.contains(&label.to_string()) {
                continue;
            }
            let all_found = markers.iter().all(|marker| found.contains(marker));
            if all_found {
                arch.push(label.to_string());
            }
        }
    }

    // Output
    if format == "json" {
        let output = ConventionsOutput {
            architecture: arch,
            frameworks,
            naming_patterns: naming,
        };
        println!("{}", serde_json::to_string_pretty(&output)?);
        return Ok(());
    }

    // Text output
    println!("{}", "Project Conventions:".bold());
    println!();

    if !arch.is_empty() {
        println!("{} {}", "Architecture:".cyan(), arch.join(", "));
        println!();
    }

    let mut cats: Vec<&String> = frameworks.keys().collect();
    cats.sort();
    for cat in cats {
        let hits = &frameworks[cat];
        let items: Vec<String> = hits
            .iter()
            .map(|h| format!("{} ({})", h.name, h.count))
            .collect();
        println!("{} {}", format!("{}:", cat).cyan(), items.join(", "));
    }
    if !frameworks.is_empty() {
        println!();
    }

    if !naming.is_empty() {
        println!("{}", "Naming Patterns:".cyan());
        for np in &naming {
            println!("  {:20} {}", np.suffix, np.count);
        }
        println!();
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn framework(import: &str) -> Option<&'static str> {
        FRAMEWORK_RULES
            .iter()
            .find(|(prefix, _, _)| import_matches_rule(import, prefix))
            .map(|&(_, _, display)| display)
    }

    #[test]
    fn framework_rules_match_whole_segments_across_ecosystems() {
        assert_eq!(framework("androidx.hilt.navigation.compose"), Some("Hilt"));
        assert_eq!(
            framework("kotlinx.coroutines.flow.Flow"),
            Some("Coroutines")
        );
        assert_eq!(framework("retrofit2.Retrofit"), Some("Retrofit"));
        assert_eq!(framework("okhttp3.OkHttpClient"), Some("OkHttp"));
        assert_eq!(framework("rx.Observable"), Some("Rx"));
        assert_eq!(framework("Combine"), Some("Combine"));
        assert_eq!(framework("SwiftUI"), Some("SwiftUI"));
        assert_eq!(framework("react-dom"), Some("React"));
        assert_eq!(framework("@jest/globals"), Some("Jest"));
        assert_eq!(framework("package:flutter/material.dart"), Some("Flutter"));
        assert_eq!(framework("testing"), Some("testing"));
        assert_eq!(framework("rails/all"), Some("Rails"));
        assert_eq!(framework("sidekiq/testing"), Some("Sidekiq"));
        assert_eq!(framework("rspec/rails"), Some("RSpec"));
        assert_eq!(framework("django.db.models"), Some("Django"));
        assert_eq!(framework("CombineExt"), Some("Combine"));
        assert_eq!(framework("XCTestDynamicOverlay"), Some("XCTest"));
    }

    #[test]
    fn framework_rules_skip_names_that_only_contain_the_prefix() {
        assert_eq!(framework("sequel-combine"), Some("Sequel"));
        assert_eq!(framework("preact"), None);
        assert_eq!(framework("ReactiveSwift"), None);
        assert_eq!(framework("../testing/setup"), None);
        assert_eq!(framework("./combineUtils"), None);
        assert_eq!(framework("shared-testing/spec_helper"), None);
    }
}
