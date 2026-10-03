use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Context, Result};
use regex::Regex;
use tree_sitter::{Node, Parser};

pub(super) struct AnnotatedFunction {
    pub name: String,
    pub path: String,
    pub line: usize,
    pub signature: String,
}

pub(super) fn find(
    root: &Path,
    annotations: &[&str],
    extensions: &[&str],
    query: Option<&str>,
    providers: bool,
    limit: usize,
) -> Result<Vec<AnnotatedFunction>> {
    if limit == 0 {
        return Ok(Vec::new());
    }
    let pattern = format!(r"^@(?:[\w$]+\.)*({})(?:[^\w$]|$)", annotations.join("|"));
    let annotation = Regex::new(&pattern)?;
    let mut files = BTreeSet::new();
    // No candidate limit: a matching declaration can follow arbitrarily many
    // unrelated annotations, and limiting the anchors loses valid results.
    super::search_files_in(
        root,
        &[root.to_path_buf()],
        &pattern[1..],
        extensions,
        |path, _, _| {
            files.insert(path.to_path_buf());
        },
    )?;
    let mut results = BTreeMap::new();
    for path in files {
        let source = std::fs::read_to_string(&path)?;
        let java = path.extension().is_some_and(|ext| ext == "java");
        let language = if java {
            tree_sitter_java::LANGUAGE.into()
        } else {
            tree_sitter_kotlin_ng::LANGUAGE.into()
        };
        let mut parser = Parser::new();
        parser.set_language(&language)?;
        let tree = parser
            .parse(&source, None)
            .context("annotation search parse failed")?;
        let mut cursor = tree.walk();
        'nodes: loop {
            let node = cursor.node();
            if matches!(node.kind(), "method_declaration" | "function_declaration") {
                if let Some(name) = node.child_by_field_name("name") {
                    let name = name.utf8_text(source.as_bytes())?.trim_matches('`');
                    let matches_query = if providers {
                        return_type(node, &source, java)
                            .is_some_and(|ty| ty.ends_with(query.unwrap_or("")))
                    } else {
                        query.is_none_or(|q| name.to_lowercase().contains(&q.to_lowercase()))
                    };
                    if matches_query {
                        let mut children = node.walk();
                        for modifiers in node
                            .named_children(&mut children)
                            .filter(|n| n.kind() == "modifiers")
                        {
                            let mut modifier_cursor = modifiers.walk();
                            for item in modifiers.named_children(&mut modifier_cursor) {
                                if matches!(item.kind(), "annotation" | "marker_annotation")
                                    && annotation.is_match(item.utf8_text(source.as_bytes())?)
                                {
                                    let line = if providers {
                                        item.start_position().row + 1
                                    } else {
                                        name_line(node) + 1
                                    };
                                    let path = super::relative_path(root, &path);
                                    let signature = source
                                        .lines()
                                        .nth(name_line(node))
                                        .unwrap_or("")
                                        .trim()
                                        .to_string();
                                    results.insert(
                                        (path.clone(), line, node.start_byte()),
                                        AnnotatedFunction {
                                            name: name.to_string(),
                                            path,
                                            line,
                                            signature,
                                        },
                                    );
                                    if results.len() > limit {
                                        results.pop_last();
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if cursor.goto_first_child() {
                continue;
            }
            while !cursor.goto_next_sibling() {
                if !cursor.goto_parent() {
                    break 'nodes;
                }
            }
        }
    }
    Ok(results.into_values().collect())
}

fn name_line(node: Node<'_>) -> usize {
    node.child_by_field_name("name")
        .map_or(node.start_position().row, |name| name.start_position().row)
}

fn return_type<'a>(node: Node<'_>, source: &'a str, java: bool) -> Option<&'a str> {
    let ty = if java {
        node.child_by_field_name("type")?
    } else {
        let mut cursor = node.walk();
        let mut after_parameters = false;
        let found = node.named_children(&mut cursor).find(|child| {
            if child.kind() == "function_value_parameters" {
                after_parameters = true;
                return false;
            }
            after_parameters
                && matches!(
                    child.kind(),
                    "user_type" | "nullable_type" | "type_identifier"
                )
        });
        found?
    };
    // Query the outer declared return type, never a parameter or generic argument.
    let text = ty.utf8_text(source.as_bytes()).ok()?;
    Some(text.split(['<', '?', '[']).next()?.trim())
}
