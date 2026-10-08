//! Grep-based search commands
//!
//! General pattern-based search commands:
//! - todo: Find TODO/FIXME/HACK comments
//! - callers: Find function callers
//! - provides: Find Dagger @Provides/@Binds for a type
//! - suspend: Find suspend functions
//! - composables: Find @Composable functions
//! - deprecated: Find @Deprecated annotations
//! - suppress: Find @Suppress annotations
//! - inject: Find @Inject points for a type
//! - annotations: Find uses of specific annotation
//! - deeplinks: Find deeplink definitions
//! - extensions: Find extension functions/types
//! - flows: Find Flow declarations
//! - previews: Find @Preview functions

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use anyhow::Result;
use colored::Colorize;
use regex::Regex;

use super::graph::short_name;
use super::{
    print_truncation_notice, relative_path, search_files_filtered, search_files_limited,
    PathResolver,
};
use crate::db;

pub(crate) fn read_java_syntax_source(path: &Path, budget: u64) -> Result<String> {
    let file = std::fs::File::open(path)?;
    anyhow::ensure!(
        file.metadata()?.len() <= budget,
        "Java syntax source exceeds the {budget} byte budget"
    );
    read_java_syntax_stream(file, budget)
}

fn read_java_syntax_stream(reader: impl std::io::Read, budget: u64) -> Result<String> {
    use std::io::Read;

    // Bound the read itself: a file may grow after the metadata check.
    let mut bytes = Vec::new();
    reader
        .take(budget.saturating_add(1))
        .read_to_end(&mut bytes)?;
    anyhow::ensure!(
        bytes.len() as u64 <= budget,
        "Java syntax source exceeds the {budget} byte budget"
    );
    Ok(String::from_utf8(bytes)?)
}

/// All source code extensions (for grep-based commands: todo, search, callers, etc.)
pub const ALL_SOURCE_EXTENSIONS: [&str; 58] = [
    "kt", "java", "swift", "m", "h",    // Mobile
    "dart", // Flutter
    "gd",   // GDScript (Godot)
    "pm", "pl", "t", "rb", // Scripting
    "ts", "tsx", "mts", "js", "jsx", "mjs", "cjs", // JavaScript/TypeScript
    "vue", "svelte", // Web frameworks
    "css", "pcss", "postcss", "scss", "less", // CSS family
    "py",   // Python
    "go",   // Go
    "rs",   // Rust
    "zig",  // Zig
    "cs",   // C#
    "cpp", "cc", "c", "hpp", // C++
    "scala", "sc", // Scala
    "php", "phtml", // PHP
    "groovy", "gradle", // Groovy
    "lua",    // Lua
    "ex", "exs", // Elixir
    "sh", "bash", "zsh", // Shell
    "sql", // SQL
    "r", "R", // R
    "bsl", "os", // BSL
    "lisp", "lsp", "cl", "asd", // Common Lisp
    "proto", "wsdl", "xsd", // Schema
];

/// Combine a search pattern with a literal, case-insensitive line filter.
///
/// `search_files_limited` counts `limit` against raw pattern matches, so any
/// filtering done afterwards in the handler silently under-reports. Folding
/// the filter into the pattern keeps `--limit` honest.
fn pattern_with_line_filter(pattern: &str, filter: Option<&str>) -> String {
    match filter.filter(|f| !f.is_empty()) {
        Some(f) => {
            let f = regex::escape(f);
            format!("(?:{pattern}).*(?i:{f})|(?i:{f}).*(?:{pattern})")
        }
        None => pattern.to_string(),
    }
}

/// Trailing word boundary: `\b` for normal names, empty for Ruby bang/question methods
fn trailing_boundary(function_name: &str) -> &str {
    if function_name.ends_with('!') || function_name.ends_with('?') {
        "" // ! and ? are non-word chars — natural boundary, \b would fail here
    } else {
        r"\b"
    }
}

/// A Ruby symbol naming the method: `before_save :name`, `validate :name`,
/// `delegate :name`, `map(&:name)`, `send(:name)`. The symbol has to end
/// where the name does, since `:name?`, `:name!` and `:name=` name other
/// methods (`authorize(record, :update?)` is about `update?`, not `update`),
/// and a `::` in front is a path, not a symbol (`use super::name;`,
/// `Billing::Invoice`, `std::mem::take`).
const SYMBOL_REF_IDIOM: &str = r"(?:^|[^:]):{fn}(?:[^\w?!=]|$)";

/// Call idioms recognised across languages, joined into one alternation.
/// `{fn}` stands for the escaped function name. Every idiom contains `{fn}`,
/// so a line that matches always contains the name verbatim; the batched
/// call-tree scan relies on that.
const CALLER_IDIOMS: [&str; 13] = [
    r"[.>]{fn}\s*\(",                // obj.func( or obj->func(
    r"\b{fn}\s*\(",                  // bare func( anywhere in line
    r"->{fn}\s*\(",                  // ->func(
    r"&{fn}\s*\(",                   // &func(
    r"this\.{fn}\s*\(",              // this.func(
    r"super\.{fn}\s*\(",             // super.func(
    r"\.{fn}(?:\s|$)",               // Ruby: obj.method (no parens)
    SYMBOL_REF_IDIOM,                // Ruby: :method_name (callbacks, delegate, &:name)
    r"\b{fn}\.",                     // Ruby: bare method.chain (e.g. scope.where)
    r"\bawait\s+{fn}\s*\(",          // TS: await func(
    r"\bawait\s+[\w.]+\.{fn}\s*\(",  // TS: await obj.func(
    r"\breturn\s+{fn}\s*\(",         // TS: return func(
    r"\breturn\s+[\w.]+\.{fn}\s*\(", // TS: return obj.func(
];

/// The call idiom of a Ruby predicate or bang method alone: the bare name,
/// with neither receiver nor parentheses (`if next_page? && …`, `save!`). No
/// local variable can end in `?` or `!`, so the word itself is a call. Not
/// after `#`, which marks a method in documentation (`describe "#valid?"`),
/// and not before `=` or `~`: `save!=` and `save!~` apply `!=` and `!~` to
/// `save`.
const BARE_PREDICATE_CALL_IDIOM: &str = r"(?:^|[^#\w]){fn}(?:[^=~]|$)";

/// Whether [`BARE_PREDICATE_CALL_IDIOM`] applies: a plain identifier ending in
/// `?` or `!`.
fn is_predicate_or_bang_name(function_name: &str) -> bool {
    function_name
        .strip_suffix(&['?', '!'][..])
        .is_some_and(|body| {
            !body.is_empty() && body.chars().all(|c| c.is_alphanumeric() || c == '_')
        })
}

fn caller_pattern(fn_pattern: &str) -> String {
    CALLER_IDIOMS
        .iter()
        .map(|idiom| idiom.replace("{fn}", fn_pattern))
        .collect::<Vec<_>>()
        .join("|")
}

/// Build regex pattern that matches function/method calls across languages
fn build_caller_pattern(function_name: &str) -> String {
    let escaped = regex::escape(function_name);
    let mut pattern = caller_pattern(&escaped);
    if is_predicate_or_bang_name(function_name) {
        pattern.push('|');
        pattern.push_str(&BARE_PREDICATE_CALL_IDIOM.replace("{fn}", &escaped));
    }
    pattern
}

/// A pattern matching every line that [`build_caller_pattern`] matches for
/// at least one of `function_names`. Every idiom holds the alternation of
/// the names where one name stood, and the bare name at a word boundary
/// widens the bare predicate idiom, so the superset holds for every name.
fn build_any_caller_pattern(function_names: &[String]) -> String {
    let names: Vec<String> = function_names
        .iter()
        .map(|name| regex::escape(name))
        .collect();
    let mut pattern = caller_pattern(&format!("(?:{})", names.join("|")));
    let predicates: Vec<String> = function_names
        .iter()
        .filter(|name| is_predicate_or_bang_name(name))
        .map(|name| regex::escape(name))
        .collect();
    if !predicates.is_empty() {
        pattern.push_str(&format!(r"|\b(?:{})", predicates.join("|")));
    }
    pattern
}

/// Words the Java-style branch of [`build_def_skip_pattern`] would otherwise
/// read as a return type. Standing right before a name they make the line a
/// call, never a definition: `return foo(`, `await foo(`, `new Foo(`,
/// `if foo(`, `for x in foo(`, `export default foo(`, `go foo(`, `puts foo(`.
const KEYWORDS_BEFORE_CALL: [&str; 33] = [
    "and", "assert", "await", "case", "default", "defer", "echo", "elif", "else", "elsif", "from",
    "go", "if", "in", "match", "new", "not", "of", "or", "print", "puts", "raise", "range",
    "return", "then", "throw", "try", "unless", "until", "when", "while", "with", "yield",
];

/// Lines that define one particular function, as opposed to calling it.
struct DefinitionPattern {
    full: Regex,
    /// Matches every line `full` matches and is cheap to run: no captures,
    /// no word boundaries, no `\w`, so the lazy DFA handles it on any input.
    candidate: Regex,
}

impl DefinitionPattern {
    fn is_match(&self, line: &str) -> bool {
        // Nearly every line a caller scan hands over is a call, not a
        // definition, and `full` needs the PikeVM for its captures and its
        // Unicode word boundaries. Run on every call line of a name as
        // common as `call`, it was what `callers` spent its time on.
        if !self.candidate.is_match(line) {
            return false;
        }
        // `regex` has no lookaround, so the word in return-type position is
        // captured and a keyword there is ruled out here instead.
        self.full.captures_iter(line).any(|caps| {
            caps.name("type")
                .map_or(true, |word| !KEYWORDS_BEFORE_CALL.contains(&word.as_str()))
        })
    }
}

/// Build regex pattern that skips function/method definitions
fn build_def_skip_pattern(function_name: &str) -> DefinitionPattern {
    let fn_escaped = regex::escape(function_name);
    let tb = trailing_boundary(function_name);
    let full = Regex::new(&format!(
        concat!(
            r"\b(?:fun|func|sub)\s+{fn}\s*[<({{\[]",           // Kotlin/Swift/Perl
            r"|\bdef\s+(?:self\.)?{fn}{tb}",                    // Ruby: def method / def self.method
            r"|\b(?:(?:public|private|protected|static|final|abstract|synchronized|override)\s+)*",
            r"(?:void|int|long|boolean|char|byte|short|float|double|(?P<type>[\w.]+)(?:<[^{{;]*>)?(?:\[\])*)\s+{fn}\s*\(", // Java
        ),
        fn = fn_escaped,
        tb = tb
    ))
    .expect("Invalid def skip pattern");
    // The Kotlin/Swift/Perl and Java branches both put whitespace right
    // before the name and one of `<({[` after it; the Ruby branch needs `def`.
    let candidate = Regex::new(&format!(
        r"\s{fn}\s*[<({{\[]|def\s+(?:self\.)?{fn}",
        fn = fn_escaped
    ))
    .expect("Invalid def skip candidate pattern");
    DefinitionPattern { full, candidate }
}

/// Emit a bounded result page. Count describes returned matches, not an
/// unbounded total that these early-terminating searches have not collected.
fn print_search_json<T: serde::Serialize>(items: &[T]) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({"items": items, "count": items.len()}))?
    );
    Ok(())
}

fn print_line_search_json(items: &[(String, usize, String)]) -> Result<()> {
    let items: Vec<_> = items
        .iter()
        .map(|(path, line, content)| {
            serde_json::json!({"path": path, "line": line, "content": content})
        })
        .collect();
    print_search_json(&items)
}

/// Find TODO/FIXME/HACK comments
pub fn cmd_todo(root: &Path, pattern: &str, limit: usize, format: &str) -> Result<()> {
    let search_pattern = format!(r"//.*({pattern})|#.*({pattern})");

    let mut todos: HashMap<String, Vec<(String, usize, String)>> = HashMap::new();
    todos.insert("TODO".to_string(), vec![]);
    todos.insert("FIXME".to_string(), vec![]);
    todos.insert("HACK".to_string(), vec![]);
    todos.insert("OTHER".to_string(), vec![]);

    let mut count = 0;

    search_files_limited(
        root,
        &search_pattern,
        &ALL_SOURCE_EXTENSIONS,
        limit,
        |path, line_num, line| {
            let rel_path = relative_path(root, path);
            let content: String = line.chars().take(80).collect();
            let upper = content.to_uppercase();

            let category = if upper.contains("TODO") {
                "TODO"
            } else if upper.contains("FIXME") {
                "FIXME"
            } else if upper.contains("HACK") {
                "HACK"
            } else {
                "OTHER"
            };

            todos
                .get_mut(category)
                .unwrap()
                .push((rel_path, line_num, content));
            count += 1;
        },
    )?;

    if format == "json" {
        let mut items: Vec<_> = todos
            .iter()
            .flat_map(|(category, rows)| {
                rows.iter().map(move |(path, line, content)| {
                    serde_json::json!({"path": path, "line": line,
                        "content": content, "category": category})
                })
            })
            .collect();
        items.sort_by(|a, b| {
            a["path"]
                .as_str()
                .cmp(&b["path"].as_str())
                .then_with(|| a["line"].as_u64().cmp(&b["line"].as_u64()))
        });
        return print_search_json(&items);
    }

    let total: usize = todos.values().map(|v| v.len()).sum();
    println!("{}", format!("Found {} comments:", total).bold());

    for (category, items) in &todos {
        if !items.is_empty() {
            println!("\n{}", format!("{} ({}):", category, items.len()).cyan());
            for (path, line_num, content) in items {
                println!("  {}:{}", path, line_num);
                println!("    {}", content);
            }
        }
    }

    Ok(())
}

/// Find function callers
pub fn cmd_callers(
    root: &Path,
    function_name: &str,
    limit: usize,
    format: &str,
    scope: &db::SearchScope<'_>,
) -> Result<()> {
    let pattern = format!(
        "{}|{}",
        build_caller_pattern(function_name),
        regex::escape(function_name)
    );
    let def_pattern = build_def_skip_pattern(function_name);
    let conn = db::open_db_leased(root)?;
    let resolver = PathResolver::try_from_conn(root, &conn)?.with_decoration(format != "json");
    let roots = resolver.grep_roots();
    // Every caller idiom contains the name itself.
    let word_index = super::WordIndex::load(root, &conn)?;
    let prefilter = word_index
        .as_ref()
        .and_then(|words| words.prefilter(&[function_name]));

    // Retain one parsed Java file, so project-sized caller scans stay bounded.
    let mut java_calls = None;
    let mut java_error = None;
    let caller_regex = Regex::new(&build_caller_pattern(function_name))?;
    let page = super::search_files_page_in_selected(
        root,
        &roots,
        &pattern,
        &ALL_SOURCE_EXTENSIONS,
        limit,
        prefilter.as_ref(),
        &|path| {
            resolver
                .scoped_relative_path(path)
                .is_some_and(|relative| scope.matches_path(&relative))
        },
        &|path, line| {
            path.extension().is_some_and(|ext| ext == "java") || !def_pattern.is_match(line)
        },
        |path, line_num, line| {
            let scoped_path = resolver.scoped_relative_path(path)?;
            if !scope.matches_path(&scoped_path) {
                return None;
            }
            let rel_path = super::display_path(&resolver, root, path);
            if path.extension().is_some_and(|ext| ext == "java") {
                if java_calls.as_ref().is_none_or(
                    |(cached, _): &(PathBuf, std::collections::HashSet<usize>)| cached != path,
                ) {
                    let calls =
                        match read_java_syntax_source(path, crate::indexer::max_file_size_bytes())
                            .and_then(|content| {
                                crate::parsers::treesitter::java::invocation_lines(
                                    &content,
                                    function_name,
                                )
                            }) {
                            Ok(calls) => calls,
                            Err(error) => {
                                java_error = Some(error);
                                return None;
                            }
                        };
                    java_calls = Some((path.to_path_buf(), calls));
                }
                if !java_calls.as_ref().unwrap().1.contains(&line_num) {
                    return None;
                }
            } else if !caller_regex.is_match(line) {
                return None;
            }
            let content: String = line.chars().take(70).collect();
            Some((rel_path, line_num, content))
        },
    )?;
    if let Some(error) = java_error {
        return Err(error);
    }

    if format == "json" {
        let items: Vec<_> = page
            .items
            .iter()
            .map(|(path, line, content)| {
                serde_json::json!({"path": path, "line": line, "content": content})
            })
            .collect();
        let result = serde_json::json!({
            "schema_version": super::PAGINATED_JSON_SCHEMA_VERSION,
            "items": items,
            "pagination": page.pagination,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "Callers of '{}' (showing {} of {}):",
            function_name, page.pagination.returned, page.pagination.total
        )
        .bold()
    );

    let mut by_file: HashMap<&str, Vec<(usize, &str)>> = HashMap::new();
    for (path, line, content) in &page.items {
        by_file
            .entry(path)
            .or_default()
            .push((*line, content.as_str()));
    }
    let mut paths: Vec<_> = by_file.into_iter().collect();
    paths.sort_by(|left, right| left.0.cmp(right.0));
    for (path, items) in paths {
        println!("\n  {}:", path.cyan());
        for (line_num, content) in items {
            println!("    :{} {}", line_num, content);
        }
    }
    print_truncation_notice(page.pagination);

    Ok(())
}

/// Show call hierarchy (callers tree) for a function
pub fn cmd_call_tree(
    root: &Path,
    function_name: &str,
    max_depth: usize,
    limit_per_level: usize,
    format: &str,
    scope: &db::SearchScope<'_>,
) -> Result<()> {
    // A missing or unreadable index is not fatal here: attribution falls back
    // to the textual scan that predates the index.
    let conn = db::open_db_leased(root).ok();

    let callers = collect_tree_callers(
        root,
        conn.as_deref(),
        function_name,
        max_depth,
        limit_per_level,
        scope,
        format != "json",
    )?;
    if format == "json" {
        let mut items = Vec::new();
        walk_call_tree(
            function_name,
            max_depth,
            &callers,
            &mut |depth, site, node| {
                let status = match node {
                    TreeNode::Shown => "shown",
                    TreeNode::ExpandedAbove => "expanded_above",
                    TreeNode::Recursive => "recursive",
                };
                items.push(serde_json::json!({
                    "depth": depth, "name": site.name, "path": site.path, "line": site.line,
                    "status": status,
                }));
            },
        );
        let result = serde_json::json!({
            "schema_version": super::PAGINATED_JSON_SCHEMA_VERSION,
            "function": function_name,
            "max_depth": max_depth,
            "limit_per_level": limit_per_level,
            "count": items.len(),
            "items": items,
        });
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    println!("{}", format!("Call tree for '{}':", function_name).bold());
    println!("  {}", function_name.cyan());
    walk_call_tree(
        function_name,
        max_depth,
        &callers,
        &mut |depth, site, node| {
            let (caller, file_path, line_num) = (&site.name, &site.path, site.line);
            let indent = "  ".repeat(depth + 1);
            match node {
                TreeNode::Shown => println!(
                    "{}← {} ({}:{})",
                    indent,
                    caller.yellow(),
                    file_path,
                    line_num
                ),
                TreeNode::ExpandedAbove => println!(
                    "{}← {} ({}:{}) {}",
                    indent,
                    caller.yellow(),
                    file_path,
                    line_num,
                    "(expanded above)".dimmed()
                ),
                TreeNode::Recursive => println!("{}← {} (recursive)", indent, caller.dimmed()),
            }
        },
    );

    Ok(())
}

/// Java graph ids retain owner and overload identity through every level.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum CallerTarget {
    Name(String),
    JavaSymbol(i64),
}

/// A calling declaration, with a lookup identity separate from its display.
#[derive(Clone, Debug, PartialEq, Eq)]
struct CallerSite {
    name: String,
    path: String,
    line: usize,
    target: CallerTarget,
    callable: bool,
}

impl CallerSite {
    fn lexical(name: String, path: String, line: usize) -> Self {
        Self {
            target: CallerTarget::Name(name.clone()),
            name,
            path,
            line,
            callable: true,
        }
    }
}

/// Calling functions of one function.
type CallerSites = Vec<CallerSite>;

/// How one edge of the call tree is shown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TreeNode {
    /// With its callers below it, when it has any within the depth limit.
    Shown,
    /// This lookup identity already has its callers shown in the tree.
    ExpandedAbove,
    /// The same definition as a function on the path above it: a cycle.
    Recursive,
}

/// Callers of every function the printed tree expands.
///
/// Each function takes a scan of the whole repository to find its callers,
/// and one scan per function made the cost grow with the tree's width. So the
/// depth-first walk the tree is printed in is replayed against what is known
/// so far, every function it still lacks is looked up in a single shared scan,
/// and the replay repeats until nothing is missing — about one scan per level.
/// A shared scan gives each function the lines a scan of its own would, so the
/// tree comes out the same. The repository is walked once, on the first scan,
/// and every scan goes over the same list of files.
fn collect_tree_callers(
    root: &Path,
    conn: Option<&rusqlite::Connection>,
    function_name: &str,
    max_depth: usize,
    limit: usize,
    scope: &db::SearchScope<'_>,
    decorate_paths: bool,
) -> Result<HashMap<CallerTarget, CallerSites>> {
    let mut callers = HashMap::new();
    if limit == 0 {
        return Ok(callers);
    }
    let mut files: Option<Vec<PathBuf>> = None;
    let resolver = conn
        .map(|conn| PathResolver::try_from_conn(root, conn))
        .transpose()?;
    let word_index = match conn {
        Some(conn) => super::WordIndex::load(root, conn)?,
        None => None,
    };
    loop {
        let missing = walk_call_tree(function_name, max_depth, &callers, &mut |_, _, _| {});
        if missing.is_empty() {
            return Ok(callers);
        }
        let files = match files {
            Some(ref files) => files,
            None => {
                let mut selected = super::project_source_files(root, &ALL_SOURCE_EXTENSIONS)?;
                // Apply ownership and owner-relative selectors before every
                // level's limits, for both graph and syntax attribution.
                selected.retain(|path| {
                    let relative = match &resolver {
                        Some(resolver) => resolver.scoped_relative_path(path),
                        None => Some(relative_path(root, path)),
                    };
                    relative.is_some_and(|relative| scope.matches_path(&relative))
                });
                selected.dedup();
                files.insert(selected)
            }
        };
        // Skipping files that hold none of the names keeps the path order of
        // the rest, so each name still gets the same first lines.
        let names: Vec<&str> = missing
            .iter()
            .filter_map(|target| match target {
                CallerTarget::Name(name) => Some(name.as_str()),
                CallerTarget::JavaSymbol(_) => None,
            })
            .collect();
        let prefilter = if names.len() == missing.len() {
            word_index
                .as_ref()
                .and_then(|words| words.prefilter(&names))
        } else {
            None
        };
        let found = find_caller_functions(
            root,
            conn,
            files,
            &missing,
            limit,
            prefilter.as_ref(),
            decorate_paths,
        )?;
        callers.extend(missing.into_iter().zip(found));
    }
}

/// Visit the call tree depth-first, in print order, handing every edge to
/// `visit` as `(depth, caller, node)`.
///
/// Every caller is a definition of its own, shown with its file even when a
/// function of the same name from another file is already in the tree: two
/// `it "works"` blocks are two callers. Each lookup identity is expanded
/// once, at its first node; a later node of that identity is
/// [`TreeNode::ExpandedAbove`], and one that is the very definition
/// of a function on its own path is [`TreeNode::Recursive`]. A caller whose
/// name no code can call is shown but not expanded; see [`is_callable_name`].
///
/// Returns the functions whose callers the walk needed but `callers` lacks;
/// their subtrees are skipped, so a walk with anything missing is only a
/// draft of the final one.
fn walk_call_tree<'a>(
    function_name: &'a str,
    max_depth: usize,
    callers: &'a HashMap<CallerTarget, CallerSites>,
    visit: &mut dyn FnMut(usize, &'a CallerSite, TreeNode),
) -> Vec<CallerTarget> {
    let target = CallerTarget::Name(function_name.to_string());
    let mut walk = TreeWalk {
        max_depth,
        callers,
        expanded: std::collections::HashSet::from([target.clone()]),
        root_target: target.clone(),
        path: Vec::new(),
        missing: Vec::new(),
        visit,
    };
    walk.callers_of(&target, 1);
    walk.missing
}

struct TreeWalk<'a, 'v> {
    max_depth: usize,
    callers: &'a HashMap<CallerTarget, CallerSites>,
    /// Lookup identities whose callers the tree shows already.
    expanded: std::collections::HashSet<CallerTarget>,
    /// The initial name query already expands all matching Java declarations.
    root_target: CallerTarget,
    /// Callers from the root down to the node being expanded.
    path: Vec<&'a CallerSite>,
    missing: Vec<CallerTarget>,
    visit: &'v mut dyn FnMut(usize, &'a CallerSite, TreeNode),
}

impl<'a> TreeWalk<'a, '_> {
    fn callers_of(&mut self, target: &CallerTarget, depth: usize) {
        if depth > self.max_depth {
            return;
        }
        let callers = self.callers;
        let Some(sites) = callers.get(target) else {
            if !self.missing.contains(target) {
                self.missing.push(target.clone());
            }
            return;
        };
        for site in sites {
            if self.path.contains(&site) {
                (self.visit)(depth, site, TreeNode::Recursive);
                continue;
            }
            let expandable =
                depth < self.max_depth && site.callable && is_callable_name(&site.name);
            let expansion = match (&self.root_target, &site.target) {
                (CallerTarget::Name(name), CallerTarget::JavaSymbol(_)) if name == &site.name => {
                    &self.root_target
                }
                _ => &site.target,
            };
            if expandable && self.expanded.insert(expansion.clone()) {
                (self.visit)(depth, site, TreeNode::Shown);
                self.path.push(site);
                self.callers_of(&site.target, depth + 1);
                self.path.pop();
                continue;
            }
            // Expanded earlier, so its callers are known unless this walk is
            // still a draft; a name without callers leaves nothing out.
            let above = expandable
                && callers
                    .get(expansion)
                    .is_some_and(|sites| !sites.is_empty());
            let node = if above {
                TreeNode::ExpandedAbove
            } else {
                TreeNode::Shown
            };
            (self.visit)(depth, site, node);
        }
    }
}

/// Whether source code can call `name`, i.e. whether it is an identifier:
/// word characters and the `$`, `#`, `-` some languages allow in names,
/// joined by `::` or `.` (`Applicant::MergeService`, `self.call`), with an
/// optional trailing `!`, `?` or `=` (Ruby `save!`, `valid?`, `name=`).
///
/// The index also names blocks that nothing calls by name — `it "works"`,
/// `let(:user)`, `describe "Foo"`, `attributes :id`. Such a block does own
/// the call lines inside it, so it is a caller worth showing, but looking
/// for calls to it would cost a scan of the whole repository and find none.
fn is_callable_name(name: &str) -> bool {
    let body = name.strip_suffix(&['!', '?', '='][..]).unwrap_or(name);
    let mut has_word = false;
    let mut chars = body.chars();
    while let Some(c) = chars.next() {
        match c {
            ':' => {
                if chars.next() != Some(':') {
                    return false;
                }
            }
            '.' | '$' | '#' | '-' => {}
            c if c.is_alphanumeric() || c == '_' => has_word = true,
            _ => return false,
        }
    }
    has_word
}

/// Find the calling declarations for each target within selected `files`.
///
/// A fresh Java graph retains declaration ids; Java fallback uses syntax.
/// The existing text scan handles call idioms not stored in `refs`, with
/// indexed ranges attributing their owners when available.
///
/// Each function gets the first `limit * 3` call lines in path order. A
/// definition line or a file outside `in_file` does not count against that:
/// in path order definitions cluster (every worker in `app/workers` defines
/// `perform`) and would leave no budget for the calls.
fn find_caller_functions(
    root: &Path,
    conn: Option<&rusqlite::Connection>,
    files: &[PathBuf],
    function_targets: &[CallerTarget],
    limit: usize,
    prefilter: Option<&super::WordPrefilter<'_>>,
    decorate_paths: bool,
) -> Result<Vec<CallerSites>> {
    // Java syntax distinguishes calls from prose, declarations and method
    // references, and attributes calls even when declarations share a line.
    // Parse one file at a time; retain at most `limit` owners per requested name.
    let mut resolved_callers: Vec<CallerSites> = vec![Vec::new(); function_targets.len()];
    let mut graph_answered = vec![false; function_targets.len()];
    let resolver =
        conn.map(|conn| PathResolver::from_conn(root, conn).with_decoration(decorate_paths));
    if let Some(conn) = conn {
        let state = db::symbol_graph_state(conn)?;
        if state.built && !state.stale {
            let resolver = resolver.as_ref().unwrap();
            let selected: HashSet<&Path> = files.iter().map(PathBuf::as_path).collect();
            let absolute = |path: &str, root_path: Option<&str>| {
                let path = PathBuf::from(resolver.resolve_with_root_raw(path, root_path));
                if path.is_absolute() {
                    path
                } else {
                    root.join(path)
                }
            };
            for ((target, sites), answered) in function_targets
                .iter()
                .zip(resolved_callers.iter_mut())
                .zip(graph_answered.iter_mut())
            {
                // A receiver spelling (p.leaf, this.leaf) selects syntax
                // occurrences, not the declaration name stored in the graph.
                let targets: Vec<db::GraphSymbolInfo> = match target {
                    CallerTarget::Name(name) if name.contains('.') => continue,
                    CallerTarget::Name(name) => db::find_graph_symbols_by_name(conn, name)?
                        .into_iter()
                        .filter(|symbol| {
                            symbol.kind == "function" && symbol.path.ends_with(".java")
                        })
                        .collect(),
                    CallerTarget::JavaSymbol(id) => db::load_graph_symbol_infos(conn, &[*id])?
                        .into_values()
                        .collect(),
                };
                let callers =
                    super::graph::resolved_callers_of_filtered(conn, &targets, limit, |source| {
                        let path = absolute(&source.path, source.root_path.as_deref());
                        matches!(source.kind.as_str(), "function" | "property" | "constant")
                            && source.path.ends_with(".java")
                            && selected.contains(path.as_path())
                            // A forced nested root may leave a second indexed
                            // row through its parent. Keep the most specific
                            // owner without merging distinct overload ids.
                            && resolver
                                .scoped_relative_path(&path)
                                .is_some_and(|relative| relative == source.path)
                    })?;
                for source in callers {
                    let path = absolute(&source.path, source.root_path.as_deref());
                    sites.push(CallerSite {
                        target: CallerTarget::JavaSymbol(source.id),
                        name: source.name,
                        path: super::display_path(resolver, root, &path),
                        line: source.line as usize,
                        callable: source.kind == "function",
                    });
                }
                sites.sort_by(|a, b| (&a.path, a.line, &a.name).cmp(&(&b.path, b.line, &b.name)));
                sites.dedup();
                sites.truncate(limit);
                *answered = true;
            }
        }
    }
    // Only name queries enter lexical matching. Exact Java declarations have
    // no fallback to similarly named occurrences in Java or another language.
    let mut lexical_indices = Vec::new();
    let mut function_names = Vec::new();
    for (index, target) in function_targets.iter().enumerate() {
        if let CallerTarget::Name(name) = target {
            lexical_indices.push(index);
            function_names.push(name.clone());
        }
    }
    if lexical_indices.is_empty() {
        return Ok(resolved_callers);
    }
    let mut java_callers: Vec<CallerSites> = lexical_indices
        .iter()
        .map(|index| std::mem::take(&mut resolved_callers[*index]))
        .collect();
    let graph_answered: Vec<bool> = lexical_indices
        .iter()
        .map(|index| graph_answered[*index])
        .collect();
    let mut other_files = Vec::new();
    for path in files {
        if !path
            .extension()
            .is_some_and(|extension| extension == "java")
        {
            other_files.push(path.clone());
            continue;
        }
        // A fresh graph is authoritative about resolved Java calls, even
        // when the set is empty. A lexical fallback would invent a caller
        // dispatched to an external or different receiver type.
        if graph_answered.iter().all(|answered| *answered) {
            continue;
        }
        let rel = resolver.as_ref().map_or_else(
            || relative_path(root, path),
            |resolver| super::display_path(resolver, root, path),
        );
        if prefilter.is_some_and(|filter| !filter.may_contain(path))
            || java_callers.iter().all(|sites| sites.len() >= limit)
        {
            continue;
        }
        let content = read_java_syntax_source(path, crate::indexer::max_file_size_bytes())?;
        let owners = crate::parsers::treesitter::java::invocation_caller_sites(
            &content,
            &function_names,
            limit,
        )?;
        for ((sites, owners), answered) in java_callers.iter_mut().zip(owners).zip(&graph_answered)
        {
            if *answered {
                continue;
            }
            let remaining = limit.saturating_sub(sites.len());
            sites.extend(owners.into_iter().take(remaining).map(|owner| CallerSite {
                target: CallerTarget::Name(owner.name.clone()),
                name: owner.name,
                path: rel.clone(),
                line: owner.line,
                callable: owner.callable,
            }));
        }
    }
    let patterns: Vec<(String, String)> = function_names
        .iter()
        .map(|name| (build_caller_pattern(name), name.clone()))
        .collect();
    let def_patterns: Vec<DefinitionPattern> = function_names
        .iter()
        .map(|name| build_def_skip_pattern(name))
        .collect();

    // Pattern to find function definitions (for locating the containing function)
    // Group 1: fun/func/function/def/sub style, Group 2: Ruby def/def self., Group 3: Java return-type style, Group 4: TS arrow function
    let func_def_re = Regex::new(concat!(
        r"(?:fun|function|func|sub)\s+(\w+)\s*[<(\[]",
        r"|\bdef\s+(?:self\.)?(\w[!\w?]*)",
        r"|(?:(?:public|private|protected|static|final|abstract|synchronized|override|export|async)\s+)*",
        r"(?:void|int|long|boolean|char|byte|short|float|double|[\w.]+(?:<[^{;]*>)?(?:\[\])*)\s+(\w+)\s*\(",
        r"|(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[a-zA-Z_]\w*)\s*(?::\s*[^=]+)?\s*=>",
    ))?;

    let mut files_with_calls: Vec<BTreeMap<PathBuf, Vec<usize>>> =
        function_names.iter().map(|_| BTreeMap::new()).collect();

    // First pass: find all files and line numbers with calls
    super::search_files_limited_each_prefiltered(
        &other_files,
        &build_any_caller_pattern(&function_names),
        &patterns,
        limit * 3,
        prefilter,
        |index, _path, line| !def_patterns[index].is_match(line),
        |index, path, line_num, _line| {
            files_with_calls[index]
                .entry(path.to_path_buf())
                .or_default()
                .push(line_num);
        },
    )?;

    let root_key = db::normalize_root_for_storage(root);
    let lexical_callers: Vec<CallerSites> = files_with_calls
        .into_iter()
        .zip(&function_names)
        .zip(java_callers)
        .map(|((files, name), mut java_sites)| {
            java_sites.extend(attribute_call_lines(
                root,
                &root_key,
                conn,
                name,
                files,
                limit,
                &func_def_re,
            ));
            java_sites.sort_by(|a, b| (&a.path, a.line, &a.name).cmp(&(&b.path, b.line, &b.name)));
            java_sites.truncate(limit);
            java_sites
        })
        .collect();
    for (index, sites) in lexical_indices.into_iter().zip(lexical_callers) {
        resolved_callers[index] = sites;
    }
    Ok(resolved_callers)
}

/// Second pass of [`find_caller_functions`]: the function containing each
/// call line of `function_name`, at most `limit` distinct ones, the first in
/// path order. A line the index knows to declare a definition of that name
/// is skipped: the match there is the definition, in a form the textual
/// definition filter does not know (`func (s *Server) Handle(`, a JavaScript
/// method `handle(event) {`, `attr_reader :handle`), not a call.
///
/// Every file lies under the primary root `root`, stored as `root_key`.
fn attribute_call_lines(
    root: &Path,
    root_key: &str,
    conn: Option<&rusqlite::Connection>,
    function_name: &str,
    files_with_calls: BTreeMap<PathBuf, Vec<usize>>,
    limit: usize,
    func_def_re: &Regex,
) -> CallerSites {
    let defined_name = short_name(function_name).unwrap_or(function_name);
    let mut results: CallerSites = vec![];

    for (file_path, call_lines) in files_with_calls {
        if results.len() >= limit {
            break;
        }

        let rel_path = relative_path(root, &file_path);
        // When the index has ranges for this file, "no owner" is an answer,
        // not a gap: the call sits at module level and has no calling
        // function. Only a file the index cannot speak for gets the scan,
        // and only such a file has to be read off disk at all.
        let ranges_known = conn
            .map(|conn| {
                db::file_has_symbol_ranges(conn, Some(root_key), &rel_path).unwrap_or(false)
            })
            .unwrap_or(false);
        let content = if ranges_known {
            String::new()
        } else {
            match std::fs::read_to_string(&file_path) {
                Ok(content) => content,
                Err(_) => continue,
            }
        };
        let lines: Vec<&str> = content.lines().collect();

        for call_line in call_lines {
            if results.len() >= limit {
                break;
            }
            let declares_target = conn.is_some_and(|conn| {
                db::find_definitions_on_line(conn, Some(root_key), &rel_path, call_line as i64)
                    .unwrap_or_default()
                    .iter()
                    .any(|name| short_name(name).unwrap_or(name) == defined_name)
            });
            if declares_target {
                continue;
            }

            let owner = conn
                .and_then(|conn| {
                    db::find_owning_symbol(conn, Some(root_key), &rel_path, call_line as i64)
                        .unwrap_or(None)
                })
                .map(|symbol| (symbol.name, symbol.line as usize));
            let owner = match owner {
                Some(owner) => Some(owner),
                None if ranges_known => None,
                None => find_containing_function(&lines, call_line, func_def_re),
            };

            if let Some((func_name, func_line)) = owner {
                // Avoid adding the same function twice for this target
                if !results
                    .iter()
                    .any(|site| site.name == func_name && site.path == rel_path)
                {
                    results.push(CallerSite::lexical(func_name, rel_path.clone(), func_line));
                }
            }
        }
    }

    results
}

/// Find the function that contains a given line number
fn find_containing_function(
    lines: &[&str],
    target_line: usize,
    func_def_re: &Regex,
) -> Option<(String, usize)> {
    // Search backwards from the target line to find a function definition
    let start_idx = (target_line.saturating_sub(1)).min(lines.len().saturating_sub(1));

    for i in (0..=start_idx).rev() {
        let line = lines[i];
        if let Some(caps) = func_def_re.captures(line) {
            // Group 1: fun/function/func/sub, Group 2: Ruby def, Group 3: Java return-type, Group 4: TS arrow
            if let Some(name) = caps
                .get(1)
                .or_else(|| caps.get(2))
                .or_else(|| caps.get(3))
                .or_else(|| caps.get(4))
            {
                return Some((name.as_str().to_string(), i + 1));
            }
        }
    }

    None
}

/// Find Dagger @Provides/@Binds for a type
pub fn cmd_provides(root: &Path, type_name: &str, limit: usize, format: &str) -> Result<()> {
    let results = super::annotation_functions::find(
        root,
        &["Provides", "Binds"],
        &["kt", "kts", "java"],
        Some(type_name),
        true,
        limit,
    )?;
    if format == "json" {
        let items: Vec<_> = results
            .iter()
            .map(|item| {
                serde_json::json!({"path": item.path, "line": item.line, "name": item.name,
                    "content": item.signature.chars().take(100).collect::<String>()})
            })
            .collect();
        return print_search_json(&items);
    }
    println!(
        "{}",
        format!("Providers for '{}' ({}):", type_name, results.len()).bold()
    );
    for item in results {
        println!("  {}:{}", item.path, item.line);
        println!(
            "    {}",
            item.signature.chars().take(100).collect::<String>()
        );
    }
    Ok(())
}

/// Captures a suspend function's name, skipping type parameters and an extension
/// receiver (`suspend fun <T> Foo<T>.bar(`) so the receiver type isn't reported.
const SUSPEND_FUN_NAME_PATTERN: &str =
    r"\bsuspend\s+fun\s+(?:<[^>]*>\s*)?(?:[\w.<>?,* ]+\.)?`?(\w+)`?\s*[(<]";

/// Find suspend functions
pub fn cmd_suspend(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    let func_regex = Regex::new(SUSPEND_FUN_NAME_PATTERN)?;

    let mut suspends: Vec<(String, String, usize)> = vec![];

    search_files_filtered(
        root,
        SUSPEND_FUN_NAME_PATTERN,
        &["kt", "kts"],
        limit,
        |_, line| {
            func_regex.captures(line).is_some_and(|caps| {
                query.is_none_or(|q| caps[1].to_lowercase().contains(&q.to_lowercase()))
            })
        },
        |path, line_num, line| {
            if let Some(caps) = func_regex.captures(line) {
                let func_name = caps.get(1).unwrap().as_str().to_string();

                let rel_path = relative_path(root, path);
                suspends.push((func_name, rel_path, line_num));
            }
        },
    )?;

    println!(
        "{}",
        format!("Suspend functions ({}):", suspends.len()).bold()
    );

    for (func_name, path, line_num) in &suspends {
        println!("  {}: {}:{}", func_name.cyan(), path, line_num);
    }

    Ok(())
}

/// Find @Composable functions
pub fn cmd_composables(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_annotated_functions(root, "Composable", query, limit)
}

fn print_annotated_functions(
    root: &Path,
    annotation: &str,
    query: Option<&str>,
    limit: usize,
) -> Result<()> {
    let items = super::annotation_functions::find(
        root,
        &[annotation],
        &["kt", "kts"],
        query,
        false,
        limit,
    )?;
    println!(
        "{}",
        format!("@{} functions ({}):", annotation, items.len()).bold()
    );
    for item in items {
        println!("  {}: {}:{}", item.name.cyan(), item.path, item.line);
    }
    Ok(())
}

/// Find @Deprecated annotations
pub fn cmd_deprecated(root: &Path, query: Option<&str>, limit: usize, format: &str) -> Result<()> {
    // Kotlin/Java/C#: @Deprecated/@Obsolete, Swift: @available(*, deprecated)
    // Python: @deprecated, Perl: DEPRECATED, Rust: #[deprecated], Go: // Deprecated:
    // JS/TS: @deprecated (JSDoc), PHP: @deprecated (PHPDoc), C++: [[deprecated]]
    let pattern = pattern_with_line_filter(
        r"@Deprecated|@Obsolete|@available\s*\([^)]*deprecated|#\[deprecated|#.*DEPRECATED|=head.*DEPRECATED|@deprecated|\[\[deprecated",
        query,
    );

    let mut items: Vec<(String, usize, String)> = vec![];

    search_files_limited(
        root,
        &pattern,
        &ALL_SOURCE_EXTENSIONS,
        limit,
        |path, line_num, line| {
            if let Some(q) = query {
                if !line.to_lowercase().contains(&q.to_lowercase()) {
                    return;
                }
            }

            let rel_path = relative_path(root, path);
            let content: String = line.trim().chars().take(80).collect();
            items.push((rel_path, line_num, content));
        },
    )?;

    if format == "json" {
        return print_line_search_json(&items);
    }
    println!("{}", format!("@Deprecated items ({}):", items.len()).bold());

    for (path, line_num, content) in &items {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }

    Ok(())
}

/// Find @Suppress annotations
pub fn cmd_suppress(root: &Path, query: Option<&str>, limit: usize, format: &str) -> Result<()> {
    let pattern =
        pattern_with_line_filter(r"@(?:[\w$]+:)?(?:[\w$]+\.)*Suppress(?:Warnings)?\b", query);

    let mut items: Vec<(String, usize, String)> = vec![];

    search_files_limited(
        root,
        &pattern,
        &["kt", "java"],
        limit,
        |path, line_num, line| {
            if let Some(q) = query {
                if !line.to_lowercase().contains(&q.to_lowercase()) {
                    return;
                }
            }

            let rel_path = relative_path(root, path);
            let content: String = line.trim().chars().take(80).collect();
            items.push((rel_path, line_num, content));
        },
    )?;

    if format == "json" {
        return print_line_search_json(&items);
    }
    println!(
        "{}",
        format!("@Suppress annotations ({}):", items.len()).bold()
    );

    for (path, line_num, content) in &items {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }

    Ok(())
}

/// Find @Inject/@Autowired points for a type: field/setter injection and
/// constructor parameters (`class Foo @Inject constructor(bar: Bar)`), where
/// the type usually sits several lines below the annotation.
pub fn cmd_inject(root: &Path, type_name: &str, limit: usize, format: &str) -> Result<()> {
    let type_pattern = format!(r"\b{}\b", regex::escape(type_name));
    let type_re = Regex::new(&type_pattern)?;

    let mut candidate_files: std::collections::BTreeSet<PathBuf> = Default::default();
    search_files_limited(
        root,
        &type_pattern,
        &["kt", "java"],
        100_000,
        |path, _line_num, _line| {
            candidate_files.insert(path.to_path_buf());
        },
    )?;

    let mut items: Vec<(String, usize, String)> = vec![];
    for path in &candidate_files {
        if items.len() >= limit {
            break;
        }
        let java = path
            .extension()
            .is_some_and(|extension| extension == "java");
        let content = if java {
            read_java_syntax_source(path, crate::indexer::max_file_size_bytes())?
        } else {
            let Ok(content) = std::fs::read_to_string(path) else {
                continue;
            };
            content
        };
        if !content.contains("Inject") && !content.contains("Autowired") {
            continue;
        }
        let lines: Vec<&str> = content.lines().collect();
        let rel_path = relative_path(root, path);
        let injection_sites = if java {
            crate::parsers::treesitter::java::injection_lines(&content, &type_re)?
        } else {
            injection_lines(&content, &type_re)
        };
        for line_idx in injection_sites {
            if items.len() >= limit {
                break;
            }
            let text: String = lines
                .get(line_idx)
                .map(|l| l.trim().chars().take(80).collect())
                .unwrap_or_default();
            items.push((rel_path.clone(), line_idx + 1, text));
        }
    }

    if format == "json" {
        return print_line_search_json(&items);
    }
    println!(
        "{}",
        format!("Injection points for '{}' ({}):", type_name, items.len()).bold()
    );

    for (path, line_num, content) in &items {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }

    Ok(())
}

static DI_ANNOTATION_RE: std::sync::LazyLock<Regex> = std::sync::LazyLock::new(|| {
    Regex::new(r"@(?:\w+:)?(?:Inject|Autowired)\b").expect("valid DI annotation regex")
});

static LEADING_ANNOTATION_RE: std::sync::LazyLock<Regex> = std::sync::LazyLock::new(|| {
    Regex::new(r"^\s*@[\w.:]+(?:\s*\([^()]*\))?").expect("valid annotation regex")
});

/// 0-based line indices where `type_re` occurs inside an injection site:
/// the parameter list following `@Inject` (constructor / method injection),
/// or the declaration line of an injected field or property.
fn injection_lines(content: &str, type_re: &Regex) -> Vec<usize> {
    let mut lines = std::collections::BTreeSet::new();
    for di in DI_ANNOTATION_RE.find_iter(content) {
        let mut decl_start = di.end();
        while let Some(a) = LEADING_ANNOTATION_RE.find(&content[decl_start..]) {
            decl_start += a.end();
        }
        let rest = &content[decl_start..];
        let stop = rest.find(['(', ';', '=', '{', '}']);
        let head = &rest[..stop.unwrap_or(rest.len())];
        let is_property = head.split_whitespace().any(|w| w == "var" || w == "val");

        let span = match stop {
            Some(i) if !is_property && rest.as_bytes()[i] == b'(' => {
                let open = decl_start + i;
                match matching_paren(content, open) {
                    Some(close) => open..close,
                    None => continue,
                }
            }
            _ => {
                let offset = rest.len() - rest.trim_start().len();
                let line_start = decl_start + offset;
                let line_end = content[line_start..]
                    .find('\n')
                    .map_or(content.len(), |n| line_start + n);
                let decl_end = stop.map_or(content.len(), |i| decl_start + i);
                line_start..line_end.min(decl_end).max(line_start)
            }
        };

        for m in type_re.find_iter(&content[span.clone()]) {
            let pos = span.start + m.start();
            lines.insert(content[..pos].matches('\n').count());
        }
    }
    lines.into_iter().collect()
}

/// Byte offset of the `)` that closes the `(` at `open`, if any.
fn matching_paren(content: &str, open: usize) -> Option<usize> {
    let mut depth = 0usize;
    for (i, b) in content.as_bytes()[open..].iter().enumerate() {
        match b {
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(open + i);
                }
            }
            _ => {}
        }
    }
    None
}

/// Find uses of specific annotation
pub fn cmd_annotations(root: &Path, annotation: &str, limit: usize, format: &str) -> Result<()> {
    // Normalize annotation (add @ if missing for Java/Kotlin/Swift/ObjC)
    // For Perl, attributes are like :lvalue, :method
    let search_annotation = if annotation.starts_with('@') || annotation.starts_with(':') {
        annotation.to_string()
    } else {
        format!("@{}", annotation)
    };
    let pattern = regex::escape(&search_annotation);

    let mut items: Vec<(String, usize, String)> = vec![];

    search_files_limited(
        root,
        &pattern,
        &ALL_SOURCE_EXTENSIONS,
        limit,
        |path, line_num, line| {
            let rel_path = relative_path(root, path);
            let content: String = line.trim().chars().take(80).collect();
            items.push((rel_path, line_num, content));
        },
    )?;

    if format == "json" {
        return print_line_search_json(&items);
    }
    println!(
        "{}",
        format!("Classes with {} ({}):", search_annotation, items.len()).bold()
    );

    for (path, line_num, content) in &items {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }

    Ok(())
}

/// Find deeplink definitions
pub fn cmd_deeplinks(root: &Path, query: Option<&str>, limit: usize, format: &str) -> Result<()> {
    // Search for specific deeplink patterns (NOT generic :// URLs)
    // Android: @DeepLink, DeepLinkHandler, @AppLink, NavDeepLink, intent-filter with android:scheme
    // iOS: openURL, application(_:open:, handleOpen, CFBundleURLSchemes, UniversalLink
    let pattern = pattern_with_line_filter(
        r#"[Dd]eep[Ll]ink|@DeepLink|DeepLinkHandler|@AppLink|NavDeepLink|android:scheme|openURL|application\([^)]*open:|handleOpen|CFBundleURLSchemes|UniversalLink|NSUserActivity"#,
        query,
    );

    let mut items: Vec<(String, usize, String)> = vec![];

    search_files_limited(
        root,
        &pattern,
        &["kt", "java", "xml", "swift", "m", "h", "plist"],
        limit,
        |path, line_num, line| {
            if let Some(q) = query {
                if !line.to_lowercase().contains(&q.to_lowercase()) {
                    return;
                }
            }

            let rel_path = relative_path(root, path);
            let content: String = line.trim().chars().take(100).collect();
            items.push((rel_path, line_num, content));
        },
    )?;

    if format == "json" {
        return print_line_search_json(&items);
    }
    println!("{}", format!("Deeplinks ({}):", items.len()).bold());

    for (path, line_num, content) in &items {
        println!("  {}:{}", path.cyan(), line_num);
        println!("    {}", content);
    }

    Ok(())
}

/// Find extension functions/types
pub fn cmd_extensions(root: &Path, receiver_type: &str, limit: usize) -> Result<()> {
    // Kotlin: fun ReceiverType.functionName
    // Swift: extension ReceiverType
    let kotlin_pattern = format!(r"\bfun\s+{}\.(\w+)", regex::escape(receiver_type));
    let swift_pattern = format!(
        r"\bextension\s+{}(?:\s|[<:{{]|$)",
        regex::escape(receiver_type)
    );
    let pattern = format!(r"{}|{}", kotlin_pattern, swift_pattern);

    let kotlin_regex = Regex::new(&kotlin_pattern)?;
    let swift_regex = Regex::new(&swift_pattern)?;

    let mut items: Vec<(String, String, usize, String)> = vec![]; // (name, path, line, lang)

    search_files_filtered(
        root,
        &pattern,
        &["kt", "kts", "swift"],
        limit,
        |path, line| {
            if path.extension().is_some_and(|ext| ext == "swift") {
                swift_regex.is_match(line)
            } else {
                kotlin_regex.is_match(line)
            }
        },
        |path, line_num, line| {
            let rel_path = relative_path(root, path);

            if path.extension().is_some_and(|ext| ext != "swift") {
                let caps = kotlin_regex
                    .captures(line)
                    .expect("accepted Kotlin extension");
                let func_name = caps.get(1).unwrap().as_str().to_string();
                items.push((func_name, rel_path, line_num, "kt".to_string()));
            } else if swift_regex.is_match(line) {
                let content: String = line.trim().chars().take(60).collect();
                items.push((content, rel_path, line_num, "swift".to_string()));
            }
        },
    )?;

    println!(
        "{}",
        format!("Extensions for {} ({}):", receiver_type, items.len()).bold()
    );

    for (name, path, line_num, lang) in &items {
        if lang == "kt" {
            println!("  {}.{}: {}:{}", receiver_type.cyan(), name, path, line_num);
        } else {
            println!("  {}:{} {}", path.cyan(), line_num, name);
        }
    }

    Ok(())
}

/// Find Flow declarations
pub fn cmd_flows(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    // The search pattern must be exactly the extraction regex: lines like
    // `.asStateFlow()` would otherwise consume the limit without producing a result.
    let flow_pattern = r"\b(MutableStateFlow|MutableSharedFlow|StateFlow|SharedFlow|Flow)\s*<";
    let flow_regex = Regex::new(flow_pattern)?;

    let mut items: Vec<(String, String, usize, String)> = vec![];

    search_files_filtered(
        root,
        flow_pattern,
        &["kt", "kts"],
        limit,
        |_, line| query.is_none_or(|q| line.to_lowercase().contains(&q.to_lowercase())),
        |path, line_num, line| {
            if let Some(caps) = flow_regex.captures(line) {
                let flow_type = caps.get(1).unwrap().as_str().to_string();

                let rel_path = relative_path(root, path);
                let content: String = line.trim().chars().take(70).collect();
                items.push((flow_type, rel_path, line_num, content));
            }
        },
    )?;

    println!("{}", format!("Flow declarations ({}):", items.len()).bold());

    for (flow_type, path, line_num, content) in &items {
        println!("  [{}] {}:{}", flow_type.cyan(), path, line_num);
        println!("    {}", content);
    }

    Ok(())
}

/// Find @Preview functions
pub fn cmd_previews(root: &Path, query: Option<&str>, limit: usize) -> Result<()> {
    print_annotated_functions(root, "Preview", query, limit)
}

/// Structural code search via ast-grep (requires `sg` or `ast-grep` installed)
pub fn cmd_ast_grep(root: &Path, pattern: &str, lang: Option<&str>, json: bool) -> Result<()> {
    // Find ast-grep binary
    let binary = find_ast_grep_binary()
        .ok_or_else(|| anyhow::anyhow!(
            "ast-grep not found. Install it:\n  brew install ast-grep    # macOS\n  npm i -g @ast-grep/cli   # npm\n  cargo install ast-grep    # cargo"
        ))?;

    let mut cmd = std::process::Command::new(&binary);
    cmd.arg("run")
        .arg("--pattern")
        .arg(pattern)
        .current_dir(root);

    if let Some(lang) = lang {
        cmd.arg("--lang").arg(lang);
    }

    if json {
        cmd.arg("--json=compact");
    }

    let status = cmd.status()?;

    if !status.success() && status.code() != Some(1) {
        // Exit code 1 = no matches (normal for grep), anything else is an error
        anyhow::bail!("ast-grep exited with code {:?}", status.code());
    }

    Ok(())
}

fn find_ast_grep_binary() -> Option<String> {
    for name in &["sg", "ast-grep"] {
        if std::process::Command::new(name)
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
        {
            return Some(name.to_string());
        }
    }
    None
}

#[cfg(test)]
mod java_syntax_budget_tests {
    #[test]
    fn java_source_over_the_syntax_budget_is_an_error_not_unbounded_input() {
        let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(".artifacts/tests");
        std::fs::create_dir_all(&base).unwrap();
        let directory = tempfile::tempdir_in(base).unwrap();
        let path = directory.path().join("Probe.java");
        let source = "class Probe {}";
        std::fs::write(&path, source).unwrap();
        assert_eq!(
            super::read_java_syntax_source(&path, source.len() as u64).unwrap(),
            source
        );
        assert!(super::read_java_syntax_source(&path, source.len() as u64 - 1).is_err());
    }

    #[test]
    fn java_stream_read_is_bounded_even_if_the_source_grows() {
        let mut stream = std::io::Cursor::new(b"class Probe {} trailing data");
        let error = super::read_java_syntax_stream(&mut stream, 8).unwrap_err();
        assert!(error.to_string().contains("8 byte budget"));
        assert_eq!(stream.position(), 9);
    }

    #[test]
    fn java_stream_preserves_utf8_and_empty_file_semantics() {
        let source = "class Пример {}";
        assert_eq!(
            super::read_java_syntax_stream(source.as_bytes(), source.len() as u64).unwrap(),
            source
        );
        assert!(super::read_java_syntax_stream(&b"\xff"[..], 1).is_err());
        assert_eq!(super::read_java_syntax_stream(&b""[..], 0).unwrap(), "");
        assert!(super::read_java_syntax_stream(&b"x"[..], 0).is_err());
    }
}

#[cfg(test)]
mod tests {
    fn inject_lines(src: &str, ty: &str) -> Vec<usize> {
        let re = Regex::new(&format!(r"\b{}\b", regex::escape(ty))).unwrap();
        injection_lines(src, &re)
            .into_iter()
            .map(|l| l + 1)
            .collect()
    }

    #[test]
    fn inject_finds_kotlin_constructor_parameters() {
        let src = "class A @Inject constructor(\n    private val repo: Lazy<Repo>,\n    @Named(\"x\") private val other: Other,\n) {\n    val unrelated: Repo? = null\n}\n";
        assert_eq!(inject_lines(src, "Repo"), vec![2]);
        assert_eq!(inject_lines(src, "Other"), vec![3]);
    }

    #[test]
    fn inject_finds_fields_with_annotation_on_previous_line() {
        let src = "class A {\n    @Inject\n    lateinit var repo: Repo\n    @field:Inject lateinit var other: Other\n    fun f(r: Repo) {}\n}\n";
        assert_eq!(inject_lines(src, "Repo"), vec![3]);
        assert_eq!(inject_lines(src, "Other"), vec![4]);
    }

    #[test]
    fn inject_finds_java_constructor_and_field() {
        let src = "class A {\n  @Inject @Named(\"a\") Repo repo;\n  @Inject\n  public A(Other o,\n           Repo r) {\n  }\n}\n";
        assert_eq!(inject_lines(src, "Repo"), vec![2, 5]);
        assert_eq!(inject_lines(src, "Other"), vec![4]);
    }

    #[test]
    fn pattern_with_line_filter_requires_both_parts() {
        let re = Regex::new(&pattern_with_line_filter(r"@Suppress", Some("unchecked"))).unwrap();
        assert!(re.is_match(r#"@Suppress("UNCHECKED_CAST")"#));
        assert!(!re.is_match(r#"@Suppress("DEPRECATION")"#));
        assert_eq!(pattern_with_line_filter("x", None), "x");
    }

    #[test]
    fn suspend_regex_skips_extension_receiver() {
        let re = Regex::new(SUSPEND_FUN_NAME_PATTERN).unwrap();
        let name = |l: &str| re.captures(l).map(|c| c[1].to_string());
        assert_eq!(
            name("override suspend fun ScreenStackNavigator.handle(x: X)").as_deref(),
            Some("handle")
        );
        assert_eq!(
            name("suspend fun <T> Flow<T>.firstOrNull(): T?").as_deref(),
            Some("firstOrNull")
        );
        assert_eq!(
            name("suspend fun load(id: String)").as_deref(),
            Some("load")
        );
    }

    use super::*;

    // --- build_caller_pattern tests ---

    fn matches(pattern: &str, text: &str) -> bool {
        Regex::new(pattern).unwrap().is_match(text)
    }

    #[test]
    fn test_caller_pattern_dot_call_with_parens() {
        let pat = build_caller_pattern("perform_async");
        assert!(matches(&pat, "  MyWorker.perform_async(id)"));
        assert!(matches(&pat, "  worker.perform_async(1, 2)"));
    }

    #[test]
    fn test_caller_pattern_dot_call_without_parens() {
        let pat = build_caller_pattern("process");
        // Ruby: obj.method without parens
        assert!(matches(&pat, "  new(*args).process"));
        assert!(matches(&pat, "  service.process"));
    }

    #[test]
    fn test_caller_pattern_bare_call_with_parens() {
        let pat = build_caller_pattern("normalize_phone");
        assert!(matches(&pat, "  normalized = normalize_phone(number)"));
        assert!(matches(&pat, "    if result = normalize_phone(input)"));
    }

    #[test]
    fn test_caller_pattern_symbol_ref() {
        let pat = build_caller_pattern("set_timestamps");
        // Ruby callbacks: before_action :method_name
        assert!(matches(&pat, "  before_save :set_timestamps"));
        assert!(matches(
            &pat,
            "  after_create :set_timestamps, if: :active?"
        ));
        for line in [
            "  validate :set_timestamps",
            "  delegate :set_timestamps, to: :record",
            "  records.each(&:set_timestamps)",
            "  send(:set_timestamps)",
            "  alias_method :touch, :set_timestamps",
            "  only: %i[a] + [:set_timestamps]",
            ":set_timestamps",
        ] {
            assert!(matches(&pat, line), "{line}");
        }
    }

    #[test]
    fn symbol_ref_ends_with_the_name_and_is_no_path() {
        let pat = build_caller_pattern("update");
        for line in [
            "    authorize(@job, :update?)",
            "  permissions :update! do",
            "  alias_method :update=, :write",
            "  :updated_at",
            "use super::update;",
            "  Billing::update",
            "  mem::update",
        ] {
            assert!(!matches(&pat, line), "{line}");
        }
        assert!(matches(&pat, "  before_action :update, only: :show"));
        let pat = build_caller_pattern("valid?");
        assert!(matches(&pat, "  validate :valid?, on: :create"));
    }

    #[test]
    fn test_caller_pattern_method_chain() {
        let pat = build_caller_pattern("recalc_counters");
        // Ruby: bare method.chain
        assert!(matches(&pat, "    recalc_counters.where(job_id: job.id)"));
    }

    #[test]
    fn test_caller_pattern_no_false_positives_in_substring() {
        let pat = build_caller_pattern("process");
        // Should NOT match "preprocess" as a bare call with parens
        assert!(!matches(&pat, "  preprocess(data)"));
    }

    #[test]
    fn test_caller_pattern_ruby_bang_method() {
        let pat = build_caller_pattern("authenticate_user!");
        // Ruby callbacks with bang methods
        assert!(matches(&pat, "  before_action :authenticate_user!"));
        assert!(matches(
            &pat,
            "  skip_before_action :authenticate_user!, only: [:index]"
        ));
        // Direct calls
        assert!(matches(&pat, "  authenticate_user!(request)"));
        assert!(matches(&pat, "  current_user.authenticate_user!"));
    }

    #[test]
    fn test_caller_pattern_ruby_question_method() {
        let pat = build_caller_pattern("valid?");
        assert!(matches(&pat, "  record.valid?"));
        assert!(matches(&pat, "  valid?(params)"));
    }

    #[test]
    fn bare_predicate_and_bang_calls_are_calls() {
        let pat = build_caller_pattern("next_page?");
        assert!(matches(&pat, "    break unless next_page?"));
        assert!(matches(&pat, "    if next_page? && more"));
        assert!(matches(&pat, "next_page?"));
        assert!(matches(&pat, "  \"page #{next_page?}\""));
        assert!(matches(&pat, "  (response.next_page?)"));
        assert!(!matches(&pat, "  has_next_page?"));
        assert!(!matches(&pat, "  describe \"#next_page?\" do"));
        assert!(build_def_skip_pattern("next_page?").is_match("  def next_page?"));

        let pat = build_caller_pattern("save!");
        assert!(matches(&pat, "    save!"));
        assert!(matches(&pat, "    save! if dirty"));
        assert!(!matches(&pat, "    save!= other"));
        assert!(!matches(&pat, "    save!~ /x/"));

        // Any other word, bare, may be a local variable.
        assert!(!matches(&build_caller_pattern("perform"), "    perform"));
        assert!(!matches(&build_caller_pattern("a::b?"), "    a::b?"));
    }

    #[test]
    fn test_caller_pattern_await_bare_call() {
        let pat = build_caller_pattern("fetchCategories");
        assert!(matches(&pat, "  await fetchCategories()"));
        assert!(matches(&pat, "  const result = await fetchCategories()"));
    }

    #[test]
    fn test_caller_pattern_await_method_call() {
        let pat = build_caller_pattern("loadEventRecords");
        assert!(matches(&pat, "  await store.loadEventRecords()"));
        assert!(matches(&pat, "  await this.loadEventRecords()"));
    }

    #[test]
    fn test_caller_pattern_return_bare_call() {
        let pat = build_caller_pattern("pluralize");
        assert!(matches(&pat, "  return pluralize(count, forms)"));
    }

    #[test]
    fn test_caller_pattern_return_method_call() {
        let pat = build_caller_pattern("serialize");
        assert!(matches(&pat, "  return serializer.serialize()"));
    }

    #[test]
    fn test_caller_pattern_await_chained() {
        let pat = build_caller_pattern("addAction");
        assert!(matches(&pat, "  await syncQueue.addAction(action)"));
    }

    #[test]
    fn test_caller_pattern_same_file_calls() {
        // Common Pinia store / composable pattern: functions calling each other
        let pat = build_caller_pattern("loadFromDB");
        // Bare call (no await)
        assert!(matches(&pat, "    loadFromDB()"));
        // Await call (most common in stores)
        assert!(matches(&pat, "    await loadFromDB()"));
        // Definition line should NOT match
        assert!(!matches(&pat, "  const loadFromDB = async () => {"));
        // Return call
        assert!(matches(&pat, "    return loadFromDB()"));
    }

    #[test]
    fn every_caller_idiom_contains_the_function_name() {
        // The batched call-tree scan skips a name's pattern on lines that do
        // not contain the name; an idiom without `{fn}` would lose matches.
        for idiom in CALLER_IDIOMS {
            assert!(idiom.contains("{fn}"), "{idiom}");
        }
        assert!(BARE_PREDICATE_CALL_IDIOM.contains("{fn}"));
    }

    #[test]
    fn any_caller_pattern_matches_each_names_calls() {
        let names = [
            "perform".to_string(),
            "save!".to_string(),
            "valid?".to_string(),
            "let(:fields)".to_string(),
        ];
        let any = Regex::new(&build_any_caller_pattern(&names)).unwrap();
        let lines = [
            "  Worker.new.perform",
            "  before_action :perform",
            "  record.save!",
            "  skip_callback :save!, if: :x",
            "  return valid?(record)",
            "  await store.valid?(x)",
            "  let(:fields).tap { }",
            "  save!",
            "  return if valid? && ready",
        ];
        for line in lines {
            assert!(
                names
                    .iter()
                    .any(|name| matches(&build_caller_pattern(name), line)),
                "{line}"
            );
            assert!(any.is_match(line), "{line}");
        }
        assert!(!any.is_match("  preperform(data)"));
    }

    fn sites(entries: &[(&str, &str, usize)]) -> CallerSites {
        entries
            .iter()
            .map(|(caller, file, line)| {
                CallerSite::lexical(caller.to_string(), file.to_string(), *line)
            })
            .collect()
    }

    fn walk(
        function_name: &str,
        max_depth: usize,
        callers: &HashMap<CallerTarget, CallerSites>,
    ) -> (Vec<String>, Vec<String>) {
        let mut edges = vec![];
        let missing = walk_call_tree(
            function_name,
            max_depth,
            callers,
            &mut |depth, site, node| {
                let (caller, file, line) = (&site.name, &site.path, site.line);
                edges.push(match node {
                    TreeNode::Shown => format!("{depth} {caller} {file}:{line}"),
                    TreeNode::ExpandedAbove => format!("{depth} {caller} {file}:{line} above"),
                    TreeNode::Recursive => format!("{depth} {caller} recursive"),
                });
            },
        );
        (
            edges,
            missing
                .into_iter()
                .map(|target| match target {
                    CallerTarget::Name(name) => name,
                    CallerTarget::JavaSymbol(_) => panic!("lexical fixture returned a graph id"),
                })
                .collect(),
        )
    }

    fn tree(entries: Vec<(&str, CallerSites)>) -> HashMap<CallerTarget, CallerSites> {
        entries
            .into_iter()
            .map(|(name, sites)| (CallerTarget::Name(name.to_string()), sites))
            .collect()
    }

    #[test]
    fn walk_call_tree_reports_what_it_lacks_without_descending() {
        let mut callers = HashMap::new();
        let (edges, missing) = walk("leaf", 3, &callers);
        assert!(edges.is_empty());
        assert_eq!(missing, ["leaf"]);

        callers.insert(
            CallerTarget::Name("leaf".to_string()),
            sites(&[("alpha", "a.rb", 2), ("beta", "b.rb", 6)]),
        );
        let (edges, missing) = walk("leaf", 3, &callers);
        assert_eq!(edges, ["1 alpha a.rb:2", "1 beta b.rb:6"]);
        assert_eq!(missing, ["alpha", "beta"]);
    }

    #[test]
    fn walk_call_tree_marks_only_a_definition_on_its_own_path_as_recursive() {
        let callers = tree(vec![
            ("leaf", sites(&[("alpha", "a.rb", 2)])),
            ("alpha", sites(&[("beta", "b.rb", 6)])),
            ("beta", sites(&[("alpha", "a.rb", 2), ("alpha", "c.rb", 4)])),
        ]);
        let (edges, missing) = walk("leaf", 4, &callers);
        assert_eq!(
            edges,
            [
                "1 alpha a.rb:2",
                "2 beta b.rb:6",
                "3 alpha recursive",
                "3 alpha c.rb:4 above",
            ]
        );
        assert!(missing.is_empty());
    }

    #[test]
    fn walk_call_tree_shows_same_named_callers_of_other_files_with_their_path() {
        let callers = tree(vec![
            (
                "leaf",
                sites(&[
                    ("it \"works\"", "a_spec.rb", 3),
                    ("it \"works\"", "b_spec.rb", 7),
                    ("export", "a.rb", 2),
                    ("export", "b.rb", 5),
                    ("leaf", "c.rb", 9),
                    ("alpha", "d.rb", 1),
                    ("beta", "e.rb", 1),
                ]),
            ),
            ("export", sites(&[("run", "r.rb", 4)])),
            ("run", sites(&[])),
            ("alpha", sites(&[("top", "t.rb", 2)])),
            ("beta", sites(&[("top", "t.rb", 2), ("bottom", "u.rb", 3)])),
            ("top", sites(&[("main", "m.rb", 1)])),
            ("bottom", sites(&[])),
        ]);
        let (edges, missing) = walk("leaf", 3, &callers);
        assert_eq!(
            edges,
            [
                "1 it \"works\" a_spec.rb:3",
                "1 it \"works\" b_spec.rb:7",
                "1 export a.rb:2",
                "2 run r.rb:4",
                "1 export b.rb:5 above",
                "1 leaf c.rb:9 above",
                "1 alpha d.rb:1",
                "2 top t.rb:2",
                "3 main m.rb:1",
                "1 beta e.rb:1",
                "2 top t.rb:2 above",
                "2 bottom u.rb:3",
            ]
        );
        assert!(missing.is_empty());
    }

    #[test]
    fn walk_call_tree_expands_a_name_first_met_at_the_depth_limit_further_up() {
        let callers = tree(vec![
            (
                "leaf",
                sites(&[("alpha", "a.rb", 2), ("helper", "h.rb", 9)]),
            ),
            ("alpha", sites(&[("helper", "h.rb", 1)])),
            ("helper", sites(&[("main", "m.rb", 1)])),
        ]);
        let (edges, missing) = walk("leaf", 2, &callers);
        assert_eq!(
            edges,
            [
                "1 alpha a.rb:2",
                "2 helper h.rb:1",
                "1 helper h.rb:9",
                "2 main m.rb:1",
            ]
        );
        assert!(missing.is_empty());
    }

    #[test]
    fn walk_call_tree_needs_no_callers_below_the_depth_limit() {
        let callers: HashMap<CallerTarget, CallerSites> = [(
            CallerTarget::Name("leaf".to_string()),
            sites(&[("alpha", "a.rb", 2)]),
        )]
        .into_iter()
        .collect();
        let (edges, missing) = walk("leaf", 1, &callers);
        assert_eq!(edges, ["1 alpha a.rb:2"]);
        assert!(missing.is_empty());
        assert_eq!(walk("leaf", 0, &callers), (vec![], vec![]));
    }

    #[test]
    fn walk_call_tree_shows_uncallable_callers_without_expanding_them() {
        let callers = tree(vec![(
            "leaf",
            sites(&[
                ("it \"works\"", "leaf_spec.rb", 2),
                ("let(:user)", "leaf_spec.rb", 5),
                ("save!", "record.rb", 3),
            ]),
        )]);
        let (edges, missing) = walk("leaf", 3, &callers);
        assert_eq!(
            edges,
            [
                "1 it \"works\" leaf_spec.rb:2",
                "1 let(:user) leaf_spec.rb:5",
                "1 save! record.rb:3",
            ]
        );
        assert_eq!(missing, ["save!"]);
    }

    #[test]
    fn callable_names_are_identifiers() {
        for name in [
            "perform",
            "save!",
            "valid?",
            "name=",
            "Applicant::MergeService",
            "::TopLevel",
            "self.call",
            "React.memo",
            "$onChange",
            "#secret",
            "button-variant",
            "ПолучитьДанные",
            "_private",
        ] {
            assert!(is_callable_name(name), "{name}");
        }
    }

    #[test]
    fn dsl_block_names_are_not_callable() {
        for name in [
            "it \"does nothing\"",
            "let(:fields)",
            "let!(:company)",
            "subject(:perform)",
            "describe \"Applicant::MergeService\"",
            "attributes :ext_id",
            "include first_name: [:presence]",
            "scope :active",
            ":result",
            "default(firebase)",
            "`backticked name`",
            "[]",
            "==",
            "a:b",
            "save!!",
            "valid?x",
            "",
        ] {
            assert!(!is_callable_name(name), "{name}");
        }
    }

    // --- build_def_skip_pattern tests ---

    #[test]
    fn def_skip_candidate_covers_every_full_match() {
        let lines = [
            "  def call",
            "\tdef self.call(params)",
            "def call!",
            "  fun call(x: Int)",
            "func call<T>(x: T)",
            "sub call {",
            "  public static void call(String s) {",
            "  private List<Map<String, Integer>> call(int x)",
            "  int[] call (int x)",
            "  Foo.Bar call(x)",
            "  return call(x)",
            "  await call(x)",
            "  Строка call(x)",
            "  x = call(y)",
            "  obj.call(x)",
            "  :call",
        ];
        for name in ["call", "call!", "valid?", "call_me"] {
            let pat = build_def_skip_pattern(name);
            for line in lines {
                let line = line.replace("call", name);
                if pat.full.is_match(&line) {
                    assert!(pat.candidate.is_match(&line), "{name}: {line}");
                }
            }
        }
    }

    #[test]
    fn test_def_skip_ruby_bang_method() {
        let pat = build_def_skip_pattern("authenticate_user!");
        assert!(pat.is_match("  def authenticate_user!"));
        assert!(pat.is_match("  def self.authenticate_user!"));
    }

    #[test]
    fn test_def_skip_ruby_instance_method() {
        let pat = build_def_skip_pattern("process");
        assert!(pat.is_match("  def process"));
        assert!(pat.is_match("  def process(args)"));
    }

    #[test]
    fn test_def_skip_ruby_self_method() {
        let pat = build_def_skip_pattern("call");
        assert!(pat.is_match("  def self.call(params)"));
        assert!(pat.is_match("  def self.call"));
    }

    #[test]
    fn test_def_skip_does_not_match_calls() {
        let pat = build_def_skip_pattern("process");
        assert!(!pat.is_match("  service.process"));
        assert!(!pat.is_match("  result = process(data)"));
    }

    #[test]
    fn test_def_skip_kotlin_fun() {
        let pat = build_def_skip_pattern("calculate");
        assert!(pat.is_match("  fun calculate(x: Int)"));
    }

    #[test]
    fn def_skip_keeps_calls_behind_a_keyword() {
        let calls = [
            ("foo", "    return foo(x)"),
            ("foo", "  const y = await foo(x)"),
            ("Foo", "    throw new Foo(message)"),
            ("foo", "  } else foo(x)"),
            ("foo", "    yield foo(x)"),
            ("foo", "    puts foo(x)"),
            ("Foo", "    raise Foo(message)"),
            ("foo", "    if foo(x)"),
            ("foo", "    elsif foo(x)"),
            ("foo", "  for item in foo(items):"),
            ("foo", "  for (const item of foo(items)) {"),
            ("foo", "export default foo(App)"),
            ("foo", "  go foo(ch)"),
            ("foo", "  defer foo(conn)"),
            ("foo", "  echo foo($x);"),
            ("foo", "  with foo(path) as handle:"),
            ("foo", "    assert foo(x)"),
            ("foo", "  match foo(x) {"),
            ("foo", "  return await foo(x)"),
        ];
        for (name, line) in calls {
            assert!(matches(&build_caller_pattern(name), line), "{line}");
            assert!(!build_def_skip_pattern(name).is_match(line), "{line}");
        }
    }

    #[test]
    fn def_skip_still_recognises_typed_definitions() {
        let definitions = [
            "  public void foo(int x) {",
            "  String foo(String x) {",
            "  public static List<String> foo(Map<String, Integer> x) {",
            "  int[] foo() {",
            "  private override fun foo() {",
            "export function foo(x) {",
            "  async function foo(x) {",
            "  public foo(x: number): void {",
            "  static foo() {",
            "  async foo() {",
            "  private def foo(x)",
            "  defp foo(x) do",
            "pub fn foo(x: u32) -> u32 {",
            "local function foo(x)",
        ];
        let pat = build_def_skip_pattern("foo");
        for line in definitions {
            assert!(pat.is_match(line), "{line}");
        }
    }

    // --- find_containing_function tests ---

    #[test]
    fn test_find_containing_ruby_method() {
        let code = vec![
            "class MyService",
            "  def process",
            "    result = other_service.call(data)",
            "    transform(result)",
            "  end",
            "end",
        ];
        let func_def_re = Regex::new(
            concat!(
                r"(?:fun|func|sub)\s+(\w+)\s*[<(\[]",
                r"|\bdef\s+(?:self\.)?(\w[!\w?]*)",
                r"|(?:(?:public|private|protected|static|final|abstract|synchronized|override)\s+)*",
                r"(?:void|int|long|boolean|char|byte|short|float|double|[\w.]+(?:<[^{;]*>)?(?:\[\])*)\s+(\w+)\s*\(",
            )
        ).unwrap();

        // Line 3 (0-indexed) = "    result = other_service.call(data)"
        let result = find_containing_function(&code, 3, &func_def_re);
        assert_eq!(result, Some(("process".to_string(), 2)));
    }

    #[test]
    fn test_find_containing_ruby_self_method() {
        let code = vec![
            "class MyService",
            "  def self.call(params)",
            "    new(params).process",
            "  end",
            "end",
        ];
        let func_def_re = Regex::new(
            concat!(
                r"(?:fun|func|sub)\s+(\w+)\s*[<(\[]",
                r"|\bdef\s+(?:self\.)?(\w[!\w?]*)",
                r"|(?:(?:public|private|protected|static|final|abstract|synchronized|override)\s+)*",
                r"(?:void|int|long|boolean|char|byte|short|float|double|[\w.]+(?:<[^{;]*>)?(?:\[\])*)\s+(\w+)\s*\(",
            )
        ).unwrap();

        let result = find_containing_function(&code, 3, &func_def_re);
        assert_eq!(result, Some(("call".to_string(), 2)));
    }

    #[test]
    fn test_find_containing_ruby_bang_method() {
        let code = vec![
            "class Updater",
            "  def update!",
            "    record.save!",
            "  end",
            "end",
        ];
        let func_def_re = Regex::new(
            concat!(
                r"(?:fun|func|sub)\s+(\w+)\s*[<(\[]",
                r"|\bdef\s+(?:self\.)?(\w[!\w?]*)",
                r"|(?:(?:public|private|protected|static|final|abstract|synchronized|override)\s+)*",
                r"(?:void|int|long|boolean|char|byte|short|float|double|[\w.]+(?:<[^{;]*>)?(?:\[\])*)\s+(\w+)\s*\(",
            )
        ).unwrap();

        let result = find_containing_function(&code, 3, &func_def_re);
        assert_eq!(result, Some(("update!".to_string(), 2)));
    }
}
