//! Module-related commands
//!
//! Commands for working with project modules:
//! - module: Find modules by pattern
//! - deps: Show module dependencies
//! - dependents: Show modules that depend on a module
//! - module-route: Show dependency path(s) between two modules
//! - unused_deps: Find unused dependencies

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::time::Instant;

use anyhow::Result;
use colored::Colorize;
use rusqlite::{params, Connection, OptionalExtension};

use super::Pagination;
use crate::db;
use crate::indexer;

/// Open the induced module graph for the invocation directory.
/// Connection-local views keep every navigation query on the same selected
/// graph, including seed resolution, reverse edges, counts and route hops.
fn open_module_query_db(root: &Path) -> Result<db::LeasedConnection> {
    let conn = db::open_db_leased(root)?;
    let resolver = super::PathResolver::try_from_conn(root, &conn)?;
    if let Ok(name) = std::env::var("AST_INDEX_SUBTREE") {
        anyhow::ensure!(
            db::list_subtrees(&conn)?.iter().any(|s| s.name == name),
            "Unknown subtree: {name}"
        );
    }
    let cwd = std::env::current_dir()?;
    let prefix = cwd
        .strip_prefix(root)
        .ok()
        .filter(|p| !p.as_os_str().is_empty())
        .map(|p| format!("{}/", p.to_string_lossy()));
    conn.execute_batch("CREATE TEMP TABLE selected_module_ids(id INTEGER PRIMARY KEY);")?;
    {
        let mut stmt = conn.prepare("SELECT id,path,root_path FROM main.modules")?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let path: String = row.get(1)?;
            let owner: String = row.get(2)?;
            let registered = owner.is_empty()
                || resolver.is_primary_root(Some(&owner))
                || resolver.subtree_name(Some(&owner)).is_some();
            if registered
                && resolver.matches_filter(if owner.is_empty() { None } else { Some(&owner) })
                && prefix
                    .as_ref()
                    .is_none_or(|p| format!("{path}/").starts_with(p))
            {
                conn.execute("INSERT INTO selected_module_ids VALUES (?1)", params![id])?;
            }
        }
    }
    conn.execute_batch(
        "CREATE TEMP VIEW modules AS
             SELECT m.* FROM main.modules m JOIN selected_module_ids s ON s.id=m.id;
         CREATE TEMP VIEW module_deps AS
             SELECT d.* FROM main.module_deps d
             JOIN temp.modules src ON src.id=d.module_id
             JOIN temp.modules dst ON dst.id=d.dep_module_id;",
    )?;
    Ok(conn)
}

/// Resolve display paths by the module's indexed owner, never filesystem probing.
fn module_display_path(
    conn: &Connection,
    root: &Path,
    name: &str,
    path: &str,
    format: &str,
) -> Result<String> {
    let owner: String = conn.query_row(
        "SELECT root_path FROM main.modules WHERE name=?1",
        params![name],
        |r| r.get(0),
    )?;
    let resolver =
        super::PathResolver::try_from_conn(root, conn)?.with_decoration(format != "json");
    let raw = if db::list_subtrees(conn)?.is_empty() && !Path::new(path).is_absolute() {
        path.to_owned()
    } else if owner.is_empty() {
        root.join(path).to_string_lossy().into_owned()
    } else {
        Path::new(&owner).join(path).to_string_lossy().into_owned()
    };
    if format != "json" {
        if let Some(name) = resolver.subtree_name(Some(&owner)) {
            return Ok(format!("[{name}] {raw}"));
        }
    }
    Ok(raw)
}

/// Exact names take precedence; aliases must identify one selected owner.
fn selected_module(conn: &Connection, root: &Path, query: &str) -> Result<Option<(i64, String)>> {
    let exact = conn
        .query_row(
            "SELECT id,name FROM modules WHERE name=?1",
            params![query],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    if exact.is_some() {
        return Ok(exact);
    }
    // An excluded exact name still belongs to its indexed owner. Reusing it
    // as shorthand would redirect an explicitly selected module across roots.
    let excluded_exact: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM main.modules WHERE name=?1)",
        params![query],
        |row| row.get(0),
    )?;
    if excluded_exact {
        return Ok(None);
    }
    let (namespace, alias) = query
        .split_once("::")
        .filter(|(namespace, _)| !namespace.is_empty() && !Path::new(query).is_absolute())
        .map_or((None, query), |(namespace, alias)| (Some(namespace), alias));
    let normalized = alias
        .trim_start_matches(':')
        .replace(':', ".")
        .replace('/', ".");
    let mut stmt = conn.prepare("SELECT id,name,path,root_path FROM modules ORDER BY name")?;
    let mut rows = stmt.query([])?;
    let mut selected = None;
    let mut normalized_selected = None;
    let mut normalized_ambiguous = false;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        let path: String = row.get(2)?;
        let owner: String = row.get(3)?;
        let absolute = if owner.is_empty() {
            root.join(&path)
        } else {
            Path::new(&owner).join(&path)
        };
        let (module_namespace, local_name) = name
            .split_once("::")
            .map_or((None, name.as_str()), |(namespace, local_name)| {
                (Some(namespace), local_name)
            });
        // A normalized alias is still owner-ambiguous, even when one of its
        // candidates happens to have an unqualified primary-root name.
        let matches_namespace =
            namespace.is_none_or(|namespace| Some(namespace) == module_namespace);
        let matches_path = matches_namespace && alias == path;
        if (namespace.is_none() && query == absolute.to_string_lossy()) || matches_path {
            anyhow::ensure!(
                selected.is_none(),
                "Ambiguous module alias; use a qualified module name"
            );
            selected = Some((row.get(0)?, name.clone()));
        }
        if !Path::new(alias).is_absolute()
            && matches_namespace
            && (normalized == local_name || normalized == path.replace('/', "."))
        {
            normalized_ambiguous |= normalized_selected.is_some();
            normalized_selected = Some((row.get(0)?, name));
        }
    }
    if selected.is_some() {
        return Ok(selected);
    }
    anyhow::ensure!(
        !normalized_ambiguous,
        "Ambiguous module alias; use a qualified module name"
    );
    Ok(normalized_selected)
}

fn module_location(conn: &Connection, name: &str) -> Result<(String, String)> {
    Ok(conn.query_row(
        "SELECT path,root_path FROM main.modules WHERE name=?1",
        params![name],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?)
}

/// Readiness belongs to the underlying index, independent of query scope.
fn module_graph_is_unindexed(conn: &Connection) -> Result<bool> {
    let has_edges: bool =
        conn.query_row("SELECT EXISTS(SELECT 1 FROM main.module_deps)", [], |row| {
            row.get(0)
        })?;
    Ok(!has_edges && db::get_metadata_value(conn, "last_modules_indexed_at")?.is_none())
}

/// Render an empty module result without mixing status prose into JSON.
fn print_empty_module_result(
    command: &str,
    subject: &str,
    limit: usize,
    reason: &str,
) -> Result<()> {
    let mut result = serde_json::json!({
        "schema_version": 2, "items": [], "empty_reason": reason
    });
    if command == "module" {
        result["pattern"] = subject.into();
        result["pagination"] = serde_json::to_value(Pagination::new(0, 0, limit))?;
    } else {
        result["module"] = subject.into();
        result["count"] = 0.into();
    }
    if command == "unused-deps" {
        result["summary"] = serde_json::json!({
            "unused": 0, "exported": 0, "used": 0, "total": 0,
            "direct": 0, "transitive": 0, "xml": 0, "resources": 0
        });
    }
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

fn print_module_edges(
    conn: &Connection,
    module: &str,
    root: &Path,
    edges: &[(String, String, String)],
    empty_reason: &str,
) -> Result<()> {
    let reason = if selected_module(conn, root, module)?.is_none() {
        Some("missing_module")
    } else if edges.is_empty() {
        Some(empty_reason)
    } else {
        None
    };
    let items: Vec<_> = edges.iter().map(|(name, path, kind)| {
        Ok(serde_json::json!({"name": name, "path": module_display_path(conn, root, name, path, "json")?, "kind": kind}))
    }).collect::<Result<_>>()?;
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "schema_version": 2, "module": module, "items": items,
            "count": items.len(), "empty_reason": reason
        }))?
    );
    Ok(())
}

/// Find modules by pattern
pub fn cmd_module(root: &Path, pattern: &str, limit: usize) -> Result<()> {
    cmd_module_with_format(root, pattern, limit, "text")
}

pub fn cmd_module_with_format(
    root: &Path,
    pattern: &str,
    limit: usize,
    format: &str,
) -> Result<()> {
    if !db::db_exists(root) {
        if format == "json" {
            return print_empty_module_result("module", pattern, limit, "no_index");
        }
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_module_query_db(root)?;

    let mut stmt = conn.prepare(
        "SELECT name, path FROM modules WHERE name LIKE ?1 ORDER BY name, path LIMIT ?2",
    )?;
    let sql_pattern = format!("%{}%", pattern);
    let mut modules: Vec<(String, String)> = stmt
        .query_map(rusqlite::params![sql_pattern, limit as i64], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<Result<_, _>>()?;

    for (name, path) in &mut modules {
        *path = module_display_path(&conn, root, name, path, format)?;
    }

    if format == "json" {
        let total: usize = conn.query_row(
            "SELECT count(*) FROM modules WHERE name LIKE ?1",
            params![sql_pattern],
            |row| row.get(0),
        )?;
        let items: Vec<_> = modules
            .iter()
            .map(|(name, path)| serde_json::json!({"name": name, "path": path}))
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "schema_version": 2, "pattern": pattern, "items": items,
                "pagination": Pagination::new(total, items.len(), limit),
                "empty_reason": if total == 0 { Some("no_matches") } else { None }
            }))?
        );
        return Ok(());
    }

    println!("{}", format!("Modules matching '{}':", sql_pattern).bold());

    for (name, path) in &modules {
        println!("  {}: {}", name.cyan(), path);
    }

    if modules.is_empty() {
        println!("  No modules found.");
    }

    Ok(())
}

/// Show module dependencies
pub fn cmd_deps(root: &Path, module: &str) -> Result<()> {
    cmd_deps_with_format(root, module, "text")
}

pub fn cmd_deps_with_format(root: &Path, module: &str, format: &str) -> Result<()> {
    if !db::db_exists(root) {
        if format == "json" {
            return print_empty_module_result("deps", module, 0, "no_index");
        }
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_module_query_db(root)?;

    // Check if module deps are indexed
    if module_graph_is_unindexed(&conn)? {
        if format == "json" {
            return print_empty_module_result("deps", module, 0, "not_indexed");
        }
        println!(
            "{}",
            "Module dependencies not indexed. Run 'ast-index rebuild' to index them.".yellow()
        );
        return Ok(());
    }

    let seed = selected_module(&conn, root, module)?;
    let deps = match seed {
        Some((_, name)) => indexer::get_module_deps(&conn, &name)?,
        None => Vec::new(),
    };

    if format == "json" {
        return print_module_edges(&conn, module, root, &deps, "no_dependencies");
    }

    let deps: Vec<_> = deps
        .into_iter()
        .map(|(name, path, kind)| {
            let path = module_display_path(&conn, root, &name, &path, format)?;
            Ok((name, path, kind))
        })
        .collect::<Result<_>>()?;

    println!(
        "{}",
        format!("Dependencies of '{}' ({}):", module, deps.len()).bold()
    );

    // Group by kind
    let api_deps: Vec<_> = deps.iter().filter(|(_, _, k)| k == "api").collect();
    let impl_deps: Vec<_> = deps
        .iter()
        .filter(|(_, _, k)| k == "implementation")
        .collect();
    let other_deps: Vec<_> = deps
        .iter()
        .filter(|(_, _, k)| k != "api" && k != "implementation")
        .collect();

    if !api_deps.is_empty() {
        println!("  {}:", "api".cyan());
        for (name, path, _) in &api_deps {
            println!("    {} ({})", name, path);
        }
    }

    if !impl_deps.is_empty() {
        println!("  {}:", "implementation".cyan());
        for (name, path, _) in &impl_deps {
            println!("    {} ({})", name, path);
        }
    }

    if !other_deps.is_empty() {
        println!("  {}:", "other".cyan());
        for (name, path, kind) in &other_deps {
            println!("    {} ({}) [{}]", name, path, kind);
        }
    }

    if deps.is_empty() {
        println!("  No dependencies found.");
    }

    Ok(())
}

/// Show modules that depend on a module
pub fn cmd_dependents(root: &Path, module: &str) -> Result<()> {
    cmd_dependents_with_format(root, module, "text")
}

pub fn cmd_dependents_with_format(root: &Path, module: &str, format: &str) -> Result<()> {
    if !db::db_exists(root) {
        if format == "json" {
            return print_empty_module_result("dependents", module, 0, "no_index");
        }
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_module_query_db(root)?;

    // Check if module deps are indexed
    if module_graph_is_unindexed(&conn)? {
        if format == "json" {
            return print_empty_module_result("dependents", module, 0, "not_indexed");
        }
        println!(
            "{}",
            "Module dependencies not indexed. Run 'ast-index rebuild' to index them.".yellow()
        );
        return Ok(());
    }

    let seed = selected_module(&conn, root, module)?;
    let dependents = match seed {
        Some((_, name)) => indexer::get_module_dependents(&conn, &name)?,
        None => Vec::new(),
    };

    if format == "json" {
        return print_module_edges(&conn, module, root, &dependents, "no_dependents");
    }

    let dependents: Vec<_> = dependents
        .into_iter()
        .map(|(name, path, kind)| {
            let path = module_display_path(&conn, root, &name, &path, format)?;
            Ok((name, path, kind))
        })
        .collect::<Result<_>>()?;

    println!(
        "{}",
        format!("Modules depending on '{}' ({}):", module, dependents.len()).bold()
    );

    // Group by kind
    let api_deps: Vec<_> = dependents.iter().filter(|(_, _, k)| k == "api").collect();
    let impl_deps: Vec<_> = dependents
        .iter()
        .filter(|(_, _, k)| k == "implementation")
        .collect();
    let other_deps: Vec<_> = dependents
        .iter()
        .filter(|(_, _, k)| k != "api" && k != "implementation")
        .collect();

    if !api_deps.is_empty() {
        println!("  {} ({}):", "via api".cyan(), api_deps.len());
        for (name, path, _) in &api_deps {
            println!("    {} ({})", name, path);
        }
    }

    if !impl_deps.is_empty() {
        println!("  {} ({}):", "via implementation".cyan(), impl_deps.len());
        for (name, path, _) in &impl_deps {
            println!("    {} ({})", name, path);
        }
    }

    if !other_deps.is_empty() {
        println!("  {} ({}):", "via other".cyan(), other_deps.len());
        for (name, path, kind) in &other_deps {
            println!("    {} ({}) [{}]", name, path, kind);
        }
    }

    if dependents.is_empty() {
        println!("  No dependents found.");
    }

    Ok(())
}

/// Find unused dependencies in a module
pub fn cmd_unused_deps(
    root: &Path,
    module: &str,
    verbose: bool,
    check_transitive: bool,
    check_xml: bool,
    check_resources: bool,
) -> Result<()> {
    cmd_unused_deps_with_format(
        root,
        module,
        verbose,
        check_transitive,
        check_xml,
        check_resources,
        "text",
    )
}

pub fn cmd_unused_deps_with_format(
    root: &Path,
    module: &str,
    verbose: bool,
    check_transitive: bool,
    check_xml: bool,
    check_resources: bool,
    format: &str,
) -> Result<()> {
    if !db::db_exists(root) {
        if format == "json" {
            return print_empty_module_result("unused-deps", module, 0, "no_index");
        }
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_module_query_db(root)?;

    // Check if module deps are indexed
    if module_graph_is_unindexed(&conn)? {
        if format == "json" {
            return print_empty_module_result("unused-deps", module, 0, "not_indexed");
        }
        println!(
            "{}",
            "Module dependencies not indexed. Run 'ast-index rebuild' first.".yellow()
        );
        return Ok(());
    }

    // Get module id and path
    let module_info = selected_module(&conn, root, module)?
        .map(|(id, name)| -> Result<_> {
            let (path, _) = module_location(&conn, &name)?;
            Ok((id, path, name))
        })
        .transpose()?;

    let (module_id, module_path, module_name) = match module_info {
        Some(info) => info,
        None => {
            if format == "json" {
                return print_empty_module_result("unused-deps", module, 0, "missing_module");
            }
            println!(
                "{}",
                format!("Module '{}' not found in index.", module).red()
            );
            return Ok(());
        }
    };

    // Get all dependencies
    let deps = indexer::get_module_deps(&conn, &module_name)?;

    if deps.is_empty() {
        if format == "json" {
            return print_empty_module_result("unused-deps", module, 0, "no_dependencies");
        }
        println!(
            "{}",
            format!("Module '{}' has no dependencies.", module).yellow()
        );
        return Ok(());
    }

    if format != "json" {
        println!(
            "{}",
            format!("Analyzing {} dependencies of '{}'...", deps.len(), module).bold()
        );
        if check_transitive || check_xml || check_resources {
            let checks: Vec<&str> = [
                if check_transitive {
                    Some("transitive")
                } else {
                    None
                },
                if check_xml { Some("XML") } else { None },
                if check_resources {
                    Some("resources")
                } else {
                    None
                },
            ]
            .into_iter()
            .flatten()
            .collect();
            println!("  Checking: direct imports + {}\n", checks.join(", "));
        } else {
            println!("  Checking: direct imports only (strict mode)\n");
        }
    }

    // Results tracking
    #[derive(Default)]
    struct DepUsage {
        direct_count: usize,
        direct_symbols: Vec<String>,
        transitive_count: usize,
        transitive_via: Vec<(String, Vec<String>)>, // (intermediate_module, symbols)
        xml_count: usize,
        xml_usages: Vec<(String, i64)>, // (class_name, line)
        resource_count: usize,
        resource_usages: Vec<(String, String)>, // (resource_name, usage_type)
    }

    // Swift code imports a dependency by its module name, so `import Dep`
    // in any source file of the module proves the dependency is used no matter
    // which kinds of symbols (structs, extensions, free functions) it touches.
    let (_, module_root) = module_location(&conn, &module_name)?;
    let module_imports = db::find_swift_imports_under(&conn, &module_path)?;

    let mut dep_usages: HashMap<String, DepUsage> = HashMap::new();
    let mut unused: Vec<(String, String, String)> = vec![];
    let mut exported: Vec<(String, String, String)> = vec![]; // api deps not directly used
    let mut used_direct: Vec<(String, String, String, usize)> = vec![];
    let mut used_transitive: Vec<(String, String, String, usize)> = vec![];
    let mut used_xml: Vec<(String, String, String, usize)> = vec![];
    let mut used_resources: Vec<(String, String, String, usize)> = vec![];

    for (dep_name, dep_path, dep_kind) in &deps {
        let mut usage = DepUsage::default();
        let (_, dependency_root) = module_location(&conn, dep_name)?;

        // 1. Check direct usage: a Swift import, else references to the
        //    dependency's symbols via the index (refs table)
        let dep_module_name = dep_name.rsplit('.').next().unwrap_or(dep_name);
        if module_imports.contains(dep_module_name) {
            usage.direct_count = 1;
            usage.direct_symbols = vec![format!("import {}", dep_module_name)];
        } else {
            let (direct_count, direct_names) = count_symbols_used_in_module(
                &conn,
                root,
                dep_path,
                &module_path,
                &dependency_root,
                &module_root,
            )?;
            usage.direct_count = direct_count;
            usage.direct_symbols = direct_names;
        }

        // 2. A dependency is used transitively when the consumer references a
        // type it re-exports. Reachability alone is not evidence of usage.
        if check_transitive && usage.direct_count == 0 {
            let mut stmt = conn.prepare(
                "WITH RECURSIVE exported(id) AS (
                     SELECT md.dep_module_id FROM module_deps md
                     JOIN modules m ON m.id=md.module_id
                     WHERE m.name=?1 AND md.dep_kind='api'
                     UNION
                     SELECT md.dep_module_id FROM module_deps md
                     JOIN exported e ON e.id=md.module_id WHERE md.dep_kind='api'
                 )
                 SELECT m.name,m.path FROM modules m JOIN exported e ON e.id=m.id
                 WHERE m.name!=?1 ORDER BY m.name,m.path",
            )?;
            let exports = stmt.query_map(params![dep_name], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            for export in exports {
                let (name, path) = export?;
                let (count, names) = count_symbols_used_in_module(
                    &conn,
                    root,
                    &path,
                    &module_path,
                    &module_location(&conn, &name)?.1,
                    &module_root,
                )?;
                if count > 0 {
                    usage.transitive_count += count;
                    usage.transitive_via.push((name, names));
                }
            }
        }

        // 3. Check XML usages
        if check_xml && usage.direct_count == 0 && usage.transitive_count == 0 {
            // Get classes from the dependency module
            let mut class_stmt = conn.prepare(&format!(
                "SELECT DISTINCT s.name, s.qualified_name, substr(f.path,-5)='.java',
                 CASE WHEN substr(f.path,-5)='.java' THEN (
                     SELECT owner.qualified_name ||
                            replace(substr(s.qualified_name,length(owner.qualified_name)+1),'.','$')
                     FROM symbols owner
                     WHERE owner.file_id=s.file_id
                       AND owner.kind IN ('class','interface','enum')
                       AND owner.qualified_name IS NOT NULL
                       AND (owner.id=s.id OR (
                           substr(s.qualified_name,1,length(owner.qualified_name)+1)=owner.qualified_name||'.'
                           AND owner.line<=s.line
                           AND COALESCE(owner.end_line,owner.line)>=COALESCE(s.end_line,s.line)
                       ))
                     ORDER BY length(owner.qualified_name),owner.id LIMIT 1
                 ) END
                 FROM symbols s
                 JOIN files f ON s.file_id = f.id
                 WHERE {MODULE_FILE_SCOPE} AND s.kind IN ('class', 'object')
                 ORDER BY s.name,s.qualified_name"
            ))?;
            let classes = class_stmt.query_map(params![dep_path], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, bool>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            })?;

            // Check if any class is used in XML layouts of the target module
            for class_name in classes {
                let (class_name, qualified_name, is_java, binary_name) = class_name?;
                // Java package identity must not collapse to the last segment:
                // app.Widget is not fixture.Widget. XML uses JVM '$' spelling
                // for nested types; derive that spelling from the enclosing
                // type, leaving legal '$' characters in package names intact.
                let mut xml_stmt = conn.prepare(
                    "SELECT x.file_path, x.line FROM xml_usages x
                     JOIN modules m ON x.module_id = m.id
                     WHERE m.id = ?1 AND (
                         x.class_name = ?2
                         OR (?4=1 AND ?3 IS NOT NULL AND (
                             x.class_name=?3 OR x.class_name=?5
                         ))
                         OR (?4=0 AND (
                             substr(x.class_name,-length(?2)-1)='.'||?2
                             OR substr(x.class_name,-length(?2)-1)='$'||?2
                         ))
                     )",
                )?;
                let xml_results = xml_stmt.query_map(
                    params![module_id, &class_name, qualified_name, is_java, binary_name],
                    |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
                )?;

                for result in xml_results {
                    let (_file_path, line) = result?;
                    usage.xml_count += 1;
                    if usage.xml_usages.len() < 3 {
                        usage.xml_usages.push((class_name.clone(), line));
                    }
                }
            }
        }

        // 4. Check resource usages
        if check_resources
            && usage.direct_count == 0
            && usage.transitive_count == 0
            && usage.xml_count == 0
        {
            // Get resources defined in the dependency module
            let mut res_stmt = conn.prepare(
                "SELECT DISTINCT r.type, r.name FROM resources r
                 JOIN modules m ON r.module_id = m.id
                 WHERE m.name = ?1
                 ORDER BY r.type,r.name",
            )?;
            let resources = res_stmt.query_map(params![dep_name], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;

            // Check if these resources are used in the target module
            for resource in resources {
                let (res_type, res_name) = resource?;
                let usage_scope = MODULE_FILE_SCOPE
                    .replace("f.path", "ru.usage_file")
                    .replace("f.root_path", "owner.root_path");
                let mut usage_stmt = conn.prepare(&format!(
                    "SELECT ru.usage_type FROM resource_usages ru
                     JOIN resources r ON ru.resource_id = r.id
                     JOIN modules owner ON owner.id=r.module_id
                     WHERE {usage_scope} AND owner.name=?2 AND r.type=?3 AND r.name=?4
                     ORDER BY ru.usage_file,ru.usage_line,ru.id"
                ))?;
                let usages = usage_stmt
                    .query_map(params![module_path, dep_name, res_type, res_name], |row| {
                        row.get::<_, String>(0)
                    })?;

                let mut count = 0;
                let mut first = None;
                for usage_type in usages {
                    let usage_type = usage_type?;
                    count += 1;
                    if first.is_none() {
                        first = Some(usage_type);
                    }
                }
                if count > 0 {
                    usage.resource_count += count;
                    if usage.resource_usages.len() < 3 {
                        usage.resource_usages.push((
                            format!("@{}/{}", res_type, res_name),
                            first.unwrap_or_default(),
                        ));
                    }
                }
            }
        }

        // Categorize the dependency
        let total_usage =
            usage.direct_count + usage.transitive_count + usage.xml_count + usage.resource_count;

        if total_usage == 0 {
            // Check if this is an api dependency (exported for consumers)
            if dep_kind == "api" {
                exported.push((dep_name.clone(), dep_path.clone(), dep_kind.clone()));
            } else {
                unused.push((dep_name.clone(), dep_path.clone(), dep_kind.clone()));
            }
        } else if usage.direct_count > 0 {
            used_direct.push((
                dep_name.clone(),
                dep_path.clone(),
                dep_kind.clone(),
                usage.direct_count,
            ));
        } else if usage.transitive_count > 0 {
            used_transitive.push((
                dep_name.clone(),
                dep_path.clone(),
                dep_kind.clone(),
                usage.transitive_count,
            ));
        } else if usage.xml_count > 0 {
            used_xml.push((
                dep_name.clone(),
                dep_path.clone(),
                dep_kind.clone(),
                usage.xml_count,
            ));
        } else if usage.resource_count > 0 {
            used_resources.push((
                dep_name.clone(),
                dep_path.clone(),
                dep_kind.clone(),
                usage.resource_count,
            ));
        }

        dep_usages.insert(dep_name.clone(), usage);
    }

    if format == "json" {
        let mut items = Vec::with_capacity(deps.len());
        for (name, path, kind) in &deps {
            let usage = &dep_usages[name];
            let category = if usage.direct_count > 0 {
                "direct"
            } else if usage.transitive_count > 0 {
                "transitive"
            } else if usage.xml_count > 0 {
                "xml"
            } else if usage.resource_count > 0 {
                "resources"
            } else if kind == "api" {
                "exported"
            } else {
                "unused"
            };
            let mut item = serde_json::json!({
                "name": name, "path": module_display_path(&conn, root, name, path, format)?, "kind": kind, "category": category,
                "usage": {"direct": usage.direct_count, "transitive": usage.transitive_count,
                          "xml": usage.xml_count, "resources": usage.resource_count}
            });
            if verbose {
                let consumers = if category == "exported" {
                    exported_consumers(&conn, name, &module_name)?
                } else {
                    Vec::new()
                };
                item["examples"] = serde_json::json!({
                    "direct": usage.direct_symbols,
                    "transitive": usage.transitive_via.iter().map(|(via, symbols)| {
                        serde_json::json!({"module": via, "symbols": symbols})
                    }).collect::<Vec<_>>(),
                    "xml": usage.xml_usages.iter().map(|(class, line)| {
                        serde_json::json!({"class": class, "line": line})
                    }).collect::<Vec<_>>(),
                    "resources": usage.resource_usages.iter().map(|(name, usage_type)| {
                        serde_json::json!({"name": name, "usage_type": usage_type})
                    }).collect::<Vec<_>>(),
                    "consumers": consumers
                });
            }
            items.push(item);
        }
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "schema_version": 2, "module": module, "items": items, "count": deps.len(),
                "empty_reason": null,
                "summary": {"unused": unused.len(), "exported": exported.len(),
                    "used": used_direct.len() + used_transitive.len() + used_xml.len() + used_resources.len(),
                    "total": deps.len(), "direct": used_direct.len(), "transitive": used_transitive.len(),
                    "xml": used_xml.len(), "resources": used_resources.len()}
            }))?
        );
        return Ok(());
    }

    // Output results
    if verbose {
        println!("{}", "=== Direct Usage ===".cyan().bold());
        for (name, _, _, count) in &used_direct {
            let usage = dep_usages.get(name).unwrap();
            let symbols_str = if usage.direct_symbols.is_empty() {
                String::new()
            } else {
                format!(": {}", usage.direct_symbols.join(", "))
            };
            println!(
                "  {} {} - {} symbols{}",
                "✓".green(),
                name,
                count,
                symbols_str
            );
        }
        if used_direct.is_empty() {
            println!("  (none)");
        }

        if check_transitive {
            println!("\n{}", "=== Transitive Usage ===".cyan().bold());
            for (name, _, _, count) in &used_transitive {
                let usage = dep_usages.get(name).unwrap();
                println!("  {} {} - {} symbols", "✓".green(), name, count);
                for (via, symbols) in &usage.transitive_via {
                    println!("    └─ via {}: {}", via, symbols.join(", "));
                }
            }
            if used_transitive.is_empty() {
                println!("  (none)");
            }
        }

        if check_xml {
            println!("\n{}", "=== XML Usage ===".cyan().bold());
            for (name, _, _, count) in &used_xml {
                let usage = dep_usages.get(name).unwrap();
                println!("  {} {} - {} usages", "✓".green(), name, count);
                for (class, line) in &usage.xml_usages {
                    println!("    └─ {}:{}", class, line);
                }
            }
            if used_xml.is_empty() {
                println!("  (none)");
            }
        }

        if check_resources {
            println!("\n{}", "=== Resource Usage ===".cyan().bold());
            for (name, _, _, count) in &used_resources {
                let usage = dep_usages.get(name).unwrap();
                println!("  {} {} - {} usages", "✓".green(), name, count);
                for (res, usage_type) in &usage.resource_usages {
                    println!("    └─ {} ({})", res, usage_type);
                }
            }
            if used_resources.is_empty() {
                println!("  (none)");
            }
        }
    }

    // Exported (api deps not directly used but intentionally re-exported)
    if !exported.is_empty() {
        println!(
            "\n{}",
            "=== Exported (not directly used) ===".yellow().bold()
        );
        for (name, _path, _kind) in &exported {
            println!("  {} {} (api)", "⚡".yellow(), name);
            if verbose {
                // Find consumers who use this exported dep
                let consumers = exported_consumers(&conn, name, &module_name)?;
                if !consumers.is_empty() {
                    println!("    └─ used by: {}", consumers.join(", "));
                }
            }
        }
    }

    // Unused
    println!("\n{}", "=== Unused ===".red().bold());
    if !unused.is_empty() {
        for (name, _path, kind) in &unused {
            println!("  {} {} ({})", "✗".red(), name, kind);
            if verbose {
                println!("    - No direct imports");
                if check_transitive {
                    println!("    - No transitive usage");
                }
                if check_xml {
                    println!("    - No XML usage");
                }
                if check_resources {
                    println!("    - No resource usage");
                }
            }
        }
    } else {
        println!("  (none - all dependencies are used)");
    }

    println!("\n{}", "=== Summary ===".bold());
    let total_used =
        used_direct.len() + used_transitive.len() + used_xml.len() + used_resources.len();
    println!(
        "Total: {} unused, {} exported, {} used of {} dependencies",
        unused.len(),
        exported.len(),
        total_used,
        deps.len()
    );
    println!("  - Direct: {}", used_direct.len());
    if check_transitive {
        println!("  - Transitive: {}", used_transitive.len());
    }
    if check_xml {
        println!("  - XML: {}", used_xml.len());
    }
    if check_resources {
        println!("  - Resources: {}", used_resources.len());
    }
    if !exported.is_empty() {
        println!("  - Exported (api): {}", exported.len());
    }

    Ok(())
}

/// Keep consumer examples bounded and stable in both renderers.
fn exported_consumers(conn: &Connection, name: &str, module: &str) -> Result<Vec<String>> {
    let mut stmt = conn.prepare(
        "SELECT DISTINCT m.name FROM module_deps md
         JOIN modules m ON md.module_id = m.id
         JOIN modules dep ON md.dep_module_id = dep.id
         WHERE dep.name = ?1 AND m.name != ?2
         ORDER BY m.name LIMIT 5",
    )?;
    let rows = stmt.query_map(params![name, module], |row| row.get(0))?;
    Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
}

/// Files belong to their deepest indexed module directory. Literal prefix
/// comparisons avoid both sibling matches and SQL wildcard interpretation.
const MODULE_FILE_SCOPE: &str = "
    (?1='' OR substr(f.path,1,length(?1)+1)=?1||'/')
    AND NOT EXISTS (
        SELECT 1 FROM main.modules child
        WHERE (child.root_path='' OR child.root_path=f.root_path)
          AND length(child.path)>length(?1)
          AND (?1='' OR substr(child.path,1,length(?1)+1)=?1||'/')
          AND substr(f.path,1,length(child.path)+1)=child.path||'/'
    )";

// ── module-route ─────────────────────────────────────────────────────────────

/// A single edge in a dependency path.
#[derive(Debug, serde::Serialize, Clone)]
struct EdgeHop {
    from: String,
    to: String,
    kind: String,
}

/// One complete path from source to target module.
#[derive(Debug, serde::Serialize, Clone)]
struct RoutePath {
    hops: Vec<EdgeHop>,
    length: usize,
}

/// Full result envelope returned by `cmd_module_route`.
#[derive(Debug, serde::Serialize)]
struct ModuleRouteResult {
    from: String,
    to: String,
    paths: Vec<RoutePath>,
    count: usize,
    truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    truncation_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    empty_reason: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    warnings: Vec<String>,
    /// Search progress when DFS was used. Surfaces via JSON for tooling.
    #[serde(skip_serializing_if = "Option::is_none")]
    search_stats: Option<SearchStats>,
}

#[derive(Debug, serde::Serialize, Clone)]
struct SearchStats {
    nodes_visited: usize,
    edges_explored: usize,
    elapsed_ms: u64,
    max_depth_reached: usize,
    timeout_ms: u64,
    suggested_timeout_ms: Option<u64>,
}

fn render_json(result: &ModuleRouteResult) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(result)?);
    Ok(())
}

fn render_text(result: &ModuleRouteResult) {
    if let Some(reason) = &result.empty_reason {
        let hint = match reason.as_str() {
            "missing_module_from" => format!("Module '{}' not found in index.", result.from),
            "missing_module_to" => format!("Module '{}' not found in index.", result.to),
            "unreachable" => format!(
                "No dependency path from '{}' to '{}'.",
                result.from, result.to
            ),
            "self" => format!("'{}' depends on itself (trivial path).", result.from),
            "truncated_timeout" | "truncated_prune_timeout" => {
                let progress = result
                    .search_stats
                    .as_ref()
                    .map(|s| {
                        let suggested = s
                            .suggested_timeout_ms
                            .map(|ms| format!(" Try --timeout-ms {}.", ms))
                            .unwrap_or_default();
                        format!(
                            " Explored {} edges across {} nodes (max depth {}) in {} ms before timeout ({} ms).{}",
                            s.edges_explored, s.nodes_visited, s.max_depth_reached, s.elapsed_ms, s.timeout_ms, suggested
                        )
                    })
                    .unwrap_or_default();
                format!(
                    "Search from '{}' to '{}' timed out before finding any paths.{}",
                    result.from, result.to, progress
                )
            }
            "truncated_max_paths" => format!(
                "Hit max-paths limit before recording any complete path from '{}' to '{}'. Try --max-paths <larger> or --max-depth <smaller>.",
                result.from, result.to
            ),
            _ => format!("No path found (reason: {}).", reason),
        };
        println!("{}", hint.yellow());
        for w in &result.warnings {
            eprintln!("{}", format!("Warning: {}", w).yellow());
        }
        return;
    }

    let shortest = result.paths.first().map(|p| p.length).unwrap_or(0);
    println!(
        "{}",
        format!(
            "{} → {} ({} path{}, shortest = {} hop{})",
            result.from.cyan(),
            result.to.cyan(),
            result.count,
            if result.count == 1 { "" } else { "s" },
            shortest,
            if shortest == 1 { "" } else { "s" }
        )
        .bold()
    );

    for (i, path) in result.paths.iter().enumerate() {
        println!(
            "\n  Path {} ({} hop{}):",
            i + 1,
            path.length,
            if path.length == 1 { "" } else { "s" }
        );
        if path.hops.is_empty() {
            println!("    {} (same module)", result.from.cyan());
        }
        for hop in &path.hops {
            println!(
                "    {} → {} [{}]",
                hop.from.cyan(),
                hop.to.cyan(),
                hop.kind.dimmed()
            );
        }
    }

    if result.truncated {
        let reason = result.truncation_reason.as_deref().unwrap_or("limit");
        let detail = result
            .search_stats
            .as_ref()
            .map(|s| {
                let suggested = s
                    .suggested_timeout_ms
                    .map(|ms| format!(", try --timeout-ms {}", ms))
                    .unwrap_or_default();
                format!(
                    " — explored {} edges, {} nodes, max depth {} in {} ms{}",
                    s.edges_explored, s.nodes_visited, s.max_depth_reached, s.elapsed_ms, suggested
                )
            })
            .unwrap_or_default();
        println!("\n  {} (truncated: {}{})", "…".dimmed(), reason, detail);
    }

    for w in &result.warnings {
        eprintln!("{}", format!("Warning: {}", w).yellow());
    }
}

/// Escape a string for use as a Mermaid node label inside `[…]`.
///
/// The characters `[`, `]`, `(`, `)`, `{`, `}`, `|`, `"`, and newlines have
/// special meaning in Mermaid flowchart syntax and must be replaced or removed.
fn mermaid_escape(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            '[' => '⟦',
            ']' => '⟧',
            '(' => '❨',
            ')' => '❩',
            '{' => '❴',
            '}' => '❵',
            '|' => '∣',
            '"' => '\'',
            '\n' | '\r' => ' ',
            other => other,
        })
        .collect()
}

/// Escape a string for use inside a DOT double-quoted string.
///
/// DOT requires `"` → `\"` and `\n` → `\\n` inside quoted identifiers.
fn dot_escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

fn render_mermaid(result: &ModuleRouteResult) {
    println!("```mermaid");
    println!("flowchart LR");
    if result.truncated {
        println!(
            "  %% Truncated: {}",
            result.truncation_reason.as_deref().unwrap_or("limit")
        );
    }
    if result.paths.is_empty() {
        let reason = result.empty_reason.as_deref().unwrap_or("no_path");
        println!("  %% No path: {}", reason);
        println!("```");
        return;
    }

    // Collect all unique node names and assign id aliases.
    let mut node_order: Vec<String> = Vec::new();
    let mut node_ids: HashMap<String, String> = HashMap::new();
    for path in &result.paths {
        for hop in &path.hops {
            for name in [&hop.from, &hop.to] {
                if !node_ids.contains_key(name.as_str()) {
                    let alias = format!("n{}", node_order.len());
                    node_ids.insert(name.clone(), alias);
                    node_order.push(name.clone());
                }
            }
        }
    }

    for name in &node_order {
        let alias = &node_ids[name];
        // Mermaid label text inside `[]` must not contain `[](){}|"\n`.
        // Replace those chars with safe Unicode look-alikes / escape sequences.
        let safe = mermaid_escape(name);
        println!("  {}[{}]", alias, safe);
    }

    let mut seen_edges: HashSet<(String, String, String)> = HashSet::new();
    for path in &result.paths {
        for hop in &path.hops {
            let key = (hop.from.clone(), hop.to.clone(), hop.kind.clone());
            if seen_edges.contains(&key) {
                continue;
            }
            seen_edges.insert(key);
            let from_id = &node_ids[&hop.from];
            let to_id = &node_ids[&hop.to];
            // Omit edge label for "implementation" to reduce visual noise.
            if hop.kind == "implementation" {
                println!("  {} --> {}", from_id, to_id);
            } else {
                let safe_kind = mermaid_escape(&hop.kind);
                println!("  {} -->|{}| {}", from_id, safe_kind, to_id);
            }
        }
    }
    println!("```");
}

fn render_dot(result: &ModuleRouteResult) {
    println!("digraph module_route {{");
    println!("  rankdir=LR;");
    if result.truncated {
        println!(
            "  // Truncated: {}",
            result.truncation_reason.as_deref().unwrap_or("limit")
        );
    }

    if result.paths.is_empty() {
        let reason = result.empty_reason.as_deref().unwrap_or("no_path");
        println!("  // No path: {}", reason);
        println!("}}");
        return;
    }

    // Collect unique node names.
    let mut node_set: HashSet<String> = HashSet::new();
    for path in &result.paths {
        for hop in &path.hops {
            node_set.insert(hop.from.clone());
            node_set.insert(hop.to.clone());
        }
    }
    let mut node_list: Vec<_> = node_set.iter().cloned().collect();
    node_list.sort();
    for name in &node_list {
        println!("  \"{}\";", dot_escape(name));
    }

    let mut seen_edges: HashSet<(String, String, String)> = HashSet::new();
    for path in &result.paths {
        for hop in &path.hops {
            let key = (hop.from.clone(), hop.to.clone(), hop.kind.clone());
            if seen_edges.contains(&key) {
                continue;
            }
            seen_edges.insert(key);
            println!(
                "  \"{}\" -> \"{}\" [label=\"{}\"];",
                dot_escape(&hop.from),
                dot_escape(&hop.to),
                dot_escape(&hop.kind),
            );
        }
    }
    println!("}}");
}

/// Outcome of a `bfs_shortest` run. Distinguishes "no path exists" from
/// "we timed out before deciding" — the caller must NOT report `unreachable`
/// when the budget was exhausted.
enum BfsOutcome {
    Found(RoutePath),
    NotFound,
    TimedOut,
}

/// BFS from `from_id` to `to_id` respecting `kind_filter` and `max_depth`.
/// Stops as soon as `to_id` is first dequeued (single shortest path).
fn bfs_shortest(
    conn: &Connection,
    from_id: i64,
    to_id: i64,
    kind_filter: Option<&str>,
    max_depth: usize,
    deadline: Instant,
    timeout_ms: u64,
) -> Result<BfsOutcome> {
    // Queue entry: (node_id, current_depth)
    let mut queue: VecDeque<(i64, usize)> = VecDeque::new();
    // predecessor: node_id → (predecessor_id, edge_kind, node_name)
    let mut predecessor: HashMap<i64, (i64, String, String)> = HashMap::new();
    // name cache: id → name
    let mut names: HashMap<i64, String> = HashMap::new();

    // Seed the source name.
    let from_name: String = db::get_module_name(conn, from_id)?.unwrap_or_default();
    names.insert(from_id, from_name);

    queue.push_back((from_id, 0));
    let mut visited: HashSet<i64> = HashSet::new();
    visited.insert(from_id);

    while let Some((node_id, depth)) = queue.pop_front() {
        // Wall-clock guard checked per node (not per edge).
        if deadline.elapsed().as_millis() as u64 >= timeout_ms {
            return Ok(BfsOutcome::TimedOut);
        }

        if node_id == to_id {
            // Backtrack predecessor chain.
            return Ok(BfsOutcome::Found(build_path_from_predecessors(
                &predecessor,
                &names,
                from_id,
                to_id,
            )));
        }

        if depth >= max_depth {
            continue;
        }

        let edges = db::get_outgoing_edges_dedup(conn, node_id, kind_filter)?;
        for (dep_id, dep_name, edge_kind) in edges {
            if !visited.contains(&dep_id) {
                visited.insert(dep_id);
                let node_name = names.get(&node_id).cloned().unwrap_or_default();
                predecessor.insert(dep_id, (node_id, edge_kind, node_name));
                names.insert(dep_id, dep_name);
                queue.push_back((dep_id, depth + 1));
            }
        }
    }

    Ok(BfsOutcome::NotFound)
}

fn build_path_from_predecessors(
    predecessor: &HashMap<i64, (i64, String, String)>,
    names: &HashMap<i64, String>,
    from_id: i64,
    to_id: i64,
) -> RoutePath {
    let mut chain: Vec<(i64, String, String)> = Vec::new(); // (node_id, edge_kind, from_name)
    let mut cur = to_id;
    while cur != from_id {
        if let Some((prev_id, kind, from_name)) = predecessor.get(&cur) {
            chain.push((cur, kind.clone(), from_name.clone()));
            cur = *prev_id;
        } else {
            break;
        }
    }
    chain.reverse();

    let hops: Vec<EdgeHop> = chain
        .into_iter()
        .map(|(to_id_hop, kind, from_name)| EdgeHop {
            from: from_name,
            to: names.get(&to_id_hop).cloned().unwrap_or_default(),
            kind,
        })
        .collect();
    let length = hops.len();
    RoutePath { hops, length }
}

/// Frame for iterative DFS.
struct DfsFrame {
    node_id: i64,
    edges: Vec<(i64, String, String)>, // remaining outgoing edges to explore
    edge_idx: usize,
}

/// Statistics from a DFS traversal — used to give actionable hints when
/// the search hits a wall-clock or path-count cap.
#[derive(Debug, Clone, Default)]
pub struct DfsStats {
    pub nodes_visited: usize,
    pub edges_explored: usize,
    pub elapsed_ms: u64,
    pub max_depth_reached: usize,
}

/// Reverse BFS from `to_id`: returns the minimum number of hops needed
/// from each visited node to reach `to_id` (in forward graph), bounded by
/// `max_depth`. Nodes that cannot reach `to_id` within `max_depth` are
/// absent from the map. Used by `dfs_all_paths` to prune subtrees that
/// cannot lead to the target.
///
/// Returns `(distance_map, truncated_by_timeout)`. When the second element is
/// `true` the map is incomplete — the BFS was cut short by the wall-clock
/// deadline, so absence from the map does NOT mean "unreachable".
fn compute_reverse_distances(
    conn: &Connection,
    to_id: i64,
    kind_filter: Option<&str>,
    max_depth: usize,
    deadline: Instant,
    timeout_ms: u64,
) -> Result<(HashMap<i64, usize>, bool)> {
    let mut dist: HashMap<i64, usize> = HashMap::new();
    let mut queue: VecDeque<(i64, usize)> = VecDeque::new();
    let mut timed_out = false;
    dist.insert(to_id, 0);
    queue.push_back((to_id, 0));

    while let Some((node, d)) = queue.pop_front() {
        if deadline.elapsed().as_millis() as u64 >= timeout_ms {
            timed_out = true;
            break;
        }
        if d >= max_depth {
            continue;
        }
        let preds = db::get_incoming_edges_dedup(conn, node, kind_filter)?;
        for (pred_id, _name, _kind) in preds {
            if !dist.contains_key(&pred_id) {
                dist.insert(pred_id, d + 1);
                queue.push_back((pred_id, d + 1));
            }
        }
    }
    Ok((dist, timed_out))
}

/// DFS collecting all simple paths from `from_id` to `to_id`.
///
/// Pruning strategy: a reverse BFS from `to_id` computes the minimum hop
/// distance from every node to `to_id` (`dist_to`). During DFS we never
/// recurse into a child that is absent from `dist_to` (cannot reach the
/// target at all) or for which `current_depth + dist_to[child] > max_depth`
/// (cannot reach within budget). On large graphs (1k+ modules) this trims
/// 90%+ of decoy subtrees and lets DFS finish in milliseconds.
fn dfs_all_paths(
    conn: &Connection,
    from_id: i64,
    to_id: i64,
    kind_filter: Option<&str>,
    max_depth: usize,
    max_paths: usize,
    deadline: Instant,
    timeout_ms: u64,
) -> Result<(Vec<RoutePath>, bool, Option<String>, DfsStats)> {
    let mut results: Vec<RoutePath> = Vec::new();
    let mut truncated = false;
    let mut truncation_reason: Option<String> = None;
    let mut stats = DfsStats::default();

    // max_paths == 0 means "do not collect any path" — return truncated
    // immediately so we don't materialise one path before checking the cap.
    if max_paths == 0 {
        stats.elapsed_ms = deadline.elapsed().as_millis() as u64;
        return Ok((results, true, Some("max_paths".to_string()), stats));
    }

    // Reverse-BFS pruning map: node → min hops to to_id. Nodes that can't
    // reach to_id within max_depth are absent.
    let (dist_to, prune_timed_out) =
        compute_reverse_distances(conn, to_id, kind_filter, max_depth, deadline, timeout_ms)?;

    // When the reverse BFS was cut short we cannot trust absence from dist_to:
    // some reachable nodes may simply not have been visited yet.
    // Signal immediately rather than running DFS that would produce wrong "unreachable".
    if prune_timed_out {
        truncated = true;
        truncation_reason = Some("prune_timeout".to_string());
        stats.elapsed_ms = deadline.elapsed().as_millis() as u64;
        return Ok((results, truncated, truncation_reason, stats));
    }

    // If from_id can't reach to_id at all, return empty fast.
    if !dist_to.contains_key(&from_id) {
        stats.elapsed_ms = deadline.elapsed().as_millis() as u64;
        return Ok((results, truncated, truncation_reason, stats));
    }

    // Stack of frames; each frame owns its remaining edge list.
    let mut stack: Vec<DfsFrame> = Vec::new();
    // current_path tracks (node_id, from_name, edge_kind_into_this_node).
    let mut current_path: Vec<(i64, String, Option<String>)> = Vec::new();
    let mut on_path: HashSet<i64> = HashSet::new();

    // Name cache.
    let mut names: HashMap<i64, String> = HashMap::new();
    let from_name: String = db::get_module_name(conn, from_id)?.unwrap_or_default();
    names.insert(from_id, from_name.clone());

    // Push the root frame.
    let mut root_edges = db::get_outgoing_edges_dedup(conn, from_id, kind_filter)?;
    for (id, name, _) in &root_edges {
        names.insert(*id, name.clone());
    }
    // Reorder edges so any direct edge to `to_id` is processed first. Without
    // this, DFS may exhaust its timeout exploring siblings (alphabetically
    // earlier than the target) before ever recording the direct hit, and the
    // user gets a misleading "no path" result on a graph that obviously has one.
    root_edges.sort_by_key(|(id, _, _)| if *id == to_id { 0 } else { 1 });
    stack.push(DfsFrame {
        node_id: from_id,
        edges: root_edges,
        edge_idx: 0,
    });
    on_path.insert(from_id);
    current_path.push((from_id, from_name, None));

    'outer: loop {
        // Timeout check per frame operation.
        if deadline.elapsed().as_millis() as u64 >= timeout_ms {
            truncated = true;
            truncation_reason = Some("timeout".to_string());
            break;
        }

        let frame_depth = stack.len();
        let frame = match stack.last_mut() {
            Some(f) => f,
            None => break,
        };

        if frame.edge_idx >= frame.edges.len() {
            // All children of this frame explored — backtrack.
            on_path.remove(&frame.node_id);
            current_path.pop();
            stack.pop();
            continue;
        }

        let (child_id, child_name, edge_kind) = frame.edges[frame.edge_idx].clone();
        frame.edge_idx += 1;
        stats.edges_explored += 1;

        if on_path.contains(&child_id) {
            continue; // Cycle: skip.
        }

        // Reverse-BFS pruning: skip children that can't reach to_id within
        // remaining budget. frame_depth = hops already taken (edges in
        // current_path); +1 = this edge to child; +dist_to[child] = remaining
        // hops to to_id. Total path length must be ≤ max_depth.
        match dist_to.get(&child_id) {
            None => continue, // child cannot reach to_id at all.
            Some(&d_child) => {
                if frame_depth.saturating_add(d_child) > max_depth {
                    continue;
                }
            }
        }

        if child_id == to_id {
            // Reaching the cap alone does not prove that paths were omitted.
            // Look for one more complete path without storing it, so an exact
            // fit can finish with truncated=false and memory stays bounded.
            if results.len() >= max_paths {
                truncated = true;
                truncation_reason = Some("max_paths".to_string());
                break 'outer;
            }
            // Found a path — materialise it.
            //
            // Layout of current_path:
            //   index 0  → (root_id,   root_name,   None)           ← no incoming edge
            //   index k  → (node_k_id, node_k_name, Some(kind_k-1→k)) ← kind of edge (k-1)→k
            //
            // So the outgoing edge kind for hop i→(i+1) is stored at
            // current_path[i+1].2 for intermediate hops, and `edge_kind` for
            // the final hop to `child_id` (= to_id).
            let mut hops: Vec<EdgeHop> = Vec::with_capacity(current_path.len());
            for i in 0..current_path.len() {
                let (_node_id, ref node_name, _) = current_path[i];
                let (next_name, kind) = if i + 1 < current_path.len() {
                    // Intermediate hop: next node is already on the path.
                    let next_id = current_path[i + 1].0;
                    let next_n = names.get(&next_id).cloned().unwrap_or_default();
                    // current_path[i+1].2 is the kind of edge i → i+1 (set when
                    // we pushed node i+1 onto the stack).
                    let k = current_path[i + 1].2.clone().unwrap_or_default();
                    (next_n, k)
                } else {
                    // Last hop: destination is child_id (= to_id), found in
                    // this iteration; edge_kind is the outgoing kind from node i.
                    (child_name.clone(), edge_kind.clone())
                };
                hops.push(EdgeHop {
                    from: node_name.clone(),
                    to: next_name,
                    kind,
                });
            }
            let length = hops.len();
            results.push(RoutePath { hops, length });

            continue; // Do not push to_id onto stack — stop here.
        }

        // Depth guard: current depth = stack.len() (number of frames already on stack = path length to frame.node_id)
        if frame_depth >= max_depth {
            continue;
        }

        // Push child onto stack.
        on_path.insert(child_id);
        names.insert(child_id, child_name.clone());
        stats.nodes_visited += 1;

        let mut child_edges = db::get_outgoing_edges_dedup(conn, child_id, kind_filter)?;
        for (id, name, _) in &child_edges {
            names.entry(*id).or_insert_with(|| name.clone());
        }
        child_edges.sort_by_key(|(id, _, _)| if *id == to_id { 0 } else { 1 });

        current_path.push((child_id, child_name, Some(edge_kind)));
        let new_depth = stack.len() + 1;
        if new_depth > stats.max_depth_reached {
            stats.max_depth_reached = new_depth;
        }
        stack.push(DfsFrame {
            node_id: child_id,
            edges: child_edges,
            edge_idx: 0,
        });
    }

    // Sort: shortest first, then lexicographic by hop names for determinism.
    results.sort_by(|a, b| {
        a.length.cmp(&b.length).then_with(|| {
            let a_key: Vec<_> = a.hops.iter().map(|h| h.to.as_str()).collect();
            let b_key: Vec<_> = b.hops.iter().map(|h| h.to.as_str()).collect();
            a_key.cmp(&b_key)
        })
    });

    stats.elapsed_ms = deadline.elapsed().as_millis() as u64;
    Ok((results, truncated, truncation_reason, stats))
}

fn suggested_route_timeout(timeout_ms: u64) -> u64 {
    timeout_ms
        .saturating_mul(2)
        .max(timeout_ms.saturating_add(1000))
        .min(60_000)
}

/// Show dependency path(s) between two modules.
pub fn cmd_module_route(
    root: &Path,
    from: &str,
    to: &str,
    all: bool,
    max_paths: usize,
    max_depth: usize,
    timeout_ms: u64,
    via_kind: &str,
    format: &str,
) -> Result<()> {
    // Validate format early so JSON mode never emits ANSI.
    match format {
        "text" | "json" | "mermaid" | "dot" => {}
        _ => {
            // Unknown format: we cannot emit JSON because we don't know the
            // caller's intent, so emit a plain-text error to stderr.
            eprintln!(
                "{}",
                format!(
                    "Invalid --format '{}'. Use: text, json, mermaid, dot.",
                    format
                )
                .red()
            );
            return Ok(());
        }
    }

    // Validate via_kind.
    match via_kind {
        "api" | "implementation" | "all" => {}
        _ => {
            let msg = format!(
                "Invalid --via-kind '{}'. Use: api, implementation, all.",
                via_kind
            );
            if format == "json" {
                let result = ModuleRouteResult {
                    from: from.to_string(),
                    to: to.to_string(),
                    paths: vec![],
                    count: 0,
                    truncated: false,
                    truncation_reason: None,
                    empty_reason: Some("invalid_args".to_string()),
                    warnings: vec![msg],
                    search_stats: None,
                };
                return render_json(&result);
            }
            eprintln!("{}", msg.red());
            return Ok(());
        }
    }

    if !db::db_exists(root) {
        let msg = "Index not found. Run 'ast-index rebuild' first.";
        if format == "json" {
            let result = ModuleRouteResult {
                from: from.to_string(),
                to: to.to_string(),
                paths: vec![],
                count: 0,
                truncated: false,
                truncation_reason: None,
                empty_reason: Some("index_missing".to_string()),
                warnings: vec![],
                search_stats: None,
            };
            return render_json(&result);
        }
        eprintln!("{}", msg.red());
        return Ok(());
    }

    let conn = open_module_query_db(root)?;

    // Check module_deps populated.
    if module_graph_is_unindexed(&conn)? {
        let msg = "Module dependencies not indexed. Run 'ast-index rebuild'.";
        if format == "json" {
            let result = ModuleRouteResult {
                from: from.to_string(),
                to: to.to_string(),
                paths: vec![],
                count: 0,
                truncated: false,
                truncation_reason: None,
                empty_reason: Some("not_indexed".to_string()),
                warnings: vec![],
                search_stats: None,
            };
            return render_json(&result);
        }
        eprintln!("{}", msg.yellow());
        return Ok(());
    }

    // Multi-root warning.
    let extra_roots = db::get_extra_roots(&conn)?;
    if !extra_roots.is_empty() {
        eprintln!(
            "{}",
            "Multi-root project detected; module-route v1 only walks the primary root graph."
                .yellow()
        );
    }

    // Staleness warning.
    let mut warnings: Vec<String> = Vec::new();
    if let Ok(Some((indexed_at, updated_at))) = db::get_modules_index_freshness(&conn) {
        if updated_at > indexed_at {
            warnings.push("index_may_be_stale".to_string());
        }
    }

    // Resolve module ids.
    let from_id = selected_module(&conn, root, from)?.map(|(id, _)| id);
    let to_id = selected_module(&conn, root, to)?.map(|(id, _)| id);

    let kind_filter: Option<&str> = if via_kind == "all" {
        None
    } else {
        Some(via_kind)
    };
    let deadline = Instant::now();

    // Self-query: when from == to, check whether a real self-edge exists in
    // the DB. If it does, traverse it as a proper 1-hop cycle path. Otherwise
    // return empty with empty_reason="self" so callers can distinguish.
    if let (Some(fid), Some(_tid)) = (from_id, to_id) {
        if fid == _tid {
            if let Some(real_kind) = db::get_module_self_edge_kind(&conn, fid, kind_filter)? {
                // Real self-loop: surface the actual dep_kind from the DB,
                // while honoring the same budgets as every other 1-hop path.
                let truncation_reason = if all && max_paths == 0 {
                    Some("max_paths".to_string())
                } else if deadline.elapsed().as_millis() as u64 >= timeout_ms {
                    Some("timeout".to_string())
                } else {
                    None
                };
                let truncated = truncation_reason.is_some();
                let empty_reason = if let Some(reason) = &truncation_reason {
                    Some(format!("truncated_{}", reason))
                } else if max_depth == 0 {
                    Some("unreachable".to_string())
                } else {
                    None
                };
                let name = db::get_module_name(&conn, fid)?.unwrap_or_else(|| from.to_string());
                let hop = EdgeHop {
                    from: name.clone(),
                    to: name,
                    kind: real_kind,
                };
                let result = ModuleRouteResult {
                    from: from.to_string(),
                    to: to.to_string(),
                    paths: if empty_reason.is_none() {
                        vec![RoutePath {
                            hops: vec![hop],
                            length: 1,
                        }]
                    } else {
                        vec![]
                    },
                    count: usize::from(empty_reason.is_none()),
                    truncated,
                    search_stats: truncated.then(|| SearchStats {
                        nodes_visited: 0,
                        edges_explored: 0,
                        elapsed_ms: deadline.elapsed().as_millis() as u64,
                        max_depth_reached: 0,
                        timeout_ms,
                        suggested_timeout_ms: (truncation_reason.as_deref() == Some("timeout"))
                            .then(|| suggested_route_timeout(timeout_ms)),
                    }),
                    truncation_reason,
                    empty_reason,
                    warnings,
                };
                return dispatch_render(format, &result);
            }
            // No self-edge: trivial "same module" answer.
            let result = ModuleRouteResult {
                from: from.to_string(),
                to: to.to_string(),
                paths: vec![],
                count: 0,
                truncated: false,
                truncation_reason: None,
                empty_reason: Some("self".to_string()),
                warnings,
                search_stats: None,
            };
            return dispatch_render(format, &result);
        }
    }

    // Missing module handling.
    let (fid, tid) = match (from_id, to_id) {
        (None, _) => {
            let result = ModuleRouteResult {
                from: from.to_string(),
                to: to.to_string(),
                paths: vec![],
                count: 0,
                truncated: false,
                truncation_reason: None,
                empty_reason: Some("missing_module_from".to_string()),
                warnings,
                search_stats: None,
            };
            return dispatch_render(format, &result);
        }
        (_, None) => {
            let result = ModuleRouteResult {
                from: from.to_string(),
                to: to.to_string(),
                paths: vec![],
                count: 0,
                truncated: false,
                truncation_reason: None,
                empty_reason: Some("missing_module_to".to_string()),
                warnings,
                search_stats: None,
            };
            return dispatch_render(format, &result);
        }
        (Some(f), Some(t)) => (f, t),
    };

    // Run BFS/DFS.
    let (paths, truncated, truncation_reason, empty_reason, search_stats) = if all {
        let (paths, truncated, trunc_reason, dfs_stats) = dfs_all_paths(
            &conn,
            fid,
            tid,
            kind_filter,
            max_depth,
            max_paths,
            deadline,
            timeout_ms,
        )?;
        let reason = if paths.is_empty() {
            // Truncated (timeout / prune_timeout / max_paths) wins over
            // reachability — saying "no path" when the search was cut short
            // is a lie.
            if truncated {
                // prune_timeout gets its own value so callers can distinguish
                // "we ran DFS but timed out" from "the pruning phase itself
                // was too slow to finish".
                let tag = trunc_reason.as_deref().unwrap_or("limit");
                Some(format!("truncated_{}", tag))
            } else {
                let reachable = if kind_filter.is_some() {
                    matches!(
                        bfs_shortest(&conn, fid, tid, None, max_depth, deadline, timeout_ms)?,
                        BfsOutcome::Found(_)
                    )
                } else {
                    false
                };
                if reachable {
                    Some("kind_filter".to_string())
                } else {
                    Some("unreachable".to_string())
                }
            }
        } else {
            None
        };
        // Suggest a higher --timeout-ms for both DFS timeout and prune timeout.
        let suggested_timeout_ms = if truncated
            && matches!(
                trunc_reason.as_deref(),
                Some("timeout") | Some("prune_timeout")
            ) {
            // Heuristic: 2× current, rounded up to next second. Capped at 60s
            // to keep the suggestion sane on pathological graphs.
            Some(suggested_route_timeout(timeout_ms))
        } else {
            None
        };
        let stats = SearchStats {
            nodes_visited: dfs_stats.nodes_visited,
            edges_explored: dfs_stats.edges_explored,
            elapsed_ms: dfs_stats.elapsed_ms,
            max_depth_reached: dfs_stats.max_depth_reached,
            timeout_ms,
            suggested_timeout_ms,
        };
        (paths, truncated, trunc_reason, reason, Some(stats))
    } else {
        let outcome = bfs_shortest(
            &conn,
            fid,
            tid,
            kind_filter,
            max_depth,
            deadline,
            timeout_ms,
        )?;
        match outcome {
            BfsOutcome::Found(p) => (vec![p], false, None, None, None),
            BfsOutcome::TimedOut => {
                // Shortest-mode timeout must NOT collapse to "unreachable" —
                // we don't know whether a path exists. Report as truncated,
                // mirroring the --all behaviour, so callers can retry with a
                // larger --timeout-ms.
                let suggested_timeout_ms = Some(suggested_route_timeout(timeout_ms));
                let stats = SearchStats {
                    nodes_visited: 0,
                    edges_explored: 0,
                    elapsed_ms: deadline.elapsed().as_millis() as u64,
                    max_depth_reached: 0,
                    timeout_ms,
                    suggested_timeout_ms,
                };
                (
                    vec![],
                    true,
                    Some("timeout".to_string()),
                    Some("truncated_timeout".to_string()),
                    Some(stats),
                )
            }
            BfsOutcome::NotFound => {
                let reason = if kind_filter.is_some() {
                    // Check without filter; reuse the original deadline so the
                    // total work stays within the user-specified budget.
                    let outcome_unfiltered =
                        bfs_shortest(&conn, fid, tid, None, max_depth, deadline, timeout_ms)?;
                    match outcome_unfiltered {
                        BfsOutcome::Found(_) => Some("kind_filter".to_string()),
                        // Treat a follow-up timeout as unreachable here: the
                        // primary attempt already returned NotFound, so the
                        // caller knows the kind-filtered path is absent; the
                        // unfiltered probe is best-effort.
                        BfsOutcome::NotFound | BfsOutcome::TimedOut => {
                            Some("unreachable".to_string())
                        }
                    }
                } else {
                    Some("unreachable".to_string())
                };
                (vec![], false, None, reason, None)
            }
        }
    };

    let count = paths.len();
    let result = ModuleRouteResult {
        from: from.to_string(),
        to: to.to_string(),
        paths,
        count,
        truncated,
        truncation_reason,
        empty_reason,
        warnings,
        search_stats,
    };

    dispatch_render(format, &result)
}

fn dispatch_render(format: &str, result: &ModuleRouteResult) -> Result<()> {
    match format {
        "json" => render_json(result),
        "mermaid" => {
            render_mermaid(result);
            Ok(())
        }
        "dot" => {
            render_dot(result);
            Ok(())
        }
        _ => {
            render_text(result);
            Ok(())
        }
    }
}

/// Per-scan deduplication with a bounded page cache. An empty SQLite filename
/// creates a private temporary database that spills to disk and disappears on
/// close. It never modifies the project index or retains a project-sized set
/// of symbol names in Rust memory.
struct UsedDependencySymbols {
    connection: Connection,
}

impl UsedDependencySymbols {
    fn new() -> Result<Self> {
        let connection = Connection::open("")?;
        // The data is disposable, so journaling/durability are unnecessary.
        // One transaction avoids per-symbol commits; cache spilling remains
        // enabled even while the transaction is open.
        connection.execute_batch(
            "PRAGMA cache_size=-2048;
             PRAGMA cache_spill=ON;
             PRAGMA journal_mode=OFF;
             PRAGMA synchronous=OFF;
             CREATE TABLE used_symbols(name TEXT PRIMARY KEY) WITHOUT ROWID;
             BEGIN;",
        )?;
        Ok(Self { connection })
    }

    fn insert(&self, name: &str) -> Result<()> {
        self.connection
            .prepare_cached("INSERT OR IGNORE INTO used_symbols(name) VALUES(?1)")?
            .execute(params![name])?;
        Ok(())
    }

    fn summary(&self) -> Result<(usize, Vec<String>)> {
        let count = self
            .connection
            .query_row("SELECT count(*) FROM used_symbols", [], |row| row.get(0))?;
        let mut statement = self
            .connection
            .prepare("SELECT name FROM used_symbols ORDER BY name LIMIT 3")?;
        let samples = statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok((count, samples))
    }
}

/// Check accessible import declarations with one bounded source tree at a time.
fn java_dependency_declaration(
    conn: &Connection,
    root: &Path,
    resolver: &super::PathResolver,
    qualified: &str,
    accessing_package: &str,
) -> Result<Option<crate::parsers::treesitter::java::DependencyImportDeclaration>> {
    let mut statement = conn.prepare_cached(
        "SELECT DISTINCT f.path,f.root_path FROM symbols s JOIN files f ON s.file_id=f.id
         JOIN temp.java_dependency_scope scope ON scope.file_id=f.id
         WHERE substr(f.path,-5)='.java' AND s.qualified_name=?1
         AND s.kind IN ('class','interface','enum')
         ORDER BY scope.is_consumer DESC,f.path,f.root_path LIMIT 1",
    )?;
    let mut rows = statement.query(params![qualified])?;
    if let Some(row) = rows.next()? {
        let path: String = row.get(0)?;
        let owner: String = row.get(1)?;
        let content = super::grep::read_java_syntax_source(
            &root.join(resolver.resolve_with_root_raw(&path, Some(&owner))),
            crate::indexer::max_file_size_bytes(),
        )?;
        if let Some(declaration) = crate::parsers::treesitter::java::dependency_import_declaration(
            &content,
            qualified,
            accessing_package,
        )? {
            return Ok(Some(declaration));
        }
    }
    Ok(None)
}

struct JavaDependencyType {
    identity: String,
    declaration: crate::parsers::treesitter::java::DependencyImportDeclaration,
}

/// Check inherited aliases without borrowing another root's classpath metadata.
#[derive(Clone, Copy)]
struct JavaDependencyLookup<'a> {
    conn: &'a Connection,
    root: &'a Path,
    resolver: &'a super::PathResolver,
    package: &'a str,
    contexts: &'a [String],
    local_types: &'a std::collections::BTreeMap<
        String,
        crate::parsers::treesitter::java::DependencyImportDeclaration,
    >,
    local_bindings: &'a [(String, String, std::ops::Range<usize>)],
    resolving_parent: bool,
}

impl JavaDependencyLookup<'_> {
    fn raw(&self, name: &str) -> Result<Option<JavaDependencyType>> {
        if let Some(declaration) = self.local_types.get(name) {
            return Ok(Some(JavaDependencyType {
                identity: name.to_owned(),
                declaration: declaration.clone(),
            }));
        }
        Ok(
            java_dependency_declaration(self.conn, self.root, self.resolver, name, self.package)?
                .map(|declaration| JavaDependencyType {
                    identity: name.to_owned(),
                    declaration,
                }),
        )
    }

    fn subclass(&self, name: &str, base: &str, visiting: &mut HashSet<String>) -> Result<bool> {
        if name == base {
            return Ok(true);
        }
        if visiting.len() >= 128 || !visiting.insert(name.to_owned()) {
            return Ok(false);
        }
        if let Some(owner) = self.raw(name)? {
            for parent in self.parents(&owner)? {
                if self.subclass(&parent.identity, base, visiting)? {
                    visiting.remove(name);
                    return Ok(true);
                }
            }
        }
        visiting.remove(name);
        Ok(false)
    }

    fn protected_access(&self, owner: &str) -> Result<bool> {
        for context in self.contexts {
            if self.subclass(context, owner, &mut HashSet::new())? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn private_access(&self, name: &str, package: &str) -> bool {
        let prefix = if package.is_empty() {
            String::new()
        } else {
            format!("{package}.")
        };
        let Some(nest) = name
            .strip_prefix(&prefix)
            .and_then(|name| name.split('.').next())
        else {
            return false;
        };
        self.contexts.iter().any(|context| {
            context
                .strip_prefix(&prefix)
                .and_then(|context| context.split('.').next())
                == Some(nest)
        })
    }

    fn direct(&self, name: &str) -> Result<Option<JavaDependencyType>> {
        let Some(mut found) = self.raw(name)? else {
            return Ok(None);
        };
        if !found.declaration.accessible {
            let mut allowed = true;
            for (owner, protected) in &found.declaration.access_barriers {
                allowed &= self.private_access(owner, &found.declaration.package)
                    || *protected && self.protected_access(owner)?;
            }
            found.declaration.accessible = allowed;
        }
        if found.declaration.protected_member {
            if let Some((owner, _)) = name.rsplit_once('.') {
                found.declaration.member_accessible |= self.protected_access(owner)?;
            }
        }
        if !self.resolving_parent
            && !found.declaration.protected_names.is_empty()
            && self.protected_access(name)?
        {
            found
                .declaration
                .static_names
                .extend(found.declaration.protected_names.iter().cloned());
        }
        if !self.resolving_parent
            && !found.declaration.protected_instance_names.is_empty()
            && self.protected_access(name)?
        {
            found
                .declaration
                .instance_names
                .extend(found.declaration.protected_instance_names.iter().cloned());
        }
        // Private members are available inside the declaring top-level nest,
        // including nested/local captures; they are never inherited by a child.
        if !self.resolving_parent && self.private_access(name, &found.declaration.package) {
            found
                .declaration
                .static_names
                .extend(found.declaration.private_static_names.iter().cloned());
            found
                .declaration
                .instance_names
                .extend(found.declaration.private_instance_names.iter().cloned());
        }
        Ok(Some(found))
    }

    fn lexical_type(&self, name: &str) -> Result<(bool, Option<JavaDependencyType>)> {
        let first = name.split('.').next().unwrap_or(name);
        for context in self.contexts {
            if let Some(owner) = self.direct(context)? {
                // A lexical declaration or ambiguous inherited name reserves
                // this namespace even when access/member lookup fails.
                if !self
                    .inherited_type(&owner, first, &mut HashSet::new())?
                    .is_empty()
                {
                    return Ok((true, self.type_name(&format!("{context}.{name}"), true)?));
                }
            }
        }
        Ok((false, None))
    }

    fn lexical_member(
        &self,
        member: &(String, bool),
        instances: Option<&std::collections::BTreeSet<String>>,
    ) -> Result<Option<String>> {
        for context in self.contexts {
            if let Some(owner) = self.direct(context)? {
                // A declaration in this lexical owner blocks outer/import lookup,
                // even when the inherited candidate is inaccessible or ambiguous.
                let matches = self.member_origins(
                    &owner,
                    member,
                    false,
                    instances.is_some_and(|owners| owners.contains(context)),
                    &mut HashSet::new(),
                )?;
                if !matches.is_empty() {
                    return Ok((matches.len() == 1).then(|| matches.into_iter().next().unwrap()));
                }
                if owner.declaration.declared_names.contains(member) {
                    return Ok(None);
                }
            }
        }
        Ok(None)
    }

    fn parents(&self, owner: &JavaDependencyType) -> Result<Vec<JavaDependencyType>> {
        let mut result = Vec::new();
        // Binding a superclass must not ask for protected member augmentation:
        // that augmentation itself needs this inheritance relationship. The
        // current class's body is also outside its own superclass header.
        let contexts: Vec<_> = self
            .contexts
            .iter()
            .filter(|context| {
                *context != &owner.identity && !context.starts_with(&format!("{}.", owner.identity))
            })
            .cloned()
            .collect();
        let binding = JavaDependencyLookup {
            contexts: &contexts,
            resolving_parent: true,
            ..*self
        };
        for name in &owner.declaration.parents {
            let first = name.split('.').next().unwrap_or(name);
            let suffix = &name[first.len()..];
            // A local superclass is bound where this owner is declared, not
            // by a canonical import name or a file-wide spelling match.
            let position = owner
                .identity
                .rsplit_once('@')
                .and_then(|(_, site)| site.split('.').next()?.parse::<usize>().ok());
            let local = position.and_then(|position| {
                self.local_bindings
                    .iter()
                    .filter(|(simple, _, scope)| simple == first && scope.contains(&position))
                    .min_by_key(|(_, _, scope)| scope.end - scope.start)
            });
            if let Some((_, identity, _)) = local {
                if let Some(parent) = binding.type_name(&format!("{identity}{suffix}"), true)? {
                    result.push(parent);
                }
                continue;
            }
            if position.is_some() {
                let mut prefix = owner.identity.as_str();
                let mut lexical = None;
                while let Some((outer, _)) = prefix.rsplit_once('.') {
                    if outer == owner.declaration.package {
                        break;
                    }
                    if let Some(parent) = binding.type_name(&format!("{outer}.{name}"), true)? {
                        lexical = Some(parent);
                        break;
                    }
                    prefix = outer;
                }
                if let Some(parent) = lexical {
                    result.push(parent);
                    continue;
                }
            }
            let explicit = owner.declaration.imports.iter().find(|(import, _)| {
                !import.ends_with(".*") && import.rsplit('.').next() == Some(first)
            });
            let mut candidates = Vec::new();
            if let Some((import, _)) = explicit {
                candidates.push(format!("{import}{suffix}"));
            } else {
                // Lexical enclosing types and the package precede on-demand imports.
                let mut prefix = owner.identity.as_str();
                while let Some((outer, _)) = prefix.rsplit_once('.') {
                    if outer == owner.declaration.package {
                        break;
                    }
                    candidates.push(format!("{outer}.{name}"));
                    prefix = outer;
                }
                candidates.push(if owner.declaration.package.is_empty() {
                    name.clone()
                } else {
                    format!("{}.{name}", owner.declaration.package)
                });
                candidates.push(name.clone());
            }
            let mut found = None;
            for candidate in candidates {
                let mut parent = self.raw(&candidate)?;
                if parent.is_none()
                    && (candidate == *name
                        || explicit.is_some_and(|(_, is_static)| *is_static || !suffix.is_empty()))
                {
                    parent = binding.type_name(&candidate, true)?;
                }
                if let Some(mut parent) = parent {
                    if owner.identity.contains('@') && !parent.declaration.accessible {
                        let Some(accessible) = binding.direct(&parent.identity)? else {
                            break;
                        };
                        if !accessible.declaration.accessible {
                            break;
                        }
                        parent = accessible;
                    }
                    if explicit.is_some_and(|(_, is_static)| *is_static)
                        && suffix.is_empty()
                        && !parent.declaration.static_member
                    {
                        break;
                    }
                    found = Some(parent);
                    break;
                }
            }
            if found.is_none() && explicit.is_none() {
                let mut matches = std::collections::BTreeMap::new();
                for (import, is_static) in &owner.declaration.imports {
                    if let Some(prefix) = import.strip_suffix(".*") {
                        if let Some(parent) =
                            binding.type_name(&format!("{prefix}.{name}"), *is_static)?
                        {
                            if *is_static && !parent.declaration.static_member {
                                continue;
                            }
                            matches.insert(parent.identity.clone(), parent);
                        }
                    }
                }
                if matches.len() == 1 {
                    found = matches.into_values().next();
                }
            }
            if let Some(parent) = found {
                result.push(parent);
            }
        }
        Ok(result)
    }

    fn inherited_type(
        &self,
        owner: &JavaDependencyType,
        member: &str,
        visiting: &mut HashSet<String>,
    ) -> Result<Vec<JavaDependencyType>> {
        if visiting.len() >= 128 || !visiting.insert(owner.identity.clone()) {
            return Ok(Vec::new());
        }
        let mut matches = std::collections::BTreeMap::new();
        if let Some(mut direct) = self.direct(&format!("{}.{member}", owner.identity))? {
            // A direct inaccessible declaration still hides all parent candidates.
            direct.declaration.accessible = direct.declaration.member_accessible;
            matches.insert(direct.identity.clone(), direct);
        } else {
            for parent in self.parents(owner)? {
                for found in self.inherited_type(&parent, member, visiting)? {
                    if found.declaration.accessible
                        && (!found.declaration.package_member
                            || found.declaration.package == owner.declaration.package)
                    {
                        matches.insert(found.identity.clone(), found);
                    }
                }
            }
        }
        visiting.remove(&owner.identity);
        Ok(matches.into_values().collect())
    }

    fn type_name(&self, name: &str, aliases: bool) -> Result<Option<JavaDependencyType>> {
        self.type_name_inner(name, aliases, 0)
    }

    fn type_name_inner(
        &self,
        name: &str,
        aliases: bool,
        depth: usize,
    ) -> Result<Option<JavaDependencyType>> {
        if depth >= 128 {
            return Ok(None);
        }
        if let Some(found) = self.direct(name)? {
            return Ok(found.declaration.accessible.then_some(found));
        }
        if !aliases {
            return Ok(None);
        }
        let Some((prefix, member)) = name.rsplit_once('.') else {
            return Ok(None);
        };
        let Some(owner) = self.type_name_inner(prefix, true, depth + 1)? else {
            return Ok(None);
        };
        let mut matches = self.inherited_type(&owner, member, &mut HashSet::new())?;
        // Distinct declarations remain ambiguous, including inaccessible barriers.
        if matches.len() != 1 {
            return Ok(None);
        }
        let found = matches.pop().unwrap();
        Ok(found.declaration.accessible.then_some(found))
    }

    fn member_origins(
        &self,
        owner: &JavaDependencyType,
        member: &(String, bool),
        inherited: bool,
        allow_instance: bool,
        visiting: &mut HashSet<String>,
    ) -> Result<std::collections::BTreeSet<String>> {
        let mut matches = std::collections::BTreeSet::new();
        if visiting.len() >= 128 || !visiting.insert(owner.identity.clone()) {
            return Ok(matches);
        }
        let refreshed = self.direct(&owner.identity)?;
        let owner = refreshed.as_ref().unwrap_or(owner);
        if owner.declaration.declared_names.contains(member) {
            let private_inherited = inherited
                && (owner.declaration.private_static_names.contains(member)
                    || owner.declaration.private_instance_names.contains(member));
            if !private_inherited
                && ((owner.declaration.static_names.contains(member)
                    && !(inherited && member.1 && owner.declaration.interface))
                    || allow_instance && owner.declaration.instance_names.contains(member))
            {
                matches.insert(owner.identity.clone());
            }
        } else {
            for parent in self.parents(owner)? {
                for identity in
                    self.member_origins(&parent, member, true, allow_instance, visiting)?
                {
                    if let Some(found) = self.direct(&identity)? {
                        if found.declaration.package_names.contains(member)
                            && found.declaration.package != owner.declaration.package
                        {
                            continue;
                        }
                    }
                    matches.insert(identity);
                }
            }
        }
        visiting.remove(&owner.identity);
        Ok(matches)
    }

    fn static_member(&self, owner: &str, member: &(String, bool)) -> Result<Option<String>> {
        let Some(owner) = self.type_name(owner, true)? else {
            return Ok(None);
        };
        let mut matches = self.member_origins(&owner, member, false, false, &mut HashSet::new())?;
        Ok(if matches.len() == 1 {
            matches.pop_first()
        } else {
            None
        })
    }

    /// Bind a receiver type at its declaration site within the selected classpath.
    fn value_type(
        &self,
        name: &str,
        position: usize,
        imports: &[(String, bool)],
    ) -> Result<Option<JavaDependencyType>> {
        let first = name.split('.').next().unwrap_or(name);
        let suffix = &name[first.len()..];
        if let Some((_, identity, _)) = self
            .local_bindings
            .iter()
            .filter(|(simple, _, range)| simple == first && range.contains(&position))
            .min_by_key(|(_, _, range)| range.end - range.start)
        {
            return self.type_name(&format!("{identity}{suffix}"), true);
        }
        let (bound, found) = self.lexical_type(name)?;
        if bound {
            return Ok(found);
        }
        for context in self.contexts {
            if let Some(found) = self.type_name(&format!("{context}.{name}"), true)? {
                return Ok(Some(found));
            }
        }
        if let Some((import, is_static)) = imports
            .iter()
            .find(|(import, _)| !import.ends_with(".*") && import.rsplit('.').next() == Some(first))
        {
            let found = self.type_name(
                &format!("{import}{suffix}"),
                *is_static || !suffix.is_empty(),
            )?;
            return Ok(found.filter(|found| {
                !is_static || !suffix.is_empty() || found.declaration.static_member
            }));
        }
        let local = if self.package.is_empty() {
            name.to_owned()
        } else {
            format!("{}.{name}", self.package)
        };
        if let Some(found) = self.type_name(&local, true)? {
            return Ok(Some(found));
        }
        if name.contains('.') {
            if let Some(found) = self.type_name(name, true)? {
                return Ok(Some(found));
            }
        }
        let mut matches = std::collections::BTreeMap::new();
        for (import, is_static) in imports {
            if let Some(prefix) = import.strip_suffix(".*") {
                if let Some(found) = self.type_name(&format!("{prefix}.{name}"), *is_static)? {
                    if !is_static || found.declaration.static_member {
                        matches.insert(found.identity.clone(), found);
                    }
                }
            }
        }
        Ok(if matches.len() == 1 {
            matches.into_values().next()
        } else {
            None
        })
    }

    fn value_member(
        &self,
        owner: &JavaDependencyType,
        member: &(String, bool),
    ) -> Result<Option<String>> {
        let mut matches = self.member_origins(owner, member, false, true, &mut HashSet::new())?;
        if matches.len() != 1 {
            return Ok(None);
        }
        let identity = matches.pop_first().unwrap();
        if let Some(declaration) = self.raw(&identity)? {
            // Cross-package protected instance access additionally constrains
            // the qualifier to the accessing subclass (JLS 6.6.2.1).
            if declaration.declaration.package != self.package
                && declaration
                    .declaration
                    .protected_instance_names
                    .contains(member)
            {
                let mut allowed = false;
                for context in self.contexts {
                    if self.subclass(context, &identity, &mut HashSet::new())?
                        && self.subclass(&owner.identity, context, &mut HashSet::new())?
                    {
                        allowed = true;
                        break;
                    }
                }
                if !allowed {
                    return Ok(None);
                }
            }
        }
        Ok(Some(identity))
    }

    /// Bind nominal chains without guessing an inaccessible or overloaded result.
    fn value_receiver(
        &self,
        receiver: &super::graph::DependencyValueReceiver,
        imports: &[(String, bool)],
        identities: &mut std::collections::BTreeSet<String>,
        depth: usize,
    ) -> Result<Option<(JavaDependencyType, bool)>> {
        use super::graph::DependencyValueReceiver;
        if depth >= 16 {
            return Ok(None);
        }
        match receiver {
            DependencyValueReceiver::Nominal {
                path,
                position,
                contexts,
                instance,
            } => {
                let binding = JavaDependencyLookup { contexts, ..*self };
                Ok(binding
                    .value_type(path, *position, imports)?
                    .map(|owner| (owner, *instance)))
            }
            DependencyValueReceiver::Lexical { instances, .. } => Ok(match self.contexts.first() {
                Some(context) => self
                    .raw(context)?
                    .map(|owner| (owner, instances.contains(context))),
                None => None,
            }),
            DependencyValueReceiver::Super => {
                let Some(context) = self.contexts.first() else {
                    return Ok(None);
                };
                let Some(owner) = self.raw(context)? else {
                    return Ok(None);
                };
                let mut parents = self
                    .parents(&owner)?
                    .into_iter()
                    .filter(|parent| !parent.declaration.interface);
                let Some(parent) = parents.next() else {
                    return Ok(None);
                };
                Ok(parents.next().is_none().then_some((parent, true)))
            }
            DependencyValueReceiver::Member {
                receiver,
                name,
                arity,
            } => {
                let member = (name.clone(), arity.is_some());
                let identity = if let DependencyValueReceiver::Lexical {
                    instances,
                    explicit,
                } = receiver.as_ref()
                {
                    // An enclosing callable's result remains available inside
                    // captures, but a hiding declaration reserves the name.
                    let mut found = None;
                    for context in
                        self.contexts
                            .iter()
                            .take(if *explicit { 1 } else { self.contexts.len() })
                    {
                        let instance = instances.contains(context);
                        if let Some(owner) = self.raw(context)? {
                            let candidates = self.member_origins(
                                &owner,
                                &member,
                                false,
                                instance,
                                &mut HashSet::new(),
                            )?;
                            if !candidates.is_empty()
                                || owner.declaration.declared_names.contains(&member)
                            {
                                found = if instance {
                                    self.value_member(&owner, &member)?
                                } else {
                                    self.static_member(&owner.identity, &member)?
                                };
                                break;
                            }
                        }
                    }
                    found
                } else {
                    let Some((owner, instance)) =
                        self.value_receiver(receiver, imports, identities, depth + 1)?
                    else {
                        return Ok(None);
                    };
                    if instance {
                        self.value_member(&owner, &member)?
                    } else {
                        self.static_member(&owner.identity, &member)?
                    }
                };
                let Some(identity) = identity else {
                    return Ok(None);
                };
                let Some(owner) = self.raw(&identity)? else {
                    return Ok(None);
                };
                let Some(signatures) = owner.declaration.value_types.get(&member) else {
                    return Ok(None);
                };
                let mut signatures = signatures
                    .iter()
                    .filter(|signature| arity.is_none() || signature.arity == *arity);
                let Some(signature) = signatures.next() else {
                    return Ok(None);
                };
                if signatures.next().is_some() {
                    return Ok(None);
                }
                let Some(path) = &signature.path else {
                    return Ok(None);
                };
                let binding = JavaDependencyLookup {
                    contexts: &signature.contexts,
                    package: &owner.declaration.package,
                    ..*self
                };
                // Byte coordinates from another file cannot bind a local
                // declaration in the consumer merely because offsets collide.
                let position = if identity.contains('@') {
                    signature.position
                } else {
                    usize::MAX
                };
                let found = binding.value_type(path, position, &owner.declaration.imports)?;
                if found.is_some() {
                    identities.insert(identity);
                }
                Ok(found.map(|owner| (owner, true)))
            }
        }
    }
}

/// Filter Java resource candidates through the consumer's source classpath.
/// Generated R stubs need not be indexed; authored names still take precedence.
pub(crate) fn java_resource_references(
    conn: &Connection,
    root: &Path,
    module: Option<i64>,
    content: &str,
    namespaces: &HashMap<String, Vec<i64>>,
) -> Result<Vec<crate::parsers::treesitter::java::ResourceReference>> {
    use crate::parsers::treesitter::java::{
        dependency_contexts, dependency_syntax, resource_references,
    };
    let references = resource_references(content)?;
    if references.is_empty() {
        return Ok(references);
    }
    conn.execute_batch(
        "CREATE TEMP TABLE IF NOT EXISTS java_dependency_scope(
             file_id INTEGER PRIMARY KEY, is_consumer INTEGER NOT NULL);
         DELETE FROM temp.java_dependency_scope;",
    )?;
    let membership = MODULE_FILE_SCOPE.replace("?1", "m.path");
    conn.execute(
        &format!(
            "WITH RECURSIVE consumer(id) AS (SELECT ?1), classpath(id) AS (
                 SELECT id FROM consumer
                 UNION SELECT d.dep_module_id FROM module_deps d JOIN consumer c ON c.id=d.module_id
                 UNION SELECT d.dep_module_id FROM module_deps d JOIN classpath c ON c.id=d.module_id
                       WHERE d.dep_kind='api'
             )
             INSERT INTO temp.java_dependency_scope
             SELECT f.id,EXISTS(SELECT 1 FROM consumer c WHERE c.id=m.id)
             FROM files f JOIN modules m ON (m.root_path='' OR m.root_path=f.root_path)
             JOIN classpath c ON c.id=m.id WHERE {membership} AND substr(f.path,-5)='.java'"
        ),
        params![module],
    )?;
    // Standalone Java projects have no module classpath to select.
    if module.is_none() {
        conn.execute("INSERT OR IGNORE INTO temp.java_dependency_scope SELECT id,1 FROM files WHERE substr(path,-5)='.java'", [])?;
    }
    let syntax = dependency_syntax(content)?;
    let resolver = super::PathResolver::try_from_conn(root, conn)?.with_decoration(false);
    let language = tree_sitter_java::LANGUAGE.into();
    let tree = crate::parsers::treesitter::parse_tree(content, &language)?;
    let mut output = Vec::new();
    for mut reference in references {
        let Some(node) = tree
            .root_node()
            .descendant_for_byte_range(reference.offset, reference.offset + 1)
        else {
            continue;
        };
        let contexts = dependency_contexts(node, content, &syntax.package);
        let lookup = JavaDependencyLookup {
            conn,
            root,
            resolver: &resolver,
            package: &syntax.package,
            contexts: &contexts,
            local_types: &syntax.local_types,
            local_bindings: &syntax.local_bindings,
            resolving_parent: false,
        };
        let field = (reference.qualifier.clone(), false);
        if lookup
            .lexical_member(
                &field,
                syntax.instance_contexts.get(&(
                    reference.qualifier.clone(),
                    false,
                    contexts.clone(),
                )),
            )?
            .is_some()
            || (reference.qualifier != reference.name
                && lookup.lexical_type(&reference.qualifier)?.0)
        {
            continue;
        }
        // Only an imported field reserves the expression namespace. A method
        // of the same name must leave generated static constants available.
        let mut explicit_field = false;
        let explicit_resource_field = reference.qualifier == reference.name
            && syntax.imports.iter().any(|(import, is_static)| {
                *is_static
                    && reference.namespace.as_ref().is_some_and(|namespace| {
                        import
                            == &format!(
                                "{namespace}.R.{}.{}",
                                reference.resource_type, reference.name
                            )
                    })
            });
        for (import, is_static) in &syntax.imports {
            if *is_static && !explicit_resource_field {
                if let Some(owner) = import.strip_suffix(".*") {
                    let resource_owner = reference.namespace.as_ref().is_some_and(|namespace| {
                        owner == format!("{namespace}.R.{}", reference.resource_type)
                    });
                    if !resource_owner && lookup.static_member(owner, &field)?.is_some() {
                        explicit_field = true;
                    }
                }
            }
            if *is_static && !import.ends_with(".*") {
                if let Some((owner, name)) = import.rsplit_once('.') {
                    let resource_import = reference.namespace.as_ref().is_some_and(|namespace| {
                        import
                            == &format!(
                                "{namespace}.R.{}.{}",
                                reference.resource_type, reference.name
                            )
                    });
                    if name == reference.qualifier
                        && !resource_import
                        && lookup.static_member(owner, &field)?.is_some()
                    {
                        explicit_field = true;
                    }
                }
            }
        }
        if explicit_field {
            continue;
        }
        // A single type import precedes same-package types. Otherwise a
        // cross-file authored R/string type beats generated on-demand types.
        let explicit_type = syntax.imports.iter().any(|(import, is_static)| {
            !is_static && import.rsplit('.').next() == Some(reference.qualifier.as_str())
        });
        if !explicit_type && reference.qualifier != reference.name {
            let candidate = if syntax.package.is_empty() {
                reference.qualifier.clone()
            } else {
                format!("{}.{}", syntax.package, reference.qualifier)
            };
            if lookup.type_name(&candidate, true)?.is_some()
                && (reference.qualifier != "R" || !namespaces.contains_key(&syntax.package))
            {
                continue;
            }
        }
        if reference.namespace.is_none() && reference.qualifier == "R" {
            if namespaces.contains_key(&syntax.package) {
                reference.namespace = Some(syntax.package.clone());
            } else {
                let imported: std::collections::BTreeSet<_> = syntax
                    .imports
                    .iter()
                    .filter(|(_, is_static)| !is_static)
                    .filter_map(|(import, _)| import.strip_suffix(".*"))
                    .filter(|namespace| namespaces.contains_key(*namespace))
                    .collect();
                if imported.len() > 1 {
                    continue;
                }
                reference.namespace = imported.into_iter().next().map(str::to_owned);
            }
        }
        output.push(reference);
    }
    Ok(output)
}

/// Check dependency identities using Java syntax and other languages' indexed refs.
fn count_symbols_used_in_module(
    conn: &Connection,
    root: &Path,
    dependency_path: &str,
    module_path: &str,
    dependency_root: &str,
    module_root: &str,
) -> Result<(usize, Vec<String>)> {
    let used = UsedDependencySymbols::new()?;

    // Bind declarations to the selected consumer's classpath before reading
    // accessibility or member metadata. Unrelated roots/modules cannot supply
    // a more accessible copy of an identical Java qualified name. Consumer
    // sources take precedence over dependency classes with that identity.
    conn.execute_batch(
        "CREATE TEMP TABLE IF NOT EXISTS java_dependency_scope(
             file_id INTEGER PRIMARY KEY, is_consumer INTEGER NOT NULL);
         DELETE FROM temp.java_dependency_scope;",
    )?;
    let membership = MODULE_FILE_SCOPE.replace("?1", "m.path");
    conn.execute(
        &format!(
            "WITH RECURSIVE consumer(id) AS (
                 SELECT id FROM modules WHERE path=?1 AND (?2='' OR root_path=?2)
             ), classpath(id) AS (
                 SELECT id FROM consumer
                 UNION SELECT d.dep_module_id FROM module_deps d JOIN consumer c ON c.id=d.module_id
                 UNION SELECT d.dep_module_id FROM module_deps d JOIN classpath c ON c.id=d.module_id
                       WHERE d.dep_kind='api'
             )
             INSERT INTO temp.java_dependency_scope
             SELECT f.id,EXISTS(SELECT 1 FROM consumer c WHERE c.id=m.id)
             FROM files f JOIN modules m ON (m.root_path='' OR m.root_path=f.root_path)
             JOIN classpath c ON c.id=m.id WHERE {membership} AND substr(f.path,-5)='.java'"
        ),
        params![module_path, module_root],
    )?;

    // Bare refs cannot distinguish alpha.Widget from beta.Widget and omit
    // import-only/static anchors. Stream Java files, retaining one syntax tree
    // at a time, and ask the index only for exact declaration identities.
    let resolver = super::PathResolver::try_from_conn(root, conn)?.with_decoration(false);
    let mut files = conn.prepare(&format!(
        "SELECT f.path,f.root_path FROM files f WHERE {MODULE_FILE_SCOPE} AND (?3='' OR f.root_path=?3)
         AND substr(f.path,-5)='.java' ORDER BY f.path,f.root_path"
    ))?;
    let rows = files.query_map(
        params![module_path, rusqlite::types::Null, module_root],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    )?;
    let mut exists = conn.prepare_cached(
        "SELECT EXISTS(SELECT 1 FROM symbols s JOIN files f ON s.file_id=f.id
         JOIN temp.java_dependency_scope scope ON scope.file_id=f.id
         WHERE substr(f.path,-5)='.java' AND s.qualified_name=?1
         AND s.kind IN ('class','interface','enum'))",
    )?;
    let mut owners = conn.prepare_cached(&format!(
        "SELECT DISTINCT s.name FROM symbols s JOIN files f ON s.file_id=f.id
         WHERE {MODULE_FILE_SCOPE} AND (?3='' OR f.root_path=?3) AND substr(f.path,-5)='.java' AND s.qualified_name=?2
         AND s.kind IN ('class','interface','enum')
         AND s.file_id=(SELECT candidate.file_id FROM symbols candidate
             JOIN temp.java_dependency_scope scope ON scope.file_id=candidate.file_id
             JOIN files source ON source.id=candidate.file_id
             WHERE candidate.qualified_name=?2 AND candidate.kind IN ('class','interface','enum')
             ORDER BY scope.is_consumer DESC,source.path,source.root_path LIMIT 1)
         ORDER BY s.name"
    ))?;
    for row in rows {
        let (path, root_path) = row?;
        let source_path = root.join(resolver.resolve_with_root_raw(&path, Some(&root_path)));
        let content = super::grep::read_java_syntax_source(
            &source_path,
            crate::indexer::max_file_size_bytes(),
        )?;
        let syntax = crate::parsers::treesitter::java::dependency_syntax(&content)?;
        let lookup = JavaDependencyLookup {
            conn,
            root,
            resolver: &resolver,
            package: &syntax.package,
            contexts: &[],
            local_types: &syntax.local_types,
            local_bindings: &syntax.local_bindings,
            resolving_parent: false,
        };
        let mut identities = std::collections::BTreeSet::new();
        let mut explicit = HashMap::new();
        let mut explicit_static = HashSet::new();
        let mut invalid_static_types = HashSet::new();
        let mut wildcards = vec![("java.lang".to_string(), false)];
        for (import, is_static) in &syntax.imports {
            if let Some(owner) = import.strip_suffix(".*") {
                wildcards.push((owner.to_owned(), *is_static));
                continue;
            }
            let simple = import.rsplit('.').next().unwrap_or(import).to_owned();
            // Ordinary single type imports require a canonical name. Static
            // single imports may name an inherited member of a visible owner.
            if let Some(found) = lookup.type_name(import, *is_static)? {
                if !is_static || found.declaration.static_member {
                    identities.insert(found.identity);
                } else {
                    invalid_static_types.insert(simple.clone());
                }
                explicit.insert(simple, import.clone());
                continue;
            }
            if !is_static {
                explicit.insert(simple.clone(), import.clone());
                invalid_static_types.insert(simple);
                continue;
            }
            let Some((owner, member)) = import.rsplit_once('.') else {
                continue;
            };
            let mut found = false;
            for method in [false, true] {
                let key = (member.to_owned(), method);
                if let Some(identity) = lookup.static_member(owner, &key)? {
                    identities.insert(identity);
                    explicit_static.insert(key);
                    found = true;
                }
            }
            if !found {
                // An inaccessible/external single import blocks unrelated
                // wildcard fallback in both member namespaces.
                explicit_static.insert((member.to_owned(), false));
                explicit_static.insert((member.to_owned(), true));
                invalid_static_types.insert(simple);
            }
        }
        for (name, expression, contexts) in &syntax.type_uses {
            let lookup = JavaDependencyLookup { contexts, ..lookup };
            let declaration = |name: &str| lookup.type_name(name, true);
            let mut record_type = |found: JavaDependencyType| -> Result<()> {
                for (qualifier, member, method, owners) in &syntax.qualified_members {
                    if qualifier == name && owners == contexts {
                        if let Some(identity) =
                            lookup.static_member(&found.identity, &(member.clone(), *method))?
                        {
                            identities.insert(identity);
                        }
                    }
                }
                identities.insert(found.identity);
                Ok(())
            };
            let (first, suffix) = name
                .split_once('.')
                .map_or((name.as_str(), ""), |(first, _)| {
                    (first, &name[first.len()..])
                });
            if *expression {
                let member = (first.to_owned(), false);
                let mut value_import = explicit_static.contains(&member)
                    || lookup
                        .lexical_member(
                            &member,
                            syntax.instance_contexts.get(&(
                                first.to_owned(),
                                false,
                                contexts.clone(),
                            )),
                        )?
                        .is_some();
                if !value_import {
                    for (owner, is_static) in &wildcards {
                        if *is_static && lookup.static_member(owner, &member)?.is_some() {
                            value_import = true;
                            break;
                        }
                    }
                }
                // Imported values shadow expression qualifiers, while a
                // same-spelled type annotation remains in the type namespace.
                if value_import {
                    continue;
                }
            }
            if !invalid_static_types.contains(first) {
                let (bound, found) = lookup.lexical_type(name)?;
                if bound {
                    if let Some(found) = found {
                        record_type(found)?;
                    }
                    continue;
                }
            }
            if let Some(import) = explicit.get(first) {
                if invalid_static_types.contains(first) {
                    continue;
                }
                let candidate = format!("{import}{suffix}");
                if let Some(found) = declaration(&candidate)? {
                    record_type(found)?;
                }
                continue;
            }
            let local = if syntax.package.is_empty() {
                name.clone()
            } else {
                format!("{}.{name}", syntax.package)
            };
            if exists.query_row(params![&local], |row| row.get::<_, bool>(0))? {
                if let Some(found) = declaration(&local)? {
                    record_type(found)?;
                }
                continue;
            }
            if name.contains('.') {
                if let Some(found) = declaration(&local)? {
                    record_type(found)?;
                    continue;
                }
            }
            if name.contains('.') {
                if let Some(found) = declaration(name)? {
                    record_type(found)?;
                    continue;
                }
            }
            let mut matches = std::collections::BTreeMap::new();
            for (package, is_static) in &wildcards {
                let candidate = format!("{package}.{name}");
                if let Some(found) = lookup.type_name(&candidate, *is_static)? {
                    if !is_static || found.declaration.static_member {
                        matches.insert(found.identity.clone(), found);
                    }
                }
            }
            // Ambiguous on-demand imports are not evidence of either owner.
            if matches.len() == 1 {
                record_type(matches.into_values().next().unwrap())?;
            }
        }
        for (name, method, contexts) in &syntax.member_uses {
            let member = &(name.clone(), *method);
            let lookup = JavaDependencyLookup { contexts, ..lookup };
            if invalid_static_types.contains(name) {
                continue;
            }
            if let Some(identity) = lookup.lexical_member(
                member,
                syntax
                    .instance_contexts
                    .get(&(name.clone(), *method, contexts.clone())),
            )? {
                identities.insert(identity);
                continue;
            }
            if explicit_static.contains(member) {
                continue;
            }
            let mut matches = std::collections::BTreeSet::new();
            for (owner, is_static) in &wildcards {
                if *is_static {
                    if let Some(identity) = lookup.static_member(owner, member)? {
                        matches.insert(identity);
                    }
                }
            }
            if matches.len() == 1 {
                identities.extend(matches);
            }
        }
        for member in super::graph::dependency_value_members(&content, &syntax.package)? {
            if let Some(chain) = &member.chain {
                let accessing = JavaDependencyLookup {
                    contexts: &member.use_contexts,
                    ..lookup
                };
                if let Some((owner, instance)) =
                    accessing.value_receiver(chain, &syntax.imports, &mut identities, 0)?
                {
                    let key = (member.name, member.method);
                    let identity = if instance {
                        accessing.value_member(&owner, &key)?
                    } else {
                        accessing.static_member(&owner.identity, &key)?
                    };
                    if let Some(identity) = identity {
                        identities.insert(identity);
                    }
                }
                continue;
            }
            let binding = JavaDependencyLookup {
                contexts: &member.contexts,
                ..lookup
            };
            if let Some(owner) =
                binding.value_type(&member.path, member.position, &syntax.imports)?
            {
                let accessing = JavaDependencyLookup {
                    contexts: &member.use_contexts,
                    ..lookup
                };
                if let Some(identity) =
                    accessing.value_member(&owner, &(member.name, member.method))?
                {
                    identities.insert(identity);
                }
            }
        }
        // A local qualifier can name an inherited external superclass type.
        // Its spelling is shadowed in the source type-use pass, but the bound
        // parent still contributes its declaring dependency (e.g. Base.Nested).
        for (identity, declaration) in &syntax.local_types {
            let mut contexts = Vec::new();
            let mut prefix = identity.as_str();
            while let Some((outer, _)) = prefix.rsplit_once('.') {
                if outer == syntax.package {
                    break;
                }
                contexts.push(outer.to_owned());
                prefix = outer;
            }
            let lookup = JavaDependencyLookup {
                contexts: &contexts,
                ..lookup
            };
            let owner = JavaDependencyType {
                identity: identity.clone(),
                declaration: declaration.clone(),
            };
            for parent in lookup.parents(&owner)? {
                if !parent.identity.contains('@') && parent.declaration.accessible {
                    identities.insert(parent.identity);
                }
            }
        }
        for identity in identities {
            for name in owners
                .query_map(params![dependency_path, identity, dependency_root], |row| {
                    row.get::<_, String>(0)
                })?
            {
                used.insert(&name?)?;
            }
        }
    }

    // Stream every indexed type instead of either truncating the dependency
    // at 100 types or retaining its whole type population in a Rust Vec.
    let mut candidates = conn.prepare(&format!(
        "SELECT DISTINCT s.name FROM symbols s JOIN files f ON s.file_id=f.id
         WHERE {MODULE_FILE_SCOPE} AND (?3='' OR f.root_path=?3) AND s.kind IN ('class','interface','enum','object')
         ORDER BY s.name"
    ))?;
    let symbols = candidates.query_map(
        params![dependency_path, rusqlite::types::Null, dependency_root],
        |row| row.get::<_, String>(0),
    )?;

    let mut stmt = conn.prepare_cached(&format!(
        "SELECT EXISTS(SELECT 1 FROM refs r JOIN files f ON r.file_id=f.id
         WHERE {MODULE_FILE_SCOPE} AND (?3='' OR f.root_path=?3) AND substr(f.path,-5)!='.java' AND r.name=?2)"
    ))?;

    for symbol in symbols {
        let symbol = symbol?;
        let count: i64 =
            stmt.query_row(params![module_path, &symbol, module_root], |row| row.get(0))?;
        if count > 0 {
            used.insert(&symbol)?;
        }
    }

    used.summary()
}

#[cfg(test)]
mod dependency_usage_memory_tests {
    use super::UsedDependencySymbols;

    #[test]
    fn deduplication_exceeds_cache_without_an_unbounded_name_vector() {
        let used = UsedDependencySymbols::new().unwrap();
        let suffix = "x".repeat(64);
        for number in (0..80_000).rev() {
            let name = format!("Symbol{number:06}_{suffix}");
            used.insert(&name).unwrap();
            used.insert(&name).unwrap();
        }
        let (count, samples) = used.summary().unwrap();
        assert_eq!(count, 80_000);
        assert_eq!(
            samples,
            (0..3)
                .map(|number| format!("Symbol{number:06}_{suffix}"))
                .collect::<Vec<_>>()
        );
        let pragma = |name: &str| {
            used.connection
                .pragma_query_value(None, name, |row| row.get::<_, i64>(0))
                .unwrap()
        };
        assert_eq!(pragma("cache_size"), -2048);
        assert!(pragma("cache_spill") > 0);
        assert!(pragma("page_count") * pragma("page_size") > 2 * 1024 * 1024);
    }
}
