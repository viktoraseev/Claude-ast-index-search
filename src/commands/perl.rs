//! Lexical Perl searches with accepted-line limits and stable source ordering.

use std::path::Path;

use anyhow::Result;
use colored::Colorize;

use super::{relative_path, search_files_filtered};

fn matches_query(text: &str, query: Option<&str>) -> bool {
    query.is_none_or(|q| text.to_lowercase().contains(&q.to_lowercase()))
}

/// Read the identifier after sub/use/require, without its arguments or body.
fn declaration_name(line: &str) -> Option<&str> {
    let mut words = line.split_whitespace();
    words.next()?;
    words
        .next()?
        .split(|c: char| !c.is_alphanumeric() && c != '_' && c != ':')
        .next()
}

fn is_pragma(line: &str) -> bool {
    let mut words = line.split_whitespace();
    if words.next() != Some("use") {
        return false;
    }
    declaration_name(line).is_some_and(|name| {
        matches!(
            name,
            "strict" | "warnings" | "constant" | "base" | "parent" | "utf8"
        ) || name
            .strip_prefix('v')
            .is_some_and(|tail| !tail.is_empty() && tail.chars().all(|c| c.is_ascii_digit()))
    })
}

/// Collect only accepted lines, in path/line order, using the shared bounded search.
fn print_search<K>(
    root: &Path,
    pattern: &str,
    extensions: &[&str],
    limit: usize,
    title: &str,
    width: usize,
    keep: K,
) -> Result<()>
where
    K: Fn(&str) -> bool + Sync,
{
    let mut results = Vec::new();
    search_files_filtered(
        root,
        pattern,
        extensions,
        limit,
        |_, line| keep(line),
        |path, line_num, line| {
            let content: String = line.trim().chars().take(width).collect();
            results.push((relative_path(root, path), line_num, content));
        },
    )?;
    println!("{}", format!("{} ({}):", title, results.len()).bold());
    for (path, line_num, content) in results {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }
    Ok(())
}

/// Find Perl @EXPORT and @EXPORT_OK definitions.
pub fn cmd_perl_exports(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_search(
        root,
        r"\bour\s+@EXPORT(?:_OK)?\b|@EXPORT(?:_OK)?\s*=",
        &["pm"],
        limit,
        "Perl exports",
        100,
        |line| matches_query(line, query),
    )
}

/// Find Perl subroutine definitions, filtered by declared name.
pub fn cmd_perl_subs(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_search(
        root,
        r"^\s*sub\s+\w+",
        &["pm", "pl", "t"],
        limit,
        "Perl subroutines",
        80,
        |line| declaration_name(line).is_some_and(|name| matches_query(name, query)),
    )
}

/// Find POD documentation sections.
pub fn cmd_perl_pod(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_search(
        root,
        r"^=(head[1-4]|item|over|back|pod|cut|begin|end|for)\b",
        &["pm", "pl", "pod"],
        limit,
        "POD documentation",
        100,
        |line| matches_query(line, query),
    )
}

/// Find lexical Test::More / Test::Simple assertion lines.
pub fn cmd_perl_tests(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_search(
        root,
        r"\b(ok|is|isnt|like|unlike|cmp_ok|is_deeply|diag|pass|fail|subtest|plan|done_testing|SKIP|TODO)\s*[\(\{]",
        &["t", "pm", "pl"],
        limit,
        "Perl tests",
        100,
        |line| matches_query(line, query),
    )
}

/// Find Perl use/require statements, excluding exact use pragmas.
pub fn cmd_perl_imports(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_search(
        root,
        r"^\s*(use|require)\s+[A-Za-z]",
        &["pm", "pl", "t"],
        limit,
        "Perl imports",
        100,
        |line| {
            !is_pragma(line)
                && declaration_name(line).is_some_and(|name| matches_query(name, query))
        },
    )
}
