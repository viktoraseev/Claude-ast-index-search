//! Code analysis commands
//!
//! - unused-symbols: Find potentially unused public symbols

use std::path::Path;

use anyhow::Result;
use colored::Colorize;

use super::PathResolver;
use crate::db::{self, SearchScope};

/// Find potentially unused symbols in a module or project
pub fn cmd_unused_symbols(
    root: &Path,
    module: Option<&str>,
    export_only: bool,
    limit: usize,
    format: &str,
) -> Result<()> {
    cmd_unused_symbols_scoped(
        root,
        module,
        export_only,
        limit,
        format,
        &SearchScope::none(),
    )
}

pub fn cmd_unused_symbols_scoped(
    root: &Path,
    module: Option<&str>,
    export_only: bool,
    limit: usize,
    format: &str,
    scope: &SearchScope,
) -> Result<()> {
    if !db::db_exists(root) {
        println!(
            "{}",
            "Index not found. Run 'ast-index rebuild' first.".red()
        );
        return Ok(());
    }

    let conn = db::open_db_leased(root)?;

    // `--module` accepts a module name (`features.surge.impl`, `:core:utils`)
    // as well as a raw path prefix; names resolve to the module's directory.
    let module_path = match module {
        Some(m) => match db::find_module_id_by_name(&conn, m)? {
            Some(id) => db::get_module_path(&conn, id)?.map(|p| {
                let path = p.trim_end_matches('/');
                if path.is_empty() {
                    String::new()
                } else {
                    format!("{path}/")
                }
            }),
            None => Some(m.to_string()),
        },
        None => None,
    };

    let selected = SearchScope {
        module: module_path.as_deref().or(scope.module),
        in_file: scope.in_file,
        dir_prefix: scope.dir_prefix,
    };
    let (mut unused, checked) =
        db::find_potentially_unused_symbols_scoped(&conn, export_only, limit, &selected)?;
    let resolver = PathResolver::try_from_conn(root, &conn)?.with_decoration(format != "json");
    for symbol in &mut unused {
        symbol.path = resolver.resolve_with_root(&symbol.path, symbol.root_path.as_deref());
    }

    if format == "json" {
        println!("{}", serde_json::to_string_pretty(&unused)?);
        return Ok(());
    }

    let scope = module.unwrap_or("project");
    println!(
        "{}",
        format!(
            "Potentially unused symbols in '{}' ({}/{} checked):",
            scope,
            unused.len(),
            checked
        )
        .bold()
    );

    for s in &unused {
        println!("  {} [{}]: {}:{}", s.name.yellow(), s.kind, s.path, s.line);
    }

    if unused.is_empty() {
        println!("  No unused symbols found.");
    }

    Ok(())
}
