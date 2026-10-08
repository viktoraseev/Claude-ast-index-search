//! Android-specific commands
//!
//! Commands for working with Android codebases:
//! - xml_usages: Find XML usages of a class (layouts, views)
//! - resource_usages: Find Android resource usages (drawables, strings, etc.)

use std::collections::HashMap;
use std::path::Path;

use anyhow::Result;
use colored::Colorize;
use rusqlite::params;

use crate::db;

/// Select indexed occurrence paths before caps and counts, retaining declaration
/// metadata outside the scope for Java references to another module's resources.
fn open_android_query_db(root: &Path) -> Result<db::LeasedConnection> {
    let conn = db::open_db_leased(root)?;
    let resolver = super::PathResolver::try_from_conn(root, &conn)?;
    let cwd = std::env::current_dir()?;
    let prefix = cwd
        .strip_prefix(root)
        .ok()
        .filter(|path| !path.as_os_str().is_empty());
    conn.execute_batch("CREATE TEMP TABLE android_scope_files(path TEXT PRIMARY KEY);")?;
    {
        let mut stmt = conn.prepare(
            "SELECT file_path FROM main.xml_usages
             UNION SELECT file_path FROM main.resources
             UNION SELECT usage_file FROM main.resource_usages",
        )?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let stored: String = row.get(0)?;
            let physical = root.join(&stored);
            if resolver
                .scoped_relative_path(&physical)
                .is_some_and(|relative| {
                    prefix.is_none_or(|prefix| Path::new(&relative).starts_with(prefix))
                })
            {
                conn.execute(
                    "INSERT INTO android_scope_files VALUES (?1)",
                    params![stored],
                )?;
            }
        }
    }
    conn.execute_batch(
        "CREATE TEMP VIEW xml_usages AS
             SELECT x.* FROM main.xml_usages x JOIN android_scope_files s ON s.path=x.file_path;
         CREATE TEMP VIEW resource_usages AS
             SELECT r.* FROM main.resource_usages r JOIN android_scope_files s ON s.path=r.usage_file;",
    )?;
    Ok(conn)
}

/// Find XML usages of a class (layouts, views)
pub fn cmd_xml_usages(root: &Path, class_name: &str, module_filter: Option<&str>) -> Result<()> {
    if !db::db_exists(root) {
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_android_query_db(root)?;

    // Check if XML usages are indexed
    let xml_count: i64 =
        conn.query_row("SELECT COUNT(*) FROM main.xml_usages", [], |row| row.get(0))?;
    if xml_count == 0 {
        println!(
            "{}",
            "XML usages not indexed. Run 'ast-index rebuild' first.".yellow()
        );
        return Ok(());
    }

    // Search for class in XML usages
    let pattern = format!("%{}%", class_name);

    let results: Vec<(String, String, i64, String, Option<String>)> = if let Some(module) =
        module_filter
    {
        let mut stmt = conn.prepare(
                "SELECT COALESCE(m.name, '(unassigned)'), x.file_path, x.line, x.class_name, x.element_id
             FROM xml_usages x
             LEFT JOIN modules m ON x.module_id = m.id
             WHERE x.class_name LIKE ?1 AND m.name = ?2
             ORDER BY x.file_path, x.line, m.name",
            )?;
        let rows = stmt.query_map(params![pattern, module], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?;
        rows.filter_map(|r| r.ok()).collect()
    } else {
        let mut stmt = conn.prepare(
                "SELECT COALESCE(m.name, '(unassigned)'), x.file_path, x.line, x.class_name, x.element_id
             FROM xml_usages x
             LEFT JOIN modules m ON x.module_id = m.id
             WHERE x.class_name LIKE ?1
             ORDER BY x.file_path, x.line, m.name
             LIMIT 100",
            )?;
        let rows = stmt.query_map(params![pattern], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?;
        rows.filter_map(|r| r.ok()).collect()
    };

    println!(
        "{}",
        format!("XML usages of '{}' ({}):", class_name, results.len()).bold()
    );

    // Group by module
    let mut by_module: HashMap<String, Vec<(String, i64, String, Option<String>)>> = HashMap::new();
    for (module, file, line, class, element_id) in results {
        by_module
            .entry(module)
            .or_default()
            .push((file, line, class, element_id));
    }

    for (module, usages) in &by_module {
        println!("\n{}:", module.cyan());
        for (file, line, class, element_id) in usages {
            let id_str = element_id
                .as_ref()
                .map(|id| format!(" ({})", id))
                .unwrap_or_default();
            println!("  {}:{}", file, line);
            println!("    <{} ...{} />", class, id_str);
        }
    }

    if by_module.is_empty() {
        println!("  No XML usages found.");
    }

    Ok(())
}

/// Find Android resource usages (drawables, strings, colors, etc.)
pub fn cmd_resource_usages(
    root: &Path,
    resource: &str,
    module_filter: Option<&str>,
    type_filter: Option<&str>,
    show_unused: bool,
) -> Result<()> {
    if !db::db_exists(root) {
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = open_android_query_db(root)?;

    // Check if resources are indexed
    let res_count: i64 = conn.query_row("SELECT COUNT(*) FROM resources", [], |row| row.get(0))?;
    if res_count == 0 {
        println!(
            "{}",
            "Resources not indexed. Run 'ast-index rebuild' first.".yellow()
        );
        return Ok(());
    }

    if show_unused {
        // Show unused resources in the module
        let module = module_filter.unwrap_or("");
        if module.is_empty() {
            println!(
                "{}",
                "Please specify --module to find unused resources.".yellow()
            );
            return Ok(());
        }
    } else if resource.is_empty() {
        println!(
            "{}",
            "Please specify a resource name (e.g., @drawable/ic_payment or use --unused).".yellow()
        );
        return Ok(());
    }

    if show_unused {
        let module = module_filter.unwrap_or("");
        println!("{}", format!("Unused resources in '{}':", module).bold());

        // Find resources defined in module that have no usages
        let mut stmt = conn.prepare(
            "SELECT r.type, r.name, r.file_path
             FROM resources r
             JOIN modules m ON r.module_id = m.id
             WHERE m.name = ?1 AND r.file_path IN (SELECT path FROM android_scope_files)
               AND NOT EXISTS (
                 SELECT 1 FROM main.resource_usages ru
                 JOIN resources used ON used.id = ru.resource_id
                 WHERE used.type = r.type AND used.name = r.name
                   AND used.module_id IS r.module_id
             )
             ORDER BY r.type, r.name",
        )?;

        let unused: Vec<(String, String, String)> = stmt
            .query_map(params![module], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })?
            .filter_map(|r| r.ok())
            .collect();

        // Group by type
        let mut by_type: HashMap<String, Vec<(String, String)>> = HashMap::new();
        for (rtype, name, path) in unused {
            if type_filter.map(|t| t == rtype).unwrap_or(true) {
                by_type.entry(rtype).or_default().push((name, path));
            }
        }

        let mut total = 0;
        for (rtype, items) in &by_type {
            println!("\n{} ({}):", rtype.cyan(), items.len());
            for (name, path) in items.iter().take(10) {
                println!("  {} @{}/{}", "⚠".yellow(), rtype, name);
                println!("    defined in: {}", path);
            }
            if items.len() > 10 {
                println!("  ... and {} more", items.len() - 10);
            }
            total += items.len();
        }

        println!("\n{}", format!("Total unused: {} resources", total).bold());
    } else {
        // Parse resource reference (e.g., @drawable/ic_payment or R.string.app_name)
        let (res_type, res_name) = parse_resource_reference(resource);

        let res_type = type_filter.unwrap_or(&res_type);

        println!(
            "{}",
            format!("Usages of '@{}/{}':", res_type, res_name).bold()
        );

        // Find resource usages
        let results: Vec<(String, i64, String, usize)> = if let Some(module) = module_filter {
            let mut stmt = conn.prepare(
                "WITH resource_modules AS (
                    SELECT name,CASE WHEN root_path='' OR root_path=?4 THEN path
                        ELSE root_path || '/' || path END AS path FROM modules
                 ), matching AS (
                 SELECT ru.usage_file, ru.usage_line, ru.usage_type
                 FROM resource_usages ru
                 JOIN resources r ON ru.resource_id = r.id
                 WHERE r.type = ?1 AND r.name = ?2 AND (
                     SELECT m.name FROM resource_modules m
                     WHERE m.path = '' OR ru.usage_file = m.path
                        OR substr(ru.usage_file, 1, length(m.path) + 1) = m.path || '/'
                     ORDER BY length(m.path) DESC LIMIT 1
                 ) = ?3
                 ), ranked AS (
                     SELECT usage_file, usage_line, usage_type,
                            COUNT(*) OVER (PARTITION BY usage_type) AS group_total,
                            ROW_NUMBER() OVER (PARTITION BY usage_type ORDER BY usage_file, usage_line) AS position
                     FROM matching
                 )
                 SELECT usage_file, usage_line, usage_type, group_total
                 FROM ranked WHERE position <= 10
                 ORDER BY usage_file, usage_line",
            )?;
            let rows = stmt.query_map(
                params![
                    res_type,
                    res_name,
                    module,
                    db::normalize_root_for_storage(root)
                ],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )?;
            rows.filter_map(|r| r.ok()).collect()
        } else {
            let mut stmt = conn.prepare(
                "WITH matching AS (
                 SELECT ru.usage_file, ru.usage_line, ru.usage_type
                 FROM resource_usages ru
                 JOIN resources r ON ru.resource_id = r.id
                 WHERE r.type = ?1 AND r.name = ?2
                 ), ranked AS (
                     SELECT usage_file, usage_line, usage_type,
                            COUNT(*) OVER (PARTITION BY usage_type) AS group_total,
                            ROW_NUMBER() OVER (PARTITION BY usage_type ORDER BY usage_file, usage_line) AS position
                     FROM matching
                 )
                 SELECT usage_file, usage_line, usage_type, group_total
                 FROM ranked WHERE position <= 10
                 ORDER BY usage_file, usage_line",
            )?;
            let rows = stmt.query_map(params![res_type, res_name], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?;
            rows.filter_map(|r| r.ok()).collect()
        };

        // Group by usage type
        let code_usages: Vec<_> = results.iter().filter(|(_, _, t, _)| t == "code").collect();
        let xml_usages: Vec<_> = results.iter().filter(|(_, _, t, _)| t == "xml").collect();
        let code_total = code_usages.first().map_or(0, |row| row.3);
        let xml_total = xml_usages.first().map_or(0, |row| row.3);
        let group_totals: HashMap<&str, usize> = results
            .iter()
            .map(|(_, _, kind, total)| (kind.as_str(), *total))
            .collect();
        let total: usize = group_totals.values().sum();

        if !code_usages.is_empty() {
            println!("\n{} ({}):", "Kotlin/Java".cyan(), code_total);
            for (file, line, _, _) in code_usages.iter().take(10) {
                println!("  {}:{}", file, line);
            }
            if code_total > code_usages.len() {
                println!("  ... and {} more", code_total - code_usages.len());
            }
        }

        if !xml_usages.is_empty() {
            println!("\n{} ({}):", "XML".cyan(), xml_total);
            for (file, line, _, _) in xml_usages.iter().take(10) {
                println!("  {}:{}", file, line);
            }
            if xml_total > xml_usages.len() {
                println!("  ... and {} more", xml_total - xml_usages.len());
            }
        }

        if results.is_empty() {
            println!("  No usages found.");
        } else {
            println!("\n{}", format!("Total: {} usages", total).bold());
        }
    }

    Ok(())
}

/// Parse resource reference like @drawable/ic_name or R.string.name
fn parse_resource_reference(resource: &str) -> (String, String) {
    // Format: @type/name
    if resource.starts_with('@') {
        let parts: Vec<&str> = resource[1..].splitn(2, '/').collect();
        if parts.len() == 2 {
            return (parts[0].to_string(), parts[1].to_string());
        }
    }

    // Format: R.type.name
    if resource.starts_with("R.") {
        let parts: Vec<&str> = resource[2..].splitn(2, '.').collect();
        if parts.len() == 2 {
            return (parts[0].to_string(), parts[1].to_string());
        }
    }

    // Assume it's just a name, try to guess type from prefix
    let resource = resource.trim_start_matches('@');
    if resource.starts_with("ic_") || resource.starts_with("img_") {
        return ("drawable".to_string(), resource.to_string());
    }
    if resource.starts_with("color_") {
        return ("color".to_string(), resource.to_string());
    }

    // Default: assume it's a string resource
    ("string".to_string(), resource.to_string())
}
