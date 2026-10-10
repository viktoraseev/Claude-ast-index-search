//! Tree-sitter based Java parser

use anyhow::Result;
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::LazyLock;
use tree_sitter::{Language, Node, Query, QueryCursor, StreamingIterator};

use super::{node_line, node_text, parse_tree, signature_line, text_end_line, LanguageParser};
use crate::db::SymbolKind;
use crate::parsers::{FileType, ParsedRef, ParsedSymbol};

static JAVA_LANGUAGE: LazyLock<Language> = LazyLock::new(|| tree_sitter_java::LANGUAGE.into());

static JAVA_QUERY: LazyLock<Query> = LazyLock::new(|| {
    Query::new(&JAVA_LANGUAGE, include_str!("queries/java.scm"))
        .expect("Failed to compile Java tree-sitter query")
});

pub static JAVA_PARSER: JavaParser = JavaParser;

pub struct JavaParser;

/// Read Java type substance from syntax, independently of body line span.
pub(crate) fn type_body_emptiness(content: &str) -> Result<HashMap<(String, i64, i64), bool>> {
    fn has_members(body: tree_sitter::Node<'_>) -> bool {
        let mut cursor = body.walk();
        let members = body
            .named_children(&mut cursor)
            .any(|child| match child.kind() {
                "line_comment" | "block_comment" | "empty_declaration" => false,
                "enum_body_declarations" => has_members(child),
                _ => true,
            });
        members
    }

    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let mut bodies = HashMap::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        if !matches!(
            node.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        ) || node.has_error()
        {
            return super::WalkControl::Continue;
        }
        if let (Some(name), Some(body)) = (
            node.child_by_field_name("name"),
            node.child_by_field_name("body"),
        ) {
            // Record components create state/accessors even with an empty body.
            let components = node.kind() == "record_declaration"
                && node
                    .child_by_field_name("parameters")
                    .is_some_and(|parameters| has_members(parameters));
            bodies.insert(
                (
                    node_text(content, &name).to_string(),
                    node_line(&name) as i64,
                    text_end_line(content, &node) as i64,
                ),
                !components && !has_members(body),
            );
        }
        super::WalkControl::Continue
    });
    Ok(bodies)
}

/// Qualified import declarations, including static and wildcard imports.
pub(crate) fn import_names(content: &str) -> Result<Vec<String>> {
    Ok(import_declarations(content)?
        .into_iter()
        .map(|(name, _)| name)
        .collect())
}

/// Java import identities and their static modifier, in source order.
pub(crate) fn import_declarations(content: &str) -> Result<Vec<(String, bool)>> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let mut imports = Vec::new();
    let mut cursor = tree.root_node().walk();
    for declaration in tree.root_node().named_children(&mut cursor) {
        if declaration.kind() != "import_declaration" {
            continue;
        }
        let mut parts = Vec::new();
        super::walk_tree_preorder(&declaration, |node| {
            if node.kind() == "identifier" || node.kind() == "asterisk" {
                parts.push(node_text(content, &node));
            }
            super::WalkControl::Continue
        });
        if !parts.is_empty() {
            let mut cursor = declaration.walk();
            let is_static = declaration
                .children(&mut cursor)
                .any(|node| node.kind() == "static");
            imports.push((parts.join("."), is_static));
        }
    }
    Ok(imports)
}

pub(crate) struct ResourceReference {
    pub namespace: Option<String>,
    pub resource_type: String,
    pub name: String,
    pub line: usize,
    pub offset: usize,
    pub qualifier: String,
}

/// Byte-scoped lexical names shared by Java dependencies and resource expressions.
#[derive(Default)]
struct LexicalShadows {
    types: HashMap<String, Vec<std::ops::Range<usize>>>,
    values: HashMap<String, Vec<std::ops::Range<usize>>>,
    methods: HashMap<String, Vec<std::ops::Range<usize>>>,
}

impl LexicalShadows {
    fn visible(
        names: &HashMap<String, Vec<std::ops::Range<usize>>>,
        name: &str,
        node: Node<'_>,
    ) -> bool {
        names.get(name).is_some_and(|ranges| {
            ranges
                .iter()
                .any(|range| range.contains(&node.start_byte()))
        })
    }

    fn expression(&self, name: &str, node: Node<'_>) -> bool {
        let first = name.split('.').next().unwrap_or(name);
        Self::visible(&self.types, first, node) || Self::visible(&self.values, first, node)
    }
}

fn lexical_shadows(root: Node<'_>, content: &str) -> LexicalShadows {
    // A declaration's spelling is not a file-wide shadow. Keep byte ranges
    // per name so adjacent blocks/methods, including sites on one line, retain
    // their own Java type and expression namespaces.
    fn ancestor<'a>(node: tree_sitter::Node<'a>, kinds: &[&str]) -> Option<tree_sitter::Node<'a>> {
        let mut parent = node.parent();
        while let Some(scope) = parent {
            if kinds.contains(&scope.kind()) {
                return Some(scope);
            }
            parent = scope.parent();
        }
        None
    }
    let mut type_shadows: HashMap<String, Vec<std::ops::Range<usize>>> = HashMap::new();
    let mut value_shadows: HashMap<String, Vec<std::ops::Range<usize>>> = HashMap::new();
    let mut method_shadows: HashMap<String, Vec<std::ops::Range<usize>>> = HashMap::new();
    super::walk_tree_preorder(&root, |node| {
        if node.kind() == "enum_constant" {
            if let (Some(name), Some(body)) = (
                node.child_by_field_name("name"),
                ancestor(node, &["enum_body"]),
            ) {
                value_shadows
                    .entry(node_text(content, &name).to_owned())
                    .or_default()
                    .push(body.byte_range());
            }
        }
        if node.kind() == "method_declaration" {
            if let (Some(name), Some(body)) = (
                node.child_by_field_name("name"),
                ancestor(
                    node,
                    &[
                        "class_body",
                        "interface_body",
                        "enum_body",
                        "annotation_type_body",
                    ],
                ),
            ) {
                method_shadows
                    .entry(node_text(content, &name).to_owned())
                    .or_default()
                    .push(body.byte_range());
            }
        }
        if node.kind() == "instanceof_expression" {
            if let Some(name) = node.child_by_field_name("name") {
                for (scope, position) in pattern_flow_scopes(node, content) {
                    value_shadows
                        .entry(node_text(content, &name).to_owned())
                        .or_default()
                        .push(position.max(scope.start_byte())..scope.end_byte());
                }
            }
        }
        if node.kind() == "resource" {
            if let (Some(name), Some(specification)) =
                (node.child_by_field_name("name"), node.parent())
            {
                // Resources are visible in their own and subsequent
                // initializers and in the try body, never catch/finally.
                for scope in [
                    Some(specification),
                    specification
                        .parent()
                        .and_then(|statement| statement.child_by_field_name("body")),
                ]
                .into_iter()
                .flatten()
                {
                    value_shadows
                        .entry(node_text(content, &name).to_owned())
                        .or_default()
                        .push(name.start_byte().max(scope.start_byte())..scope.end_byte());
                }
            }
        }
        let type_declaration = matches!(
            node.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        );
        let binding = if type_declaration {
            ancestor(
                node,
                &[
                    "program",
                    "class_body",
                    "interface_body",
                    "enum_body",
                    "block",
                    "switch_block",
                ],
            )
            .map(|scope| {
                (
                    true,
                    node.child_by_field_name("name"),
                    if matches!(scope.kind(), "block" | "switch_block") {
                        node.start_byte()..scope.end_byte()
                    } else {
                        scope.byte_range()
                    },
                )
            })
        } else if node.kind() == "type_parameter" {
            node.parent()
                .and_then(|parameters| parameters.parent())
                .map(|owner| (true, node.named_child(0), owner.byte_range()))
        } else if node.kind() == "variable_declarator" {
            ancestor(
                node,
                &[
                    "class_body",
                    "interface_body",
                    "enum_body",
                    "block",
                    "for_statement",
                ],
            )
            .map(|scope| {
                (
                    false,
                    node.child_by_field_name("name"),
                    if matches!(scope.kind(), "block" | "for_statement") {
                        node.start_byte()..scope.end_byte()
                    } else {
                        scope.byte_range()
                    },
                )
            })
        } else if matches!(
            node.kind(),
            "formal_parameter" | "spread_parameter" | "catch_formal_parameter"
        ) {
            ancestor(
                node,
                &[
                    "method_declaration",
                    "constructor_declaration",
                    "lambda_expression",
                    "catch_clause",
                    "record_declaration",
                ],
            )
            .map(|scope| {
                let name = node.child_by_field_name("name").or_else(|| {
                    let mut cursor = node.walk();
                    let mut children = node.named_children(&mut cursor);
                    children
                        .find(|child| child.kind() == "variable_declarator")
                        .and_then(|child| child.child_by_field_name("name"))
                });
                (false, name, scope.byte_range())
            })
        } else if node.kind() == "identifier"
            && node.parent().is_some_and(|parent| {
                parent.kind() == "inferred_parameters"
                    || (parent.kind() == "lambda_expression"
                        && parent
                            .child_by_field_name("parameters")
                            .is_some_and(|p| p.id() == node.id()))
            })
        {
            ancestor(node, &["lambda_expression"])
                .map(|scope| (false, Some(node), scope.byte_range()))
        } else if node.kind() == "enhanced_for_statement" {
            node.child_by_field_name("body")
                .map(|scope| (false, node.child_by_field_name("name"), scope.byte_range()))
        } else {
            None
        };
        if let Some((is_type, Some(name), range)) = binding {
            let shadows = if is_type {
                &mut type_shadows
            } else {
                &mut value_shadows
            };
            shadows
                .entry(node_text(content, &name).to_owned())
                .or_default()
                .push(range);
        }
        super::WalkControl::Continue
    });
    LexicalShadows {
        types: type_shadows,
        values: value_shadows,
        methods: method_shadows,
    }
}

/// Read Java R expressions and imported resource constants, excluding literals/import sites.
pub(crate) fn resource_references(content: &str) -> Result<Vec<ResourceReference>> {
    fn parts(node: tree_sitter::Node<'_>, content: &str) -> Option<Vec<String>> {
        match node.kind() {
            "identifier" | "asterisk" => Some(vec![node_text(content, &node).to_owned()]),
            "field_access" | "scoped_identifier" => {
                let mut output = Vec::new();
                let mut cursor = node.walk();
                for child in node.named_children(&mut cursor) {
                    if matches!(child.kind(), "line_comment" | "block_comment") {
                        continue;
                    }
                    output.extend(parts(child, content)?);
                }
                Some(output)
            }
            _ => None,
        }
    }
    fn unique(values: &[String]) -> Option<String> {
        values
            .first()
            .filter(|first| values.iter().all(|v| v == *first))
            .cloned()
    }

    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let shadows = lexical_shadows(tree.root_node(), content);
    let mut imported_r = Vec::new();
    let mut explicit_types = std::collections::HashSet::new();
    let mut aliases: HashMap<String, Vec<String>> = HashMap::new();
    let mut type_wildcards = Vec::new();
    let mut constants: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut wildcards = Vec::new();
    let mut cursor = tree.root_node().walk();
    for declaration in tree.root_node().named_children(&mut cursor) {
        if declaration.kind() != "import_declaration" {
            continue;
        }
        let mut names = Vec::new();
        super::walk_tree_preorder(&declaration, |node| {
            if matches!(node.kind(), "identifier" | "asterisk") {
                names.push(node_text(content, &node).to_owned());
            }
            super::WalkControl::Continue
        });
        let mut cursor = declaration.walk();
        let is_static = declaration
            .children(&mut cursor)
            .any(|n| n.kind() == "static");
        if !is_static {
            if let Some(name) = names.last().filter(|name| *name != "*") {
                explicit_types.insert(name.clone());
            }
        }
        let Some(r) = names.iter().rposition(|p| p == "R") else {
            continue;
        };
        if r == 0 {
            continue;
        }
        let namespace = names[..r].join(".");
        match (&names[r + 1..], is_static) {
            ([], false) => imported_r.push(namespace),
            ([kind], _) if kind == "*" => type_wildcards.push(namespace),
            ([kind], _) => aliases.entry(kind.clone()).or_default().push(namespace),
            ([kind, name], true) => {
                let target = (namespace, kind.clone());
                if name == "*" {
                    wildcards.push(target);
                } else {
                    constants.entry(name.clone()).or_default().push(target);
                }
            }
            _ => {}
        }
    }
    let mut output = Vec::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        if matches!(
            node.kind(),
            "package_declaration"
                | "import_declaration"
                | "line_comment"
                | "block_comment"
                | "string_literal"
                | "character_literal"
        ) {
            return super::WalkControl::SkipChildren;
        }
        let mut emit = |namespace, kind: &str, name: &str| {
            output.push(ResourceReference {
                namespace,
                resource_type: kind.to_owned(),
                name: name.to_owned(),
                line: node_line(&node),
                offset: node.start_byte(),
                qualifier: parts(node, content)
                    .and_then(|names| names.into_iter().next())
                    .unwrap_or_else(|| node_text(content, &node).to_owned()),
            });
        };
        if node.kind() == "field_access" {
            if let Some(names) = parts(node, content) {
                let n = names.len();
                // Reclassification of an expression name prefers lexical
                // variables/types over imported types and package prefixes.
                if shadows.expression(&names[0], node) {
                    return super::WalkControl::Continue;
                }
                if n >= 3 && names[n - 3] == "R" {
                    let namespace = if n > 3 {
                        Some(names[..n - 3].join("."))
                    } else if imported_r.is_empty() {
                        None
                    } else {
                        let Some(namespace) = unique(&imported_r) else {
                            return super::WalkControl::Continue;
                        };
                        Some(namespace)
                    };
                    emit(namespace, &names[n - 2], &names[n - 1]);
                } else if n == 2 {
                    if let Some(namespace) = aliases.get(&names[0]).and_then(|v| unique(v)) {
                        emit(Some(namespace), &names[0], &names[1]);
                    } else if !aliases.contains_key(&names[0])
                        && !explicit_types.contains(&names[0])
                    {
                        for namespace in &type_wildcards {
                            emit(Some(namespace.clone()), &names[0], &names[1]);
                        }
                    }
                }
            }
        } else if node.kind() == "identifier" {
            if let Some(parent) = node.parent() {
                // Declaration names, selectors, call names and type spellings
                // are not static constant expression sites.
                let named = parent
                    .child_by_field_name("name")
                    .is_some_and(|n| n.id() == node.id());
                if !named
                    && !matches!(
                        parent.kind(),
                        "field_access"
                            | "method_reference"
                            | "scoped_identifier"
                            | "scoped_type_identifier"
                            | "marker_annotation"
                            | "annotation"
                            | "break_statement"
                            | "continue_statement"
                            | "labeled_statement"
                    )
                {
                    let name = node_text(content, &node);
                    // Types and methods use separate namespaces: a type or
                    // method named hit does not hide an imported field hit.
                    if LexicalShadows::visible(&shadows.values, name, node) {
                        return super::WalkControl::Continue;
                    }
                    let targets = constants.get(name).unwrap_or(&wildcards);
                    // Explicit imports take precedence. Distinct wildcard
                    // targets need resource ownership to disambiguate later.
                    for (namespace, kind) in targets {
                        emit(Some(namespace.clone()), kind, name);
                    }
                }
            }
        }
        super::WalkControl::Continue
    });
    Ok(output)
}

#[cfg(test)]
mod resource_binding_tests {
    use super::resource_references;

    #[test]
    fn same_line_local_receivers_only_hide_their_own_expression_sites() {
        let source = r#"import fixture.library.R;
class Probe {
    int use() { int n = R.string.hit; { Object R = null; n += R.string.hit; } return n + R.string.hit; }
}"#;
        let sites = resource_references(source).unwrap();
        let offsets: Vec<_> = source
            .match_indices("R.string.hit")
            .map(|(i, _)| i)
            .collect();
        assert_eq!(sites.len(), 2);
        assert_eq!(
            sites.iter().map(|site| site.offset).collect::<Vec<_>>(),
            vec![offsets[0], offsets[2]]
        );
        assert!(sites
            .iter()
            .all(|site| site.namespace.as_deref() == Some("fixture.library")
                && site.resource_type == "string"
                && site.name == "hit"));
    }

    #[test]
    fn static_field_imports_keep_type_and_method_namespaces_separate() {
        let source = r#"import static fixture.library.R.string.hit;
class Probe {
    static class hit {}
    int hit() { return hit; }
    int use(int hit) { return hit; }
    java.util.function.Supplier<hit> callback = hit::new;
}
enum Choice { hit; Object use() { return hit; } }
class Other { int use() { return hit; } }
"#;
        let sites = resource_references(source).unwrap();
        assert_eq!(
            sites.iter().map(|site| site.line).collect::<Vec<_>>(),
            vec![4, 9]
        );
    }
}

/// Java dependency anchors retain qualified type spelling and import ownership.
#[derive(Default)]
pub(crate) struct DependencySyntax {
    pub package: String,
    pub imports: Vec<(String, bool)>,
    pub types: std::collections::BTreeSet<String>,
    pub declarations: std::collections::HashSet<String>,
    pub static_names: std::collections::BTreeSet<(String, bool)>,
    pub expression_types: std::collections::BTreeSet<String>,
    pub type_uses: std::collections::BTreeSet<(String, bool, Vec<String>)>,
    pub member_uses: std::collections::BTreeSet<(String, bool, Vec<String>)>,
    /// Lexical instances available at at least one occurrence of this use.
    pub instance_contexts:
        std::collections::BTreeMap<(String, bool, Vec<String>), std::collections::BTreeSet<String>>,
    pub qualified_members: std::collections::BTreeSet<(String, String, bool, Vec<String>)>,
    /// Source-local owners cannot be looked up by an importable database FQN.
    pub local_types: std::collections::BTreeMap<String, DependencyImportDeclaration>,
    pub local_bindings: Vec<(String, String, std::ops::Range<usize>)>,
}

/// Import metadata retains hiding barriers even for inaccessible members.
#[derive(Clone, Default)]
pub(crate) struct DependencyImportDeclaration {
    pub accessible: bool,
    pub member_accessible: bool,
    pub protected_member: bool,
    pub access_barriers: Vec<(String, bool)>,
    pub protected_names: std::collections::HashSet<(String, bool)>,
    pub instance_names: std::collections::HashSet<(String, bool)>,
    pub protected_instance_names: std::collections::HashSet<(String, bool)>,
    pub private_static_names: std::collections::HashSet<(String, bool)>,
    pub private_instance_names: std::collections::HashSet<(String, bool)>,
    pub package_member: bool,
    pub static_member: bool,
    pub static_names: std::collections::HashSet<(String, bool)>,
    pub declared_names: std::collections::HashSet<(String, bool)>,
    pub package_names: std::collections::HashSet<(String, bool)>,
    pub parents: Vec<String>,
    pub package: String,
    pub imports: Vec<(String, bool)>,
    pub interface: bool,
    /// Field/return signatures retain source variables and explicit arguments;
    /// ambiguous overloads retain all signatures.
    pub value_types: std::collections::HashMap<(String, bool), Vec<DependencyMemberType>>,
    pub type_parameters: Vec<String>,
    pub type_parameter_bounds: std::collections::HashMap<String, DependencyResultType>,
    pub parent_types: Vec<DependencyResultType>,
}

/// Source signatures retain variables and explicit arguments without erasure.
#[derive(Clone)]
pub(crate) enum DependencyResultType {
    Named(String, Vec<Option<DependencyResultType>>),
    Parameter { owner: String, name: String },
    Array(Box<DependencyResultType>, usize),
}

pub(crate) fn dependency_result_type(
    ty: Node<'_>,
    content: &str,
    package: &str,
    depth: usize,
) -> Option<DependencyResultType> {
    if depth >= 16 || ty.has_error() {
        return None;
    }
    if ty.kind() == "array_type" {
        let rank = node_text(content, &ty.child_by_field_name("dimensions")?)
            .matches('[')
            .count();
        return Some(DependencyResultType::Array(
            Box::new(dependency_result_type(
                ty.child_by_field_name("element")?,
                content,
                package,
                depth + 1,
            )?),
            rank,
        ));
    }
    if ty.kind() == "generic_type" {
        let name = ty.named_child(0)?;
        let arguments = ty.named_child(1)?;
        if arguments.kind() != "type_arguments" {
            return None;
        }
        let mut cursor = arguments.walk();
        return Some(DependencyResultType::Named(
            node_text(content, &name).to_owned(),
            arguments
                .named_children(&mut cursor)
                .map(|argument| dependency_result_type(argument, content, package, depth + 1))
                .collect(),
        ));
    }
    if !matches!(
        ty.kind(),
        "type_identifier"
            | "scoped_type_identifier"
            | "integral_type"
            | "floating_point_type"
            | "boolean_type"
    ) {
        return None;
    }
    let name = node_text(content, &ty);
    let mut ancestor = ty.parent();
    while let Some(node) = ancestor {
        if let Some(parameters) = node.child_by_field_name("type_parameters") {
            let mut cursor = parameters.walk();
            if parameters.named_children(&mut cursor).any(|parameter| {
                parameter
                    .named_child(0)
                    .is_some_and(|id| node_text(content, &id) == name)
            }) {
                // Method inference is a separate contract; a shadowing method
                // variable must never receive the enclosing class's argument.
                if node.kind() == "method_declaration" {
                    return None;
                }
                return Some(DependencyResultType::Parameter {
                    owner: dependency_type_identity(node, content, package),
                    name: name.to_owned(),
                });
            }
        }
        ancestor = node.parent();
    }
    Some(DependencyResultType::Named(name.to_owned(), Vec::new()))
}

#[derive(Clone)]
pub(crate) struct DependencyMemberType {
    pub path: Option<String>,
    pub result_type: Option<DependencyResultType>,
    pub array_result_type: Option<DependencyResultType>,
    pub position: usize,
    pub arity: Option<usize>,
    pub varargs: bool,
    pub contexts: Vec<String>,
    pub parameters: Option<Vec<Option<String>>>,
    pub parameter_variables: Vec<Option<(DependencyResultType, usize)>>,
    pub method_generic: bool,
    pub method_signature: Option<DependencyMethodSignature>,
    pub is_static: bool,
    pub public: bool,
    pub private: bool,
    pub protected: bool,
}

#[derive(Clone)]
pub(crate) struct DependencyMethodSignature {
    pub variables: Vec<(String, Option<DependencyResultType>)>,
    pub parameters: Vec<Option<(DependencyResultType, usize)>>,
    pub result: Option<DependencyResultType>,
    pub array_result: Option<DependencyResultType>,
}

/// Retain method variables separately from the class substitution contract.
fn dependency_method_result_type(
    ty: Node<'_>,
    content: &str,
    package: &str,
    names: &[String],
    depth: usize,
) -> Option<DependencyResultType> {
    if depth >= 16 || ty.has_error() {
        return None;
    }
    if ty.kind() == "array_type" {
        let rank = node_text(content, &ty.child_by_field_name("dimensions")?)
            .matches('[')
            .count();
        return Some(DependencyResultType::Array(
            Box::new(dependency_method_result_type(
                ty.child_by_field_name("element")?,
                content,
                package,
                names,
                depth + 1,
            )?),
            rank,
        ));
    }
    if ty.kind() == "generic_type" {
        let arguments = ty.named_child(1)?;
        return Some(DependencyResultType::Named(
            node_text(content, &ty.named_child(0)?).to_owned(),
            arguments
                .named_children(&mut arguments.walk())
                .map(|argument| {
                    dependency_method_result_type(argument, content, package, names, depth + 1)
                })
                .collect(),
        ));
    }
    let name = node_text(content, &ty);
    if ty.kind() == "type_identifier" && names.iter().any(|variable| variable == name) {
        return Some(DependencyResultType::Parameter {
            owner: "@method".into(),
            name: name.into(),
        });
    }
    dependency_result_type(ty, content, package, depth)
}

fn dependency_method_signature(
    member: Node<'_>,
    content: &str,
    package: &str,
) -> Option<DependencyMethodSignature> {
    let variables = member.child_by_field_name("type_parameters")?;
    let nodes: Vec<_> = variables.named_children(&mut variables.walk()).collect();
    let names: Vec<_> = nodes
        .iter()
        .map(|node| {
            node.named_children(&mut node.walk())
                .find(|child| child.kind() == "type_identifier")
                .map(|name| node_text(content, &name).to_owned())
        })
        .collect::<Option<_>>()?;
    let mut variables = Vec::new();
    for (node, name) in nodes.iter().zip(&names) {
        let bound = node
            .named_children(&mut node.walk())
            .find(|child| child.kind() == "type_bound");
        let bound = if let Some(bound) = bound {
            let bounds: Vec<_> = bound.named_children(&mut bound.walk()).collect();
            let [bound] = bounds.as_slice() else {
                return None;
            };
            Some(dependency_method_result_type(
                *bound, content, package, &names, 0,
            )?)
        } else {
            None
        };
        variables.push((name.clone(), bound));
    }
    let parameters = member.child_by_field_name("parameters")?;
    let parameters = parameters
        .named_children(&mut parameters.walk())
        .filter(|p| matches!(p.kind(), "formal_parameter" | "spread_parameter"))
        .map(|p| {
            let mut ty = parameter_type(p)?;
            let mut dimensions = usize::from(p.kind() == "spread_parameter");
            for child in p
                .named_children(&mut p.walk())
                .filter(|c| c.kind() == "dimensions")
            {
                dimensions += node_text(content, &child).matches('[').count();
            }
            while ty.kind() == "array_type" {
                dimensions += ty
                    .child_by_field_name("dimensions")
                    .map_or(0, |node| node_text(content, &node).matches('[').count());
                ty = ty.child_by_field_name("element")?;
            }
            let signature = dependency_method_result_type(ty, content, package, &names, 0)?;
            (ty.kind() == "generic_type"
                || matches!(&signature, DependencyResultType::Parameter { owner, .. } if owner == "@method"))
                .then_some((signature, dimensions))
        })
        .collect();
    let result = member
        .child_by_field_name("type")
        .filter(|ty| {
            ty.kind() != "array_type"
                && !member
                    .named_children(&mut member.walk())
                    .any(|n| n.kind() == "dimensions")
        })
        .and_then(|ty| dependency_method_result_type(ty, content, package, &names, 0));
    let array_result = member
        .child_by_field_name("type")
        .and_then(|ty| dependency_method_result_type(ty, content, package, &names, 0))
        .and_then(|ty| dependency_array_result(ty, member, content));
    Some(DependencyMethodSignature {
        variables,
        parameters,
        result,
        array_result,
    })
}

/// Keep array endpoints separate from the legacy scalar result contract.
fn dependency_array_result(
    result: DependencyResultType,
    declaration: Node<'_>,
    content: &str,
) -> Option<DependencyResultType> {
    let rank = declaration
        .named_children(&mut declaration.walk())
        .filter(|node| node.kind() == "dimensions")
        .map(|node| node_text(content, &node).matches('[').count())
        .sum::<usize>()
        + usize::from(declaration.kind() == "spread_parameter");
    if rank > 0 {
        Some(DependencyResultType::Array(Box::new(result), rank))
    } else if matches!(result, DependencyResultType::Array(_, _)) {
        Some(result)
    } else {
        None
    }
}

fn dependency_member_type(member: Node<'_>, content: &str, package: &str) -> DependencyMemberType {
    fn parameter(mut node: Node<'_>, name: &str, content: &str) -> bool {
        loop {
            if let Some(parameters) = node.child_by_field_name("type_parameters") {
                let mut cursor = parameters.walk();
                if parameters.named_children(&mut cursor).any(|parameter| {
                    parameter
                        .named_child(0)
                        .is_some_and(|identifier| node_text(content, &identifier) == name)
                }) {
                    return true;
                }
            }
            let Some(parent) = node.parent() else {
                return false;
            };
            node = parent;
        }
    }
    let ty = member.child_by_field_name("type");
    // Arrays, type variables and generic projections need signature inference;
    // their erasure is not evidence of a nominal result receiver.
    let path = ty
        .filter(|ty| {
            matches!(ty.kind(), "type_identifier" | "scoped_type_identifier")
                && !parameter(*ty, node_text(content, ty), content)
                && !member
                    .named_children(&mut member.walk())
                    .any(|n| n.kind() == "dimensions")
        })
        .map(|ty| node_text(content, &ty).to_owned());
    let arity = member.child_by_field_name("parameters").map(|parameters| {
        let mut count = 0;
        let mut cursor = parameters.walk();
        for parameter in parameters.named_children(&mut cursor) {
            count += usize::from(matches!(
                parameter.kind(),
                "formal_parameter" | "spread_parameter"
            ));
        }
        count
    });
    let varargs = member
        .child_by_field_name("parameters")
        .is_some_and(|parameters| {
            parameters
                .named_children(&mut parameters.walk())
                .any(|p| p.kind() == "spread_parameter")
        });
    let modifiers = member
        .named_children(&mut member.walk())
        .find(|child| child.kind() == "modifiers");
    let modifier = |word| {
        modifiers.is_some_and(|node| {
            node.children(&mut node.walk())
                .any(|child| node_text(content, &child) == word)
        })
    };
    let interface_member = member
        .parent()
        .is_some_and(|body| matches!(body.kind(), "interface_body" | "annotation_type_body"));
    let mut parameter_variables = Vec::new();
    let parameters = member.child_by_field_name("parameters").map(|parameters| {
        let mut result = Vec::new();
        for p in parameters
            .named_children(&mut parameters.walk())
            .filter(|p| !p.is_extra())
        {
            if !matches!(p.kind(), "formal_parameter" | "spread_parameter") {
                continue;
            }
            parameter_variables.push(parameter_type(p).and_then(|mut ty| {
                let mut dimensions = usize::from(p.kind() == "spread_parameter");
                for child in p.named_children(&mut p.walk()) {
                    if child.kind() == "dimensions" {
                        dimensions += node_text(content, &child).matches('[').count();
                    }
                }
                while ty.kind() == "array_type" {
                    dimensions += ty
                        .child_by_field_name("dimensions")
                        .map_or(0, |node| node_text(content, &node).matches('[').count());
                    ty = ty.child_by_field_name("element")?;
                }
                let variable = dependency_result_type(ty, content, package, 0)?;
                (matches!(&variable, DependencyResultType::Parameter { .. })
                    || matches!(&variable, DependencyResultType::Named(_, arguments) if !arguments.is_empty()))
                    .then_some((variable, dimensions))
            }));
            result.push(parameter_type(p).and_then(|ty| {
                // Inference and parameterized formal conversions are separate
                // contracts. Never erase them into a guessed overload match.
                let mut element = ty;
                while element.kind() == "array_type" {
                    element = element.child_by_field_name("element")?;
                }
                if element.kind() == "generic_type"
                    || parameter(element, node_text(content, &element), content)
                {
                    return None;
                }
                let dimensions = p
                    .named_children(&mut p.walk())
                    .filter(|child| child.kind() == "dimensions")
                    .map(|child| node_text(content, &child))
                    .collect::<String>();
                Some(
                    format!(
                        "{}{dimensions}{}",
                        node_text(content, &ty),
                        if p.kind() == "spread_parameter" {
                            "[]"
                        } else {
                            ""
                        }
                    )
                    .chars()
                    .filter(|c| !c.is_whitespace())
                    .collect(),
                )
            }));
        }
        result
    });
    DependencyMemberType {
        path,
        array_result_type: ty
            .or_else(|| {
                (member.kind() == "spread_parameter")
                    .then(|| parameter_type(member))
                    .flatten()
            })
            .and_then(|ty| dependency_result_type(ty, content, package, 0))
            .and_then(|ty| dependency_array_result(ty, member, content)),
        result_type: ty
            .filter(|ty| {
                // Array endpoints are a separate receiver contract. Retain
                // nested array slots without erasing a top-level array result.
                ty.kind() != "array_type"
                    && !member
                        .named_children(&mut member.walk())
                        .any(|n| n.kind() == "dimensions")
            })
            .and_then(|ty| dependency_result_type(ty, content, package, 0)),
        position: ty.map_or(member.start_byte(), |ty| ty.start_byte()),
        arity,
        varargs,
        contexts: dependency_contexts(member, content, package),
        parameters,
        parameter_variables,
        method_generic: member.child_by_field_name("type_parameters").is_some(),
        method_signature: dependency_method_signature(member, content, package),
        is_static: modifier("static"),
        public: modifier("public") || interface_member && !modifier("private"),
        private: modifier("private"),
        protected: modifier("protected"),
    }
}

/// Keep local declarations distinct even when names and source lines collide.
fn dependency_type_identity(node: Node<'_>, content: &str, package: &str) -> String {
    let mut names = Vec::new();
    let mut current = Some(node);
    while let Some(owner) = current {
        if matches!(
            owner.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        ) {
            if let Some(name) = owner.child_by_field_name("name") {
                let mut value = node_text(content, &name).to_owned();
                if owner
                    .parent()
                    .is_some_and(|parent| matches!(parent.kind(), "block" | "switch_block"))
                {
                    value.push_str(&format!("@{}", owner.start_byte()));
                }
                names.push(value);
            }
        }
        current = owner.parent();
    }
    names.reverse();
    if package.is_empty() {
        names.join(".")
    } else {
        format!("{package}.{}", names.join("."))
    }
}

pub(crate) fn dependency_import_declaration(
    content: &str,
    qualified: &str,
    accessing_package: &str,
) -> Result<Option<DependencyImportDeclaration>> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    Ok(
        dependency_declarations(&tree, content, accessing_package, Some(qualified))?
            .remove(qualified),
    )
}

/// Extract either one importable declaration or all occurrence-owned local types.
fn dependency_declarations(
    tree: &tree_sitter::Tree,
    content: &str,
    accessing_package: &str,
    qualified: Option<&str>,
) -> Result<std::collections::BTreeMap<String, DependencyImportDeclaration>> {
    let imports = import_declarations(content)?;
    fn is_type(node: Node<'_>) -> bool {
        matches!(
            node.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        )
    }
    fn modifier(node: Node<'_>, content: &str, keyword: &str) -> bool {
        let mut cursor = node.walk();
        let found = node
            .named_children(&mut cursor)
            .find(|child| child.kind() == "modifiers")
            .is_some_and(|modifiers| {
                let mut cursor = modifiers.walk();
                let found = modifiers
                    .children(&mut cursor)
                    .any(|child| node_text(content, &child) == keyword);
                found
            });
        found
    }
    fn interface_member(node: Node<'_>) -> bool {
        node.parent()
            .is_some_and(|body| matches!(body.kind(), "interface_body" | "annotation_type_body"))
    }
    fn accessible(node: Node<'_>, content: &str, same_package: bool) -> bool {
        !modifier(node, content, "private")
            && (modifier(node, content, "public") || interface_member(node) || same_package)
    }
    fn package_member(node: Node<'_>, content: &str) -> bool {
        !interface_member(node)
            && !["public", "private", "protected"]
                .iter()
                .any(|keyword| modifier(node, content, keyword))
    }
    let mut package = String::new();
    let mut cursor = tree.root_node().walk();
    for node in tree.root_node().named_children(&mut cursor) {
        if node.kind() == "package_declaration" {
            let mut parts = Vec::new();
            super::walk_tree_preorder(&node, |child| {
                if matches!(child.kind(), "annotation" | "marker_annotation") {
                    return super::WalkControl::SkipChildren;
                }
                if child.kind() == "identifier" {
                    parts.push(node_text(content, &child));
                }
                super::WalkControl::Continue
            });
            package = parts.join(".");
        }
    }
    let mut result = std::collections::BTreeMap::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        if !is_type(node) {
            return super::WalkControl::Continue;
        }
        let mut current = Some(node);
        let mut allowed = true;
        let mut barriers = Vec::new();
        while let Some(ancestor) = current {
            if is_type(ancestor) {
                let accessible = accessible(ancestor, content, package == accessing_package);
                allowed &= accessible;
                if !accessible {
                    barriers.push((ancestor, modifier(ancestor, content, "protected")));
                }
            } else if !matches!(
                ancestor.kind(),
                "program"
                    | "class_body"
                    | "interface_body"
                    | "enum_body"
                    | "enum_body_declarations"
                    | "annotation_type_body"
            ) {
                // Only source-owned lookup can enter a local declaration.
                if qualified.is_some() {
                    return super::WalkControl::Continue;
                }
            }
            current = ancestor.parent();
        }
        let name = dependency_type_identity(node, content, &package);
        if qualified.map_or(!name.contains('@'), |qualified| name != qualified) {
            return super::WalkControl::Continue;
        }
        // Every lexical enclosing owner is already available at this source site.
        if qualified.is_none() {
            allowed = true;
            barriers.clear();
        }
        let static_member = node
            .parent()
            .is_some_and(|parent| parent.kind() != "program")
            && (modifier(node, content, "static")
                || interface_member(node)
                || matches!(
                    node.kind(),
                    "interface_declaration"
                        | "annotation_type_declaration"
                        | "enum_declaration"
                        | "record_declaration"
                ));
        let mut protected_names = std::collections::HashSet::new();
        let mut instance_names = std::collections::HashSet::new();
        let mut protected_instance_names = std::collections::HashSet::new();
        let mut private_static_names = std::collections::HashSet::new();
        let mut private_instance_names = std::collections::HashSet::new();
        let mut static_names = std::collections::HashSet::new();
        let mut declared_names = std::collections::HashSet::new();
        let mut package_names = std::collections::HashSet::new();
        let mut value_types = std::collections::HashMap::<_, Vec<_>>::new();
        if let Some(body) = node.child_by_field_name("body") {
            super::walk_tree_preorder(&body, |member| {
                if member.id() == body.id() || member.kind() == "enum_body_declarations" {
                    return super::WalkControl::Continue;
                }
                if member.kind() == "enum_constant" {
                    if let Some(name) = member.child_by_field_name("name") {
                        let key = (node_text(content, &name).to_owned(), false);
                        declared_names.insert(key.clone());
                        static_names.insert(key);
                    }
                    return super::WalkControl::SkipChildren;
                }
                if !matches!(
                    member.kind(),
                    "field_declaration" | "constant_declaration" | "method_declaration"
                ) {
                    return super::WalkControl::SkipChildren;
                }
                let is_static = modifier(member, content, "static")
                    || interface_member(member) && member.kind() != "method_declaration";
                let allowed = accessible(member, content, package == accessing_package);
                let mut record = |key: (String, bool)| {
                    declared_names.insert(key.clone());
                    if package_member(member, content) {
                        package_names.insert(key.clone());
                    }
                    if modifier(member, content, "protected") {
                        if is_static {
                            protected_names.insert(key.clone());
                        } else {
                            protected_instance_names.insert(key.clone());
                        }
                    }
                    if modifier(member, content, "private") {
                        if is_static {
                            private_static_names.insert(key.clone());
                        } else {
                            private_instance_names.insert(key.clone());
                        }
                    }
                    if allowed {
                        if is_static {
                            static_names.insert(key);
                        } else {
                            instance_names.insert(key);
                        }
                    }
                };
                if member.kind() == "method_declaration" {
                    if let Some(name) = member.child_by_field_name("name") {
                        let key = (node_text(content, &name).to_owned(), true);
                        record(key.clone());
                        value_types
                            .entry(key)
                            .or_default()
                            .push(dependency_member_type(member, content, &package));
                    }
                } else {
                    let mut cursor = member.walk();
                    for variable in member
                        .named_children(&mut cursor)
                        .filter(|n| n.kind() == "variable_declarator")
                    {
                        if let Some(name) = variable.child_by_field_name("name") {
                            let key = (node_text(content, &name).to_owned(), false);
                            record(key.clone());
                            let mut value = dependency_member_type(member, content, &package);
                            if variable
                                .named_children(&mut variable.walk())
                                .any(|n| n.kind() == "dimensions")
                            {
                                value.path = None;
                                value.array_result_type = value
                                    .array_result_type
                                    .take()
                                    .or_else(|| value.result_type.take())
                                    .and_then(|ty| dependency_array_result(ty, variable, content));
                                value.result_type = None;
                            }
                            value_types.entry(key).or_default().push(value);
                        }
                    }
                }
                super::WalkControl::SkipChildren
            });
        }
        if node.kind() == "record_declaration" {
            if let Some(parameters) = node.child_by_field_name("parameters") {
                let mut cursor = parameters.walk();
                for component in parameters.named_children(&mut cursor).filter(|component| {
                    matches!(component.kind(), "formal_parameter" | "spread_parameter")
                }) {
                    let component_name = component.child_by_field_name("name").or_else(|| {
                        component
                            .named_children(&mut component.walk())
                            .find(|child| child.kind() == "variable_declarator")
                            .and_then(|variable| variable.child_by_field_name("name"))
                    });
                    let Some(component_name) = component_name else {
                        continue;
                    };
                    let component_name = node_text(content, &component_name).to_owned();
                    let mut value = dependency_member_type(component, content, &package);
                    // Component types are declared in the record header, but
                    // may name the record's own member types.
                    if value.contexts.first() != Some(&name) {
                        value.contexts.insert(0, name.clone());
                    }
                    if component.kind() == "spread_parameter" {
                        value.path = None;
                        value.result_type = None;
                    }
                    let field = (component_name.clone(), false);
                    declared_names.insert(field.clone());
                    private_instance_names.insert(field.clone());
                    value_types.entry(field).or_default().push(value.clone());

                    let method = (component_name, true);
                    let signatures = value_types.entry(method.clone()).or_default();
                    // An overload does not replace the implicit accessor. An
                    // explicitly declared zero-argument accessor does.
                    if !signatures
                        .iter()
                        .any(|signature| signature.arity == Some(0))
                    {
                        value.arity = Some(0);
                        value.parameters = Some(Vec::new());
                        value.public = true;
                        signatures.push(value);
                        declared_names.insert(method.clone());
                        instance_names.insert(method);
                    }
                }
            }
        }
        let mut parents = Vec::new();
        let mut parent_types = Vec::new();
        let mut cursor = node.walk();
        for branch in node.named_children(&mut cursor).filter(|child| {
            matches!(
                child.kind(),
                "superclass" | "super_interfaces" | "extends_interfaces"
            )
        }) {
            super::walk_tree_preorder(&branch, |ty| {
                if matches!(
                    ty.kind(),
                    "type_identifier" | "scoped_type_identifier" | "generic_type"
                ) {
                    if let Some(signature) = dependency_result_type(ty, content, &package, 0) {
                        parent_types.push(signature);
                    }
                    let ty = if ty.kind() == "generic_type" {
                        ty.named_child(0).unwrap_or(ty)
                    } else {
                        ty
                    };
                    parents.push(node_text(content, &ty).to_owned());
                    return super::WalkControl::SkipChildren;
                }
                super::WalkControl::Continue
            });
        }
        result.insert(
            name,
            DependencyImportDeclaration {
                accessible: allowed,
                member_accessible: accessible(node, content, package == accessing_package),
                protected_member: modifier(node, content, "protected"),
                access_barriers: barriers
                    .into_iter()
                    .map(|(barrier, protected)| {
                        let mut enclosing = Vec::new();
                        let mut parent = barrier.parent();
                        while let Some(ancestor) = parent {
                            if is_type(ancestor) {
                                if let Some(name) = ancestor.child_by_field_name("name") {
                                    enclosing.push(node_text(content, &name));
                                }
                            }
                            parent = ancestor.parent();
                        }
                        enclosing.reverse();
                        let owner = if package.is_empty() {
                            enclosing.join(".")
                        } else {
                            format!("{package}.{}", enclosing.join("."))
                        };
                        (owner, protected)
                    })
                    .collect(),
                protected_names,
                instance_names,
                protected_instance_names,
                private_static_names,
                private_instance_names,
                package_member: package_member(node, content),
                static_member,
                static_names,
                declared_names,
                package_names,
                parents,
                package: package.clone(),
                imports: imports.clone(),
                interface: matches!(
                    node.kind(),
                    "interface_declaration" | "annotation_type_declaration"
                ),
                value_types,
                type_parameters: node.child_by_field_name("type_parameters").map_or_else(
                    Vec::new,
                    |parameters| {
                        parameters
                            .named_children(&mut parameters.walk())
                            .filter_map(|parameter| parameter.named_child(0))
                            .map(|name| node_text(content, &name).to_owned())
                            .collect()
                    },
                ),
                type_parameter_bounds: node.child_by_field_name("type_parameters").map_or_else(
                    std::collections::HashMap::new,
                    |parameters| {
                        parameters
                            .named_children(&mut parameters.walk())
                            .filter_map(|parameter| {
                                let name = parameter.named_child(0)?;
                                let bound = parameter.named_child(1)?;
                                if bound.kind() != "type_bound" || bound.named_child_count() != 1 {
                                    return None;
                                }
                                Some((
                                    node_text(content, &name).to_owned(),
                                    dependency_result_type(
                                        bound.named_child(0)?,
                                        content,
                                        &package,
                                        0,
                                    )?,
                                ))
                            })
                            .collect()
                    },
                ),
                parent_types,
            },
        );
        if qualified.is_some() {
            super::WalkControl::SkipChildren
        } else {
            super::WalkControl::Continue
        }
    });
    Ok(result)
}

#[derive(Default)]
struct StatementFlow {
    normal: bool,
    breaks: HashSet<usize>,
    continues: HashSet<usize>,
    uncertain: bool,
}

impl StatementFlow {
    fn merge(&mut self, other: Self) {
        self.normal |= other.normal;
        self.breaks.extend(other.breaks);
        self.continues.extend(other.continues);
        self.uncertain |= other.uncertain;
    }
}

fn jump_target(node: Node<'_>, source: &str) -> Option<usize> {
    let label = node.named_child(0).map(|label| node_text(source, &label));
    let mut parent = node.parent();
    while let Some(scope) = parent {
        if matches!(
            scope.kind(),
            "lambda_expression" | "method_declaration" | "constructor_declaration"
        ) {
            break;
        }
        if let Some(label) = label {
            if scope.kind() == "labeled_statement"
                && scope
                    .named_child(0)
                    .is_some_and(|name| node_text(source, &name) == label)
            {
                return if node.kind() == "continue_statement" {
                    scope.named_child(1).map(|body| body.id())
                } else {
                    Some(scope.id())
                };
            }
        } else if matches!(
            scope.kind(),
            "while_statement" | "for_statement" | "enhanced_for_statement" | "do_statement"
        ) || (node.kind() == "break_statement" && scope.kind() == "switch_expression")
        {
            return Some(scope.id());
        }
        parent = scope.parent();
    }
    None
}

/// Track normal completion and escaping jumps without inspecting nested callables.
fn statement_flow(node: Node<'_>, source: &str, depth: usize) -> StatementFlow {
    let normal = || StatementFlow {
        normal: true,
        ..Default::default()
    };
    if depth >= 128 {
        return StatementFlow {
            uncertain: true,
            ..normal()
        };
    }
    let flow = |child| statement_flow(child, source, depth + 1);
    match node.kind() {
        "return_statement" | "throw_statement" => StatementFlow::default(),
        "break_statement" | "continue_statement" => {
            let Some(target) = jump_target(node, source) else {
                return StatementFlow {
                    uncertain: true,
                    ..normal()
                };
            };
            let mut result = StatementFlow::default();
            if node.kind() == "break_statement" {
                result.breaks.insert(target);
            } else {
                result.continues.insert(target);
            }
            result
        }
        "block" | "constructor_body" | "switch_block_statement_group" => {
            let mut result = normal();
            let mut cursor = node.walk();
            for child in node
                .named_children(&mut cursor)
                .filter(|child| !child.is_extra())
            {
                if !result.normal {
                    break;
                }
                result.normal = false;
                result.merge(flow(child));
            }
            result
        }
        "if_statement" => {
            let mut result = node
                .child_by_field_name("consequence")
                .map_or_else(normal, flow);
            result.merge(
                node.child_by_field_name("alternative")
                    .map_or_else(normal, flow),
            );
            result
        }
        "synchronized_statement" | "catch_clause" | "finally_clause" => node
            .child_by_field_name("body")
            .or_else(|| {
                let mut cursor = node.walk();
                let body = node
                    .named_children(&mut cursor)
                    .find(|child| child.kind() == "block");
                body
            })
            .map_or_else(normal, flow),
        "try_statement" | "try_with_resources_statement" => {
            let mut result = node.child_by_field_name("body").map_or_else(normal, flow);
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                if child.kind() == "catch_clause" {
                    result.merge(flow(child));
                } else if child.kind() == "finally_clause" {
                    let final_flow = flow(child);
                    if !final_flow.normal && !final_flow.uncertain {
                        return final_flow;
                    }
                    let was_normal = result.normal;
                    result.merge(final_flow);
                    result.normal = was_normal;
                }
            }
            result
        }
        "labeled_statement" => {
            let mut result = node.named_child(1).map_or_else(normal, flow);
            result.normal |= result.breaks.remove(&node.id());
            result
        }
        "while_statement" | "for_statement" | "enhanced_for_statement" | "do_statement" => {
            let mut result = node.child_by_field_name("body").map_or_else(normal, flow);
            let reaches_condition = result.normal | result.continues.remove(&node.id());
            let constant_true = node.child_by_field_name("condition").map_or(
                node.kind() == "for_statement",
                |condition| {
                    node_text(source, &condition).trim_matches(['(', ')', ' ', '\n']) == "true"
                },
            );
            result.normal = (!constant_true
                && (node.kind() != "do_statement" || reaches_condition))
                | result.breaks.remove(&node.id());
            result
        }
        "switch_expression" => {
            // Preserve escaping jumps from all arms; normal switch completion
            // remains conservative rather than assuming exhaustiveness.
            let mut result = normal();
            if let Some(body) = node.child_by_field_name("body") {
                let mut cursor = body.walk();
                for child in body.named_children(&mut cursor) {
                    result.merge(flow(child));
                }
            }
            result.breaks.remove(&node.id());
            result
        }
        "switch_rule" => {
            let mut result = normal();
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                result.merge(flow(child));
            }
            result
        }
        _ => normal(),
    }
}

/// Check where a pattern is definitely matched, without lending its type to
/// the opposite branch or to expressions evaluated before the match.
pub(crate) fn pattern_flow_scopes<'a>(pattern: Node<'a>, source: &str) -> Vec<(Node<'a>, usize)> {
    let mut scopes = Vec::new();
    let mut expression = pattern;
    let (mut on_true, mut on_false) = (true, false);
    let position = pattern.end_byte();
    while let Some(parent) = expression.parent() {
        match parent.kind() {
            "parenthesized_expression" => {}
            "unary_expression"
                if parent
                    .child_by_field_name("operator")
                    .is_some_and(|op| node_text(source, &op) == "!") =>
            {
                std::mem::swap(&mut on_true, &mut on_false);
            }
            "binary_expression" => {
                let operator = parent.child_by_field_name("operator");
                let operator = operator
                    .map(|op| node_text(source, &op))
                    .unwrap_or_default();
                if !matches!(operator, "&&" | "||") {
                    break;
                }
                if parent
                    .child_by_field_name("left")
                    .is_some_and(|left| left.id() == expression.id())
                    && ((operator == "&&" && on_true) || (operator == "||" && on_false))
                {
                    if let Some(right) = parent.child_by_field_name("right") {
                        scopes.push((right, position));
                    }
                }
                // A conjunction can be false without evaluating the pattern;
                // a disjunction can be true without matching it.
                if operator == "&&" {
                    on_false = false;
                } else {
                    on_true = false;
                }
            }
            "ternary_expression" | "if_statement" => {
                if !parent
                    .child_by_field_name("condition")
                    .is_some_and(|condition| condition.id() == expression.id())
                {
                    break;
                }
                let consequence = parent.child_by_field_name("consequence");
                let alternative = parent.child_by_field_name("alternative");
                for (branch, matched) in [(consequence, on_true), (alternative, on_false)] {
                    if matched {
                        scopes.extend(branch.map(|branch| (branch, position)));
                    }
                }
                if parent.kind() == "if_statement" {
                    let terminal = |branch: Option<Node<'_>>| {
                        branch.is_some_and(|branch| {
                            let flow = statement_flow(branch, source, 0);
                            !flow.normal && !flow.uncertain
                        })
                    };
                    if (on_false && terminal(consequence) && !terminal(alternative))
                        || (on_true && terminal(alternative) && !terminal(consequence))
                    {
                        if let Some(block) = parent
                            .parent()
                            .filter(|node| matches!(node.kind(), "block" | "constructor_body"))
                        {
                            scopes.push((block, parent.end_byte()));
                        }
                    }
                }
                break;
            }
            "while_statement" | "for_statement" | "do_statement" => {
                let condition = parent
                    .child_by_field_name("condition")
                    .is_some_and(|condition| condition.id() == expression.id());
                if on_true && parent.kind() != "do_statement" && condition {
                    scopes.extend(
                        parent
                            .child_by_field_name("body")
                            .map(|body| (body, position)),
                    );
                    if parent.kind() == "for_statement" {
                        let mut cursor = parent.walk();
                        scopes.extend(
                            parent
                                .children_by_field_name("update", &mut cursor)
                                .map(|update| (update, position)),
                        );
                    }
                }
                if on_false && condition {
                    // Breaks consumed by inner loops/labels do not exit this
                    // loop. Escaping breaks bypass the matched condition exit.
                    let flow = parent
                        .child_by_field_name("body")
                        .map(|body| statement_flow(body, source, 0));
                    if flow.is_some_and(|flow| flow.breaks.is_empty() && !flow.uncertain) {
                        if let Some(block) = parent
                            .parent()
                            .filter(|node| matches!(node.kind(), "block" | "constructor_body"))
                        {
                            scopes.push((block, parent.end_byte()));
                        }
                    }
                }
                break;
            }
            _ => break,
        }
        expression = parent;
    }
    scopes
}

pub(crate) fn dependency_contexts(node: Node<'_>, content: &str, package: &str) -> Vec<String> {
    let mut result = Vec::new();
    let mut parent = node.parent();
    while let Some(owner) = parent {
        if matches!(
            owner.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        ) && (owner
            .child_by_field_name("body")
            .is_some_and(|body| body.byte_range().contains(&node.start_byte()))
            || owner.kind() == "record_declaration"
                && owner
                    .child_by_field_name("parameters")
                    .is_some_and(|parameters| parameters.byte_range().contains(&node.start_byte())))
        {
            result.push(dependency_type_identity(owner, content, package));
        }
        parent = owner.parent();
    }
    result
}

/// Instance availability is occurrence-scoped, including each enclosing capture.
pub(crate) fn dependency_instances(
    node: Node<'_>,
    content: &str,
    package: &str,
) -> std::collections::BTreeSet<String> {
    let owners = dependency_contexts(node, content, package);
    let mut allowed = std::collections::BTreeSet::new();
    let mut parent = Some(node);
    let mut instance = true;
    let mut owner_index = 0;
    while let Some(scope) = parent {
        let is_static = {
            let mut cursor = scope.walk();
            let found = scope
                .named_children(&mut cursor)
                .find(|child| child.kind() == "modifiers")
                .is_some_and(|modifiers| {
                    let mut cursor = modifiers.walk();
                    let found = modifiers
                        .children(&mut cursor)
                        .any(|child| child.kind() == "static");
                    found
                });
            found
        };
        if scope.kind() == "static_initializer"
            || is_static && matches!(scope.kind(), "method_declaration" | "field_declaration")
        {
            instance = false;
        }
        if matches!(
            scope.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
        ) {
            if scope
                .child_by_field_name("body")
                .is_some_and(|body| body.byte_range().contains(&node.start_byte()))
            {
                if instance {
                    if let Some(owner) = owners.get(owner_index) {
                        allowed.insert(owner.clone());
                    }
                }
                owner_index += 1;
            }
            // Static nested types retain their own instance, but cannot
            // borrow an enclosing one. Lambdas preserve the current this.
            if is_static || scope.kind() != "class_declaration" {
                instance = false;
            }
        }
        parent = scope.parent();
    }
    allowed
}

pub(crate) fn dependency_syntax(content: &str) -> Result<DependencySyntax> {
    fn spelling(node: tree_sitter::Node<'_>, content: &str) -> String {
        let mut parts = Vec::new();
        super::walk_tree_preorder(&node, |child| {
            if child.kind() == "type_arguments" {
                return super::WalkControl::SkipChildren;
            }
            if matches!(child.kind(), "identifier" | "type_identifier" | "asterisk") {
                parts.push(node_text(content, &child));
            }
            super::WalkControl::Continue
        });
        parts.join(".")
    }

    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let lexical = lexical_shadows(tree.root_node(), content);
    let type_shadows = &lexical.types;
    let value_shadows = &lexical.values;
    let method_shadows = &lexical.methods;
    let shadowed = |name: &str, node: tree_sitter::Node<'_>, expression: bool| {
        let first = name.split('.').next().unwrap_or(name);
        let visible = |shadows: &HashMap<String, Vec<std::ops::Range<usize>>>| {
            shadows.get(first).is_some_and(|ranges| {
                ranges
                    .iter()
                    .any(|range| range.contains(&node.start_byte()))
            })
        };
        visible(type_shadows) || (expression && visible(value_shadows))
    };
    fn record_member(
        result: &mut DependencySyntax,
        name: String,
        method: bool,
        node: Node<'_>,
        content: &str,
    ) {
        let owners = dependency_contexts(node, content, &result.package);
        let key = (name, method, owners.clone());
        result
            .instance_contexts
            .entry(key.clone())
            .or_default()
            .extend(dependency_instances(node, content, &result.package));
        result.member_uses.insert(key);
    }
    let mut result = DependencySyntax::default();
    let mut actual_types = std::collections::BTreeSet::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        match node.kind() {
            "package_declaration" => {
                result.package = spelling(node, content);
                return super::WalkControl::SkipChildren;
            }
            "import_declaration" => {
                let mut cursor = node.walk();
                let is_static = node
                    .children(&mut cursor)
                    .any(|child| child.kind() == "static");
                result.imports.push((spelling(node, content), is_static));
                return super::WalkControl::SkipChildren;
            }
            "type_identifier" | "scoped_type_identifier" => {
                // Keep generic arguments as separate types, but do not reduce
                // a qualified name to each of its component identifiers.
                if !node
                    .parent()
                    .is_some_and(|p| p.kind() == "scoped_type_identifier")
                {
                    let declaration = node.parent().is_some_and(|p| {
                        p.child_by_field_name("name")
                            .is_some_and(|name| name.id() == node.id())
                            || (p.kind() == "type_parameter"
                                && p.named_child(0).is_some_and(|name| name.id() == node.id()))
                    });
                    if !declaration {
                        let name = spelling(node, content);
                        if !shadowed(&name, node, false) {
                            actual_types.insert(name.clone());
                            result.type_uses.insert((
                                name.clone(),
                                false,
                                dependency_contexts(node, content, &result.package),
                            ));
                            result.types.insert(name);
                        }
                    }
                }
            }
            "annotation" | "marker_annotation" => {
                if let Some(name) = node.child_by_field_name("name") {
                    let spelling = spelling(name, content);
                    if !shadowed(&spelling, name, false) {
                        actual_types.insert(spelling.clone());
                        result.type_uses.insert((
                            spelling.clone(),
                            false,
                            dependency_contexts(node, content, &result.package),
                        ));
                        result.types.insert(spelling);
                    }
                }
            }
            "method_invocation" | "field_access" => {
                if node.kind() == "method_invocation"
                    && node.child_by_field_name("object").is_none()
                {
                    if let Some(name) = node.child_by_field_name("name") {
                        let text = node_text(content, &name);
                        if !method_shadows.get(text).is_some_and(|ranges| {
                            ranges
                                .iter()
                                .any(|range| range.contains(&name.start_byte()))
                        }) {
                            result.static_names.insert((text.to_owned(), true));
                            record_member(&mut result, text.to_owned(), true, node, content);
                        }
                    }
                }
                if let Some(object) = node.child_by_field_name("object") {
                    if matches!(
                        object.kind(),
                        "identifier" | "scoped_identifier" | "field_access"
                    ) {
                        let name = spelling(object, content);
                        if !shadowed(&name, object, true) {
                            result.expression_types.insert(name.clone());
                            let owners = dependency_contexts(node, content, &result.package);
                            if let Some(member) =
                                node.child_by_field_name(if node.kind() == "field_access" {
                                    "field"
                                } else {
                                    "name"
                                })
                            {
                                result.qualified_members.insert((
                                    name.clone(),
                                    node_text(content, &member).to_owned(),
                                    node.kind() == "method_invocation",
                                    owners.clone(),
                                ));
                            }
                            result
                                .type_uses
                                .insert((name.clone(), true, owners.clone()));
                            record_member(
                                &mut result,
                                name.split('.').next().unwrap_or(&name).to_owned(),
                                false,
                                node,
                                content,
                            );
                            result.static_names.insert((
                                name.split('.').next().unwrap_or(&name).to_owned(),
                                false,
                            ));
                            result.types.insert(name);
                        }
                    }
                }
            }
            "identifier" => {
                if let Some(parent) = node.parent() {
                    let is_name = parent
                        .child_by_field_name("name")
                        .is_some_and(|name| name.id() == node.id());
                    let is_parameter = parent.kind() == "inferred_parameters"
                        || parent.kind() == "lambda_expression"
                            && parent
                                .child_by_field_name("parameters")
                                .is_some_and(|p| p.id() == node.id());
                    if !is_name
                        && !is_parameter
                        && !matches!(
                            parent.kind(),
                            "field_access"
                                | "scoped_identifier"
                                | "method_invocation"
                                | "package_declaration"
                                | "import_declaration"
                                | "annotation"
                                | "marker_annotation"
                                | "scoped_type_identifier"
                        )
                    {
                        let name = node_text(content, &node);
                        if !shadowed(name, node, true) {
                            result.static_names.insert((name.to_owned(), false));
                            record_member(&mut result, name.to_owned(), false, node, content);
                        }
                    }
                }
            }
            "class_declaration"
            | "interface_declaration"
            | "enum_declaration"
            | "record_declaration"
            | "annotation_type_declaration"
            | "type_parameter" => {
                if let Some(name) = node.child_by_field_name("name").or_else(|| {
                    (node.kind() == "type_parameter")
                        .then(|| node.named_child(0))
                        .flatten()
                }) {
                    result
                        .declarations
                        .insert(node_text(content, &name).to_owned());
                }
            }
            "line_comment" | "block_comment" | "string_literal" | "character_literal" => {
                return super::WalkControl::SkipChildren;
            }
            _ => {}
        }
        super::WalkControl::Continue
    });
    result
        .expression_types
        .retain(|name| !actual_types.contains(name));
    result.local_types = dependency_declarations(&tree, content, &result.package, None)?;
    super::walk_tree_preorder(&tree.root_node(), |node| {
        if let Some(parent) = node.parent() {
            if matches!(
                node.kind(),
                "class_declaration"
                    | "interface_declaration"
                    | "enum_declaration"
                    | "record_declaration"
                    | "annotation_type_declaration"
            ) && matches!(parent.kind(), "block" | "switch_block")
            {
                if let Some(name) = node.child_by_field_name("name") {
                    result.local_bindings.push((
                        node_text(content, &name).to_owned(),
                        dependency_type_identity(node, content, &result.package),
                        node.start_byte()..parent.end_byte(),
                    ));
                }
            }
        }
        super::WalkControl::Continue
    });
    Ok(result)
}

/// Source locations of externally public Java declarations, including implicit members.
pub(crate) fn public_api_lines(content: &str) -> Result<Vec<usize>> {
    fn collect(
        node: tree_sitter::Node<'_>,
        content: &str,
        implicit_public: bool,
        lines: &mut std::collections::BTreeSet<usize>,
    ) {
        let mut cursor = node.walk();
        let modifiers = node
            .named_children(&mut cursor)
            .find(|child| child.kind() == "modifiers");
        let has_modifier = |keyword| {
            modifiers.is_some_and(|modifiers| {
                let mut cursor = modifiers.walk();
                let found = modifiers
                    .children(&mut cursor)
                    .any(|child| child.kind() == keyword);
                found
            })
        };
        let public = (implicit_public || has_modifier("public"))
            && !has_modifier("private")
            && !has_modifier("protected");
        match node.kind() {
            "program" | "enum_body_declarations" => {
                let mut cursor = node.walk();
                for child in node.named_children(&mut cursor) {
                    collect(child, content, implicit_public, lines);
                }
            }
            "class_declaration"
            | "interface_declaration"
            | "enum_declaration"
            | "record_declaration"
            | "annotation_type_declaration"
                if public =>
            {
                if let Some(name) = node.child_by_field_name("name") {
                    lines.insert(name.start_position().row + 1);
                }
                let implicit_members = matches!(
                    node.kind(),
                    "interface_declaration" | "annotation_type_declaration"
                );
                let body = node.child_by_field_name("body");
                if node.kind() == "record_declaration" {
                    if let Some(parameters) = node.child_by_field_name("parameters") {
                        let mut cursor = parameters.walk();
                        for parameter in parameters.named_children(&mut cursor) {
                            let name = parameter.child_by_field_name("name").or_else(|| {
                                let mut cursor = parameter.walk();
                                let mut children = parameter.named_children(&mut cursor);
                                children
                                    .find(|child| child.kind() == "variable_declarator")
                                    .and_then(|declarator| declarator.child_by_field_name("name"))
                            });
                            if let Some(name) = name {
                                let overridden = body.is_some_and(|body| {
                                    let mut cursor = body.walk();
                                    let found = body.named_children(&mut cursor).any(|member| {
                                        member.kind() == "method_declaration"
                                            && member.child_by_field_name("name").is_some_and(
                                                |other| {
                                                    node_text(content, &other)
                                                        == node_text(content, &name)
                                                },
                                            )
                                            && member.child_by_field_name("parameters").is_some_and(
                                                |params| params.named_child_count() == 0,
                                            )
                                    });
                                    found
                                });
                                if !overridden {
                                    lines.insert(name.start_position().row + 1);
                                }
                            }
                        }
                    }
                }
                if let Some(body) = body {
                    let mut cursor = body.walk();
                    for member in body.named_children(&mut cursor) {
                        collect(member, content, implicit_members, lines);
                    }
                }
            }
            "field_declaration" | "constant_declaration" if public => {
                let mut cursor = node.walk();
                for declarator in node.named_children(&mut cursor) {
                    if declarator.kind() == "variable_declarator" {
                        if let Some(name) = declarator.child_by_field_name("name") {
                            lines.insert(name.start_position().row + 1);
                        }
                    }
                }
            }
            "enum_constant" => {
                if let Some(name) = node.child_by_field_name("name") {
                    lines.insert(name.start_position().row + 1);
                }
            }
            "method_declaration"
            | "constructor_declaration"
            | "compact_constructor_declaration"
            | "annotation_type_element_declaration"
                if public =>
            {
                if let Some(name) = node.child_by_field_name("name") {
                    lines.insert(name.start_position().row + 1);
                }
            }
            _ => {}
        }
    }
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let mut lines = std::collections::BTreeSet::new();
    collect(tree.root_node(), content, false, &mut lines);
    Ok(lines.into_iter().collect())
}

/// Find type tokens in Java fields and parameters annotated for injection.
pub(crate) fn injection_lines(content: &str, type_re: &regex::Regex) -> Result<Vec<usize>> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let mut lines = std::collections::BTreeSet::new();
    let mut cursor = tree.walk();
    loop {
        let node = cursor.node();
        if node.kind() == "modifiers" {
            let mut children = node.walk();
            let injected = node.named_children(&mut children).any(|annotation| {
                matches!(annotation.kind(), "annotation" | "marker_annotation")
                    && annotation.child_by_field_name("name").is_some_and(|name| {
                        matches!(
                            node_text(content, &name).rsplit('.').next(),
                            Some("Inject" | "Autowired")
                        )
                    })
            });
            if injected {
                if let Some(owner) = node.parent() {
                    let mut types = Vec::new();
                    match owner.kind() {
                        "method_declaration" | "constructor_declaration" => {
                            if let Some(parameters) = owner.child_by_field_name("parameters") {
                                let mut params = parameters.walk();
                                for parameter in parameters.named_children(&mut params) {
                                    if let Some(ty) = parameter_type(parameter) {
                                        types.push(ty);
                                    }
                                }
                            }
                        }
                        "field_declaration"
                        | "local_variable_declaration"
                        | "formal_parameter"
                        | "spread_parameter" => {
                            if let Some(ty) = parameter_type(owner) {
                                types.push(ty);
                            }
                        }
                        _ => {}
                    }
                    for ty in types {
                        let mut pending = vec![ty];
                        while let Some(part) = pending.pop() {
                            match part.kind() {
                                "annotation" | "marker_annotation" => continue,
                                "type_identifier"
                                | "identifier"
                                | "integral_type"
                                | "floating_point_type"
                                | "boolean_type" => {
                                    for token in type_re.find_iter(node_text(content, &part)) {
                                        let offset = part.start_byte() + token.start();
                                        lines.insert(
                                            content[..offset]
                                                .bytes()
                                                .filter(|b| *b == b'\n')
                                                .count(),
                                        );
                                    }
                                }
                                _ => {
                                    let mut fields = part.walk();
                                    pending.extend(part.named_children(&mut fields));
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
                return Ok(lines.into_iter().collect());
            }
        }
    }
}

fn parameter_type(node: tree_sitter::Node<'_>) -> Option<tree_sitter::Node<'_>> {
    node.child_by_field_name("type").or_else(|| {
        if node.kind() != "spread_parameter" {
            return None;
        }
        // Varargs types are unnamed fields in this grammar.
        let mut fields = node.walk();
        let mut children = node.named_children(&mut fields);
        children.find(|field| {
            matches!(field.kind(), "type_identifier" | "scoped_type_identifier")
                || field.kind().ends_with("_type")
        })
    })
}

/// Significant Java/Spring annotations to track
const SIGNIFICANT_ANNOTATIONS: &[&str] = &[
    "RestController",
    "Controller",
    "Service",
    "Repository",
    "Component",
    "Entity",
    "Table",
    "Configuration",
    "Bean",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
    "RequestMapping",
    "Autowired",
    "Override",
    "Transactional",
    "SpringBootApplication",
    "EnableAutoConfiguration",
    "Test",
    "BeforeEach",
    "AfterEach",
    "BeforeAll",
    "AfterAll",
    "Inject",
    "Singleton",
    "Provides",
    "Binds",
    "Module",
    "Data",
    "Value",
    "Builder",
    "AllArgsConstructor",
    "NoArgsConstructor",
    "Getter",
    "Setter",
    "Slf4j",
    "Log4j2",
];

/// Comments and string and character literals; a string template's embedded expressions are code.
static NON_CODE: super::NonCode = super::NonCode {
    language: &JAVA_LANGUAGE,
    prose: &["line_comment", "block_comment"],
    strings: &["string_literal", "character_literal"],
    code: &["string_interpolation"],
    keep: super::keep_no_string,
    declared: super::declares_nothing,
};

impl LanguageParser for JavaParser {
    fn non_code(&self) -> Option<&'static super::NonCode> {
        Some(&NON_CODE)
    }

    fn extract_refs(&self, content: &str, defined: &[ParsedSymbol]) -> Result<Vec<ParsedRef>> {
        self.extract_refs_for_lang(content, defined, FileType::Java)
    }

    fn extract_refs_for_lang(
        &self,
        content: &str,
        defined: &[ParsedSymbol],
        file_type: FileType,
    ) -> Result<Vec<ParsedRef>> {
        let tree = parse_tree(content, &JAVA_LANGUAGE)?;
        java_refs(content, &tree, defined, file_type)
    }

    fn parse_symbols_and_refs(
        &self,
        content: &str,
        file_type: FileType,
    ) -> Result<(Vec<ParsedSymbol>, Vec<ParsedRef>)> {
        super::forget_last_tree();
        let symbols = self.parse_symbols(content)?;
        let tree = match super::reuse_tree(content, &JAVA_LANGUAGE) {
            Some(tree) => tree,
            None => parse_tree(content, &JAVA_LANGUAGE)?,
        };
        let refs = java_refs(content, &tree, &symbols, file_type)?;
        Ok((symbols, refs))
    }

    fn parse_symbols(&self, content: &str) -> Result<Vec<ParsedSymbol>> {
        let tree = parse_tree(content, &JAVA_LANGUAGE)?;
        let mut symbols = Vec::new();
        let query = &*JAVA_QUERY;
        let mut cursor = QueryCursor::new();

        let capture_names = query.capture_names();
        let idx = |name: &str| -> Option<u32> {
            capture_names
                .iter()
                .position(|n| *n == name)
                .map(|i| i as u32)
        };

        let idx_class_name = idx("class_name");
        let idx_class_node = idx("class_node");
        let idx_interface_name = idx("interface_name");
        let idx_interface_node = idx("interface_node");
        let idx_enum_name = idx("enum_name");
        let idx_enum_node = idx("enum_node");
        let idx_enum_constant_name = idx("enum_constant_name");
        let idx_method_name = idx("method_name");
        let idx_method_node = idx("method_node");
        let idx_constructor_name = idx("constructor_name");
        let idx_constructor_node = idx("constructor_node");
        let idx_field_name = idx("field_name");
        let idx_field_node = idx("field_node");
        let idx_record_component_name = idx("record_component_name");
        let idx_record_component_node = idx("record_component_node");
        let idx_annotation_name = idx("annotation_name");
        let idx_annotation_call_name = idx("annotation_call_name");
        let idx_definition = idx("definition");
        let idx_import_node = idx("import_node");

        // Distinct declarations may share a line (including overloads and constructors).
        let mut emitted: std::collections::HashSet<(String, usize)> =
            std::collections::HashSet::new();
        let mut explicit_methods: std::collections::HashSet<(usize, String)> =
            std::collections::HashSet::new();
        let mut pending_record_accessors = Vec::new();

        let mut matches = cursor.matches(query, tree.root_node(), content.as_bytes());

        while let Some(m) = matches.next() {
            let end_line = find_capture(m, idx_definition).map(|c| text_end_line(content, &c.node));

            if let Some(import) = find_capture(m, idx_import_node) {
                let text = node_text(content, &import.node);
                if !text.trim_end_matches(';').trim_end().ends_with('*') {
                    let mut identifiers = Vec::new();
                    super::walk_tree_preorder(&import.node, |node| {
                        if node.kind() == "identifier" {
                            identifiers.push(node);
                        }
                        super::WalkControl::Continue
                    });
                    if let Some(name_node) = identifiers.last() {
                        symbols.push(ParsedSymbol {
                            name: node_text(content, name_node).to_string(),
                            kind: SymbolKind::Import,
                            line: node_line(name_node),
                            signature: text.trim().to_string(),
                            parents: vec![],
                            end_line,
                        });
                    }
                }
                continue;
            }

            // === Classes ===
            if let Some(name_cap) = find_capture(m, idx_class_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                    let parents = find_capture(m, idx_class_node)
                        .map(|n| extract_class_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Class,
                        line,
                        signature: signature_line(content, line),
                        parents,
                        end_line,
                    });
                }
                continue;
            }

            // === Interfaces ===
            if let Some(name_cap) = find_capture(m, idx_interface_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                    let parents = find_capture(m, idx_interface_node)
                        .map(|n| extract_interface_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Interface,
                        line,
                        signature: signature_line(content, line),
                        parents,
                        end_line,
                    });
                }
                continue;
            }

            // === Enums ===
            if let Some(name_cap) = find_capture(m, idx_enum_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                    let parents = find_capture(m, idx_enum_node)
                        .map(|n| extract_enum_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Enum,
                        line,
                        signature: signature_line(content, line),
                        parents,
                        end_line,
                    });
                }
                continue;
            }

            // === Enum constants ===
            if let Some(name_cap) = find_capture(m, idx_enum_constant_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Constant,
                        line,
                        signature: signature_line(content, line),
                        parents: vec![],
                        end_line,
                    });
                }
                continue;
            }

            // === Methods (only inside class/interface/enum body) ===
            if let Some(name_cap) = find_capture(m, idx_method_name) {
                if let Some(node_cap) = find_capture(m, idx_method_node) {
                    if is_inside_type_body(&node_cap.node) {
                        let name = node_text(content, &name_cap.node);
                        // Only a no-argument method replaces a record's implicit accessor.
                        if node_cap
                            .node
                            .child_by_field_name("parameters")
                            .is_some_and(|parameters| parameters.named_child_count() == 0)
                        {
                            if let Some(owner) = enclosing_type_site(&node_cap.node) {
                                explicit_methods.insert((owner, name.to_string()));
                            }
                        }
                        let line = node_line(&name_cap.node);
                        if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                            symbols.push(ParsedSymbol {
                                name: name.to_string(),
                                kind: SymbolKind::Function,
                                line,
                                signature: signature_line(content, line),
                                parents: vec![],
                                end_line,
                            });
                        }
                    }
                }
                continue;
            }

            // === Constructors ===
            if let Some(name_cap) = find_capture(m, idx_constructor_name) {
                if let Some(node_cap) = find_capture(m, idx_constructor_node) {
                    if is_inside_type_body(&node_cap.node) {
                        let name = node_text(content, &name_cap.node);
                        let line = node_line(&name_cap.node);
                        if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                            symbols.push(ParsedSymbol {
                                name: name.to_string(),
                                kind: SymbolKind::Function,
                                line,
                                signature: signature_line(content, line),
                                parents: vec![],
                                end_line,
                            });
                        }
                    }
                }
                continue;
            }

            // === Fields (only inside class/enum body) ===
            if let Some(name_cap) = find_capture(m, idx_field_name) {
                if let Some(node_cap) = find_capture(m, idx_field_node) {
                    if is_inside_type_body(&node_cap.node) {
                        let name = node_text(content, &name_cap.node);
                        let line = node_line(&name_cap.node);
                        if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                            symbols.push(ParsedSymbol {
                                name: name.to_string(),
                                kind: SymbolKind::Property,
                                line,
                                signature: signature_line(content, line),
                                parents: vec![],
                                end_line,
                            });
                        }
                    }
                }
                continue;
            }

            // === Record components (header parameters in record declarations) ===
            if let Some(name_cap) = find_capture(m, idx_record_component_name) {
                if let Some(node_cap) = find_capture(m, idx_record_component_node) {
                    let name = node_text(content, &name_cap.node);
                    let line = node_line(&name_cap.node);
                    let component_signature = node_text(content, &node_cap.node).trim().to_string();
                    let owner = enclosing_type_site(&node_cap.node).unwrap_or_default();

                    // Record components are class-like fields
                    if emitted.insert((name.to_string(), name_cap.node.start_byte())) {
                        symbols.push(ParsedSymbol {
                            name: name.to_string(),
                            kind: SymbolKind::Property,
                            line,
                            signature: component_signature,
                            parents: vec![],
                            end_line,
                        });
                    }

                    let accessor_signature =
                        record_component_accessor_signature(content, &node_cap.node, name);

                    // Emit synthetic accessors after we know explicit methods in the same type.
                    pending_record_accessors.push((
                        owner,
                        name.to_string(),
                        line,
                        name_cap.node.start_byte(),
                        end_line,
                        accessor_signature,
                    ));
                }
                continue;
            }

            // === Marker annotations (no arguments) ===
            if let Some(name_cap) = find_capture(m, idx_annotation_name) {
                let full_name = node_text(content, &name_cap.node);
                let name = full_name.rsplit('.').next().unwrap_or(full_name).trim();
                if SIGNIFICANT_ANNOTATIONS.contains(&name) {
                    let line = node_line(&name_cap.node);
                    if emitted.insert((format!("@{}", name), name_cap.node.start_byte())) {
                        symbols.push(ParsedSymbol {
                            name: format!("@{}", name),
                            kind: SymbolKind::Annotation,
                            line,
                            signature: signature_line(content, line),
                            parents: vec![],
                            end_line,
                        });
                    }
                }
                continue;
            }

            // === Annotations with arguments ===
            if let Some(name_cap) = find_capture(m, idx_annotation_call_name) {
                let full_name = node_text(content, &name_cap.node);
                let name = full_name.rsplit('.').next().unwrap_or(full_name).trim();
                if SIGNIFICANT_ANNOTATIONS.contains(&name) {
                    let line = node_line(&name_cap.node);
                    if emitted.insert((format!("@{}", name), name_cap.node.start_byte())) {
                        symbols.push(ParsedSymbol {
                            name: format!("@{}", name),
                            kind: SymbolKind::Annotation,
                            line,
                            signature: signature_line(content, line),
                            parents: vec![],
                            end_line,
                        });
                    }
                }
                continue;
            }
        }

        // Java records synthesize public accessor methods for components unless explicitly overridden.
        for (owner, name, line, byte, end_line, signature) in pending_record_accessors {
            if explicit_methods.contains(&(owner, name.clone())) {
                continue;
            }
            if emitted.insert((format!("{}#record_accessor", name), byte)) {
                symbols.push(ParsedSymbol {
                    name: name.to_string(),
                    kind: SymbolKind::Function,
                    line,
                    signature,
                    parents: vec![],
                    end_line,
                });
            }
        }

        Ok(symbols)
    }
}

/// Extract Java reference positions from syntax, without skipping a whole declaration line.
fn java_refs(
    content: &str,
    tree: &tree_sitter::Tree,
    _defined: &[ParsedSymbol],
    _file_type: FileType,
) -> Result<Vec<ParsedRef>> {
    let mut refs = Vec::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        if matches!(
            node.kind(),
            "package_declaration"
                | "import_declaration"
                | "line_comment"
                | "block_comment"
                | "character_literal"
        ) {
            return super::WalkControl::SkipChildren;
        }
        if matches!(node.kind(), "identifier" | "type_identifier") {
            let declaration = node.parent().is_some_and(|parent| {
                matches!(
                    parent.kind(),
                    "class_declaration"
                        | "interface_declaration"
                        | "enum_declaration"
                        | "record_declaration"
                        | "annotation_type_declaration"
                        | "method_declaration"
                        | "constructor_declaration"
                        | "compact_constructor_declaration"
                        | "variable_declarator"
                        | "formal_parameter"
                        | "spread_parameter"
                        | "catch_formal_parameter"
                        | "type_parameter"
                        | "enum_constant"
                        | "annotation_type_element_declaration"
                        | "enhanced_for_statement"
                        | "instanceof_expression"
                        | "type_pattern"
                ) && parent
                    .child_by_field_name("name")
                    .is_some_and(|name| name.id() == node.id())
            });
            let lambda_binding = node.parent().is_some_and(|parent| {
                parent.kind() == "inferred_parameters"
                    || (parent.kind() == "lambda_expression"
                        && parent
                            .child_by_field_name("parameters")
                            .is_some_and(|parameters| parameters.id() == node.id()))
            });
            if !declaration && !lambda_binding {
                let line = node_line(&node);
                refs.push(ParsedRef {
                    name: node_text(content, &node).to_string(),
                    line,
                    context: crate::parsers::truncate_context(
                        super::line_text(content, line).trim(),
                    ),
                });
            }
        }
        super::WalkControl::Continue
    });
    Ok(refs)
}

/// Find Java invocation identifier lines, excluding declarations and method references.
pub fn invocation_lines(content: &str, name: &str) -> Result<std::collections::HashSet<usize>> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let mut lines = std::collections::HashSet::new();
    super::walk_tree_preorder(&tree.root_node(), |node| {
        let identifier = invocation_identifier(node);
        if let Some(identifier) = identifier {
            if node_text(content, &identifier) == name {
                lines.insert(node_line(&identifier));
            }
        }
        super::WalkControl::Continue
    });
    Ok(lines)
}

fn invocation_identifier<'tree>(
    node: tree_sitter::Node<'tree>,
) -> Option<tree_sitter::Node<'tree>> {
    match node.kind() {
        "method_invocation" => node.child_by_field_name("name"),
        "object_creation_expression" => node.child_by_field_name("type").map(|mut ty| {
            if ty.kind() == "generic_type" {
                ty = ty.named_child(0).unwrap_or(ty);
            }
            if ty.kind() == "scoped_type_identifier" {
                ty = ty.child_by_field_name("name").unwrap_or(ty);
            }
            ty
        }),
        _ => None,
    }
}

/// Attribute actual invocations to syntax owners, retaining distinct overloads.
#[cfg(test)]
pub(crate) fn invocation_callers(
    content: &str,
    names: &[String],
    limit: usize,
) -> Result<Vec<Vec<(String, usize)>>> {
    Ok(invocation_caller_sites(content, names, limit)?
        .into_iter()
        .map(|owners| {
            owners
                .into_iter()
                .map(|owner| (owner.name, owner.line))
                .collect()
        })
        .collect())
}

#[derive(Clone, PartialEq, Eq)]
pub(crate) struct InvocationCaller {
    pub name: String,
    pub line: usize,
    pub callable: bool,
}

/// Initializers own calls but cannot themselves be called as methods.
pub(crate) fn invocation_caller_sites(
    content: &str,
    names: &[String],
    limit: usize,
) -> Result<Vec<Vec<InvocationCaller>>> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let lookup: HashMap<&str, usize> = names
        .iter()
        .enumerate()
        .map(|(i, name)| (name.as_str(), i))
        .collect();
    let mut callers = vec![Vec::new(); names.len()];
    super::walk_tree_preorder(&tree.root_node(), |node| {
        let Some(identifier) = invocation_identifier(node) else {
            return super::WalkControl::Continue;
        };
        let bare = node_text(content, &identifier);
        let qualified = match node.kind() {
            "method_invocation" => node
                .child_by_field_name("object")
                .map(|object| format!("{}.{}", node_text(content, &object), bare)),
            "object_creation_expression" => node
                .child_by_field_name("type")
                .map(|ty| node_text(content, &ty).to_string()),
            _ => None,
        };
        let indices: Vec<usize> = [
            lookup.get(bare),
            qualified.as_deref().and_then(|name| lookup.get(name)),
        ]
        .into_iter()
        .flatten()
        .copied()
        .collect();
        if indices.is_empty() {
            return super::WalkControl::Continue;
        }
        let mut parent = node.parent();
        while let Some(owner) = parent {
            if matches!(
                owner.kind(),
                "method_declaration"
                    | "constructor_declaration"
                    | "compact_constructor_declaration"
                    | "variable_declarator"
                    | "class_declaration"
                    | "interface_declaration"
                    | "enum_declaration"
                    | "record_declaration"
                    | "annotation_type_declaration"
            ) && (owner.kind() != "variable_declarator"
                || owner
                    .parent()
                    .is_some_and(|parent| parent.kind() == "field_declaration"))
            {
                if let Some(name) = owner.child_by_field_name("name") {
                    let site = InvocationCaller {
                        name: node_text(content, &name).to_string(),
                        line: node_line(&name),
                        callable: matches!(
                            owner.kind(),
                            "method_declaration"
                                | "constructor_declaration"
                                | "compact_constructor_declaration"
                        ),
                    };
                    for index in &indices {
                        if callers[*index].len() < limit && !callers[*index].contains(&site) {
                            callers[*index].push(site.clone());
                        }
                    }
                    break;
                }
            }
            parent = owner.parent();
        }
        super::WalkControl::Continue
    });
    Ok(callers)
}

#[cfg(test)]
mod invocation_owner_tests {
    #[test]
    fn local_variables_are_not_java_callers() {
        let source = "class Probe {\n\
            int leaf() { return 1; }\n\
            int localOwner() { int result = leaf(); return result; }\n\
            void lambdaOwner() { Runnable action = () -> { int result = leaf(); }; }\n\
            int field = leaf();\n\
            }\n";
        let callers = super::invocation_callers(source, &["leaf".into()], 10).unwrap();
        assert_eq!(
            callers,
            vec![vec![
                ("localOwner".into(), 3),
                ("lambdaOwner".into(), 4),
                ("field".into(), 5),
            ]]
        );
    }
}

type QualifiedNameOccurrences = HashMap<(String, usize, String), VecDeque<Option<String>>>;

pub fn collect_qualified_names(content: &str) -> Result<HashMap<(String, usize, String), String>> {
    Ok(collect_qualified_name_occurrences(content)?
        .into_iter()
        .filter_map(|(key, values)| {
            values
                .into_iter()
                .flatten()
                .next_back()
                .map(|value| (key, value))
        })
        .collect())
}

/// Keep declaration occurrences in parser order, including local declarations without an FQN.
pub fn collect_qualified_name_occurrences(content: &str) -> Result<QualifiedNameOccurrences> {
    let tree = parse_tree(content, &JAVA_LANGUAGE)?;
    let root = tree.root_node();
    let mut root_cursor = root.walk();
    let package = root
        .named_children(&mut root_cursor)
        .find(|node| node.kind() == "package_declaration")
        .and_then(|node| {
            let mut cursor = node.walk();
            let name = node
                .named_children(&mut cursor)
                .find(|child| matches!(child.kind(), "identifier" | "scoped_identifier"));
            name
        })
        .map(|node| node_text(content, &node).to_string());
    let query = &*JAVA_QUERY;
    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(query, root, content.as_bytes());
    let mut names: QualifiedNameOccurrences = HashMap::new();
    let mut accessors = Vec::new();
    let mut explicit_accessors = std::collections::HashSet::new();
    while let Some(m) = matches.next() {
        for capture in m.captures {
            let kinds: &[SymbolKind] = match query.capture_names()[capture.index as usize] {
                "class_name" => &[SymbolKind::Class],
                "interface_name" => &[SymbolKind::Interface],
                "enum_name" => &[SymbolKind::Enum],
                "enum_constant_name" => &[SymbolKind::Constant],
                "method_name" | "constructor_name" => &[SymbolKind::Function],
                "field_name" => &[SymbolKind::Property],
                "record_component_name" => &[SymbolKind::Property],
                _ => continue,
            };
            let name = node_text(content, &capture.node);
            let mut ancestors = Vec::new();
            let mut node = capture.node.parent();
            let mut is_local = false;
            while let Some(parent) = node {
                match parent.kind() {
                    "class_declaration"
                    | "interface_declaration"
                    | "annotation_type_declaration"
                    | "enum_declaration"
                    | "record_declaration" => {
                        if let Some(owner) = parent.child_by_field_name("name") {
                            // The captured type's own name is already the final segment.
                            if owner.id() != capture.node.id() {
                                ancestors.push(node_text(content, &owner).to_string());
                            }
                        }
                    }
                    "method_declaration"
                    | "constructor_declaration"
                    | "compact_constructor_declaration" => {
                        // Every callable other than this declaration encloses a
                        // local or anonymous type, which has no Java FQN.
                        if parent.child_by_field_name("name").map(|owner| owner.id())
                            != Some(capture.node.id())
                        {
                            is_local = true;
                        }
                    }
                    "object_creation_expression" => is_local = true,
                    "enum_constant" => {
                        // A constant-specific body is an anonymous subclass,
                        // but the constant itself keeps the named enum owner.
                        if parent.child_by_field_name("name").map(|owner| owner.id())
                            != Some(capture.node.id())
                        {
                            is_local = true;
                        }
                    }
                    _ => {}
                }
                node = parent.parent();
            }
            ancestors.reverse();
            if let Some(package) = &package {
                ancestors.insert(0, package.clone());
            }
            ancestors.push(name.to_string());
            let qualified = (!is_local).then(|| ancestors.join("."));
            let key = (
                kinds[0].as_str().to_string(),
                node_line(&capture.node),
                name.to_string(),
            );
            names.entry(key).or_default().push_back(qualified.clone());
            if query.capture_names()[capture.index as usize] == "record_component_name" {
                let owner = enclosing_type_site(&capture.node).unwrap_or_default();
                accessors.push((owner, name.to_string(), node_line(&capture.node), qualified));
            } else if query.capture_names()[capture.index as usize] == "method_name" {
                if let Some(method) = capture.node.parent() {
                    if method
                        .child_by_field_name("parameters")
                        .is_some_and(|parameters| parameters.named_child_count() == 0)
                    {
                        if let Some(owner) = enclosing_type_site(&method) {
                            explicit_accessors.insert((owner, name.to_string()));
                        }
                    }
                }
            }
        }
    }
    for (owner, name, line, qualified) in accessors {
        if !explicit_accessors.contains(&(owner, name.clone())) {
            names
                .entry(("function".to_string(), line, name))
                .or_default()
                .push_back(qualified);
        }
    }
    Ok(names)
}

/// Check if a node is inside a class/interface/enum/record body
fn is_inside_type_body(node: &tree_sitter::Node) -> bool {
    node.parent()
        .map(|p| {
            matches!(
                p.kind(),
                "class_body"
                    | "interface_body"
                    | "annotation_type_body"
                    | "enum_body"
                    | "enum_body_declarations"
                    | "record_body"
            )
        })
        .unwrap_or(false)
}

/// Build synthetic accessor signature for a record component (e.g. `String id()`).
fn record_component_accessor_signature(
    content: &str,
    component_node: &tree_sitter::Node,
    name: &str,
) -> String {
    if let Some(type_node) = parameter_type(*component_node) {
        let mut type_text = node_text(content, &type_node).trim().to_string();
        if let Some(dim_node) = component_node.child_by_field_name("dimensions") {
            type_text.push_str(node_text(content, &dim_node).trim());
        }
        if component_node.kind() == "spread_parameter" {
            type_text.push_str("[]");
        }
        return format!("{} {}()", type_text, name);
    }
    format!("{}()", name)
}

/// Identify the declaring type even when local types share a name and line.
fn enclosing_type_site(node: &tree_sitter::Node) -> Option<usize> {
    let mut cur = Some(*node);
    while let Some(n) = cur {
        if matches!(
            n.kind(),
            "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
        ) {
            if let Some(name_node) = n.child_by_field_name("name") {
                return Some(name_node.start_byte());
            }
        }
        cur = n.parent();
    }
    None
}

/// Extract parent types from a class_declaration (extends + implements)
fn extract_class_parents(content: &str, class_node: &tree_sitter::Node) -> Vec<(String, String)> {
    let mut parents = Vec::new();
    let mut cursor = class_node.walk();

    for child in class_node.children(&mut cursor) {
        match child.kind() {
            "superclass" => {
                // superclass -> "extends" type_identifier/generic_type
                if let Some(name) = extract_type_from_parent_node(&child, content) {
                    parents.push((name, "extends".to_string()));
                }
            }
            "super_interfaces" => {
                // super_interfaces -> "implements" type_list -> type_identifier+
                extract_type_list(&child, content, "implements", &mut parents);
            }
            _ => {}
        }
    }

    parents
}

/// Extract parent types from an interface_declaration (extends)
fn extract_interface_parents(
    content: &str,
    iface_node: &tree_sitter::Node,
) -> Vec<(String, String)> {
    let mut parents = Vec::new();
    let mut cursor = iface_node.walk();

    for child in iface_node.children(&mut cursor) {
        if child.kind() == "extends_interfaces" {
            extract_type_list(&child, content, "extends", &mut parents);
        }
    }

    parents
}

/// Extract parent types from an enum_declaration (implements)
fn extract_enum_parents(content: &str, enum_node: &tree_sitter::Node) -> Vec<(String, String)> {
    let mut parents = Vec::new();
    let mut cursor = enum_node.walk();

    for child in enum_node.children(&mut cursor) {
        if child.kind() == "super_interfaces" {
            extract_type_list(&child, content, "implements", &mut parents);
        }
    }

    parents
}

/// Extract a single type name from a superclass node
fn extract_type_from_parent_node(node: &tree_sitter::Node, content: &str) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            "type_identifier" => {
                return Some(node_text(content, &child).to_string());
            }
            "generic_type" => {
                // generic_type -> type_identifier type_arguments
                if let Some(first) = child.named_child(0) {
                    if first.kind() == "type_identifier" {
                        return Some(node_text(content, &first).to_string());
                    }
                }
            }
            "scoped_type_identifier" => {
                // Get the last identifier (e.g., com.example.MyClass -> MyClass)
                let text = node_text(content, &child);
                if let Some(last) = text.rsplit('.').next() {
                    return Some(last.to_string());
                }
            }
            _ => {}
        }
    }
    None
}

/// Extract types from a type_list (used in super_interfaces, extends_interfaces)
fn extract_type_list(
    node: &tree_sitter::Node,
    content: &str,
    inherit_kind: &str,
    parents: &mut Vec<(String, String)>,
) {
    let mut stack = vec![*node];

    while let Some(node) = stack.pop() {
        let mut cursor = node.walk();
        let mut children: Vec<tree_sitter::Node> = node.children(&mut cursor).collect();
        children.reverse();

        for child in children {
            match child.kind() {
                "type_list" => stack.push(child),
                "type_identifier" => {
                    let name = node_text(content, &child);
                    parents.push((name.to_string(), inherit_kind.to_string()));
                }
                "generic_type" => {
                    if let Some(first) = child.named_child(0) {
                        if first.kind() == "type_identifier" {
                            let name = node_text(content, &first);
                            parents.push((name.to_string(), inherit_kind.to_string()));
                        }
                    }
                }
                "scoped_type_identifier" => {
                    let text = node_text(content, &child);
                    if let Some(last) = text.rsplit('.').next() {
                        parents.push((last.to_string(), inherit_kind.to_string()));
                    }
                }
                _ => {}
            }
        }
    }
}

/// Find a capture by index in a match
fn find_capture<'a>(
    m: &'a tree_sitter::QueryMatch<'a, 'a>,
    idx: Option<u32>,
) -> Option<&'a tree_sitter::QueryCapture<'a>> {
    let idx = idx?;
    m.captures.iter().find(|c| c.index == idx)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dependency_annotation_declarations_have_type_identities() {
        let source = r#"package fixture;
public @interface Mark { class Nested {} }
"#;
        let symbols = JAVA_PARSER.parse_symbols(source).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "Mark" && s.kind == SymbolKind::Interface));
        let names = collect_qualified_names(source).unwrap();
        assert_eq!(
            names.get(&("interface".into(), 2, "Mark".into())),
            Some(&"fixture.Mark".into())
        );
        assert_eq!(
            names.get(&("class".into(), 2, "Nested".into())),
            Some(&"fixture.Mark.Nested".into())
        );
    }

    #[test]
    fn dependency_uses_keep_subclass_body_contexts_separate_from_headers_and_siblings() {
        let source = r#"package fixture;
import static base.Parent.Guarded;
class Child extends base.Parent {
    Guarded field;
    class Inner { Guarded field; int value=SECRET+secret(); }
    void local() { class Local { Guarded field; } }
}
class Peer { Guarded field; int value=SECRET+secret(); }
"#;
        let syntax = dependency_syntax(source).unwrap();
        assert!(syntax
            .type_uses
            .contains(&("base.Parent".into(), false, vec![])));
        let local = format!(
            "fixture.Child.Local@{}",
            source.find("class Local").unwrap()
        );
        for owners in [
            vec!["fixture.Child"],
            vec!["fixture.Child.Inner", "fixture.Child"],
            vec![local.as_str(), "fixture.Child"],
            vec!["fixture.Peer"],
        ] {
            assert!(syntax.type_uses.contains(&(
                "Guarded".into(),
                false,
                owners.into_iter().map(str::to_owned).collect()
            )));
        }
        assert!(syntax
            .member_uses
            .contains(&("secret".into(), true, vec!["fixture.Peer".into()])));
        assert!(!syntax
            .type_uses
            .iter()
            .any(|(name, _, _)| name == "base.Parent.Guarded"));
        let declaration = dependency_import_declaration(
            "package base; public class Parent { protected static class Guarded {} protected static int SECRET; }",
            "base.Parent.Guarded", "fixture").unwrap().unwrap();
        assert!(!declaration.accessible);
        assert!(declaration.protected_member);
        assert_eq!(
            declaration.access_barriers,
            vec![("base.Parent".into(), true)]
        );
        let declaration = dependency_import_declaration(
            "package base; public class Parent { protected static int SECRET; }",
            "base.Parent",
            "fixture",
        )
        .unwrap()
        .unwrap();
        assert!(declaration
            .protected_names
            .contains(&("SECRET".into(), false)));
        assert!(!declaration.static_names.contains(&("SECRET".into(), false)));
    }

    #[test]
    fn nested_array_slots_retain_rank_and_method_variable_ownership() {
        let source = r#"package fixture;
        class Carrier<T> {}
        class Box<T> {
            T fixed(Carrier<T[][]> value) { return null; }
            <T> T inferred(Carrier<Carrier<T[]>> value) { return null; }
            shared.Child primitive(Carrier<int[]> value) { return null; }
        }"#;
        let declaration = dependency_import_declaration(source, "fixture.Box", "fixture")
            .unwrap()
            .unwrap();
        let fixed = &declaration.value_types[&("fixed".into(), true)][0];
        assert!(matches!(&fixed.parameter_variables[0],
            Some((DependencyResultType::Named(owner, arguments), 0)) if owner == "Carrier"
            && matches!(&arguments[0], Some(DependencyResultType::Array(element, 2))
                if matches!(element.as_ref(), DependencyResultType::Parameter {owner, name}
                    if owner == "fixture.Box" && name == "T"))));
        let method = declaration.value_types[&("inferred".into(), true)][0]
            .method_signature
            .as_ref()
            .unwrap();
        assert!(matches!(&method.parameters[0],
            Some((DependencyResultType::Named(_, arguments), 0))
            if matches!(&arguments[0], Some(DependencyResultType::Named(_, slots))
                if matches!(&slots[0], Some(DependencyResultType::Array(element, 1))
                    if matches!(element.as_ref(), DependencyResultType::Parameter {owner, name}
                        if owner == "@method" && name == "T")))));
        let primitive = &declaration.value_types[&("primitive".into(), true)][0];
        assert!(matches!(&primitive.parameter_variables[0],
            Some((DependencyResultType::Named(_, arguments), 0))
            if matches!(&arguments[0], Some(DependencyResultType::Array(element, 1))
                if matches!(element.as_ref(), DependencyResultType::Named(name, slots)
                    if name == "int" && slots.is_empty()))));
    }

    #[test]
    fn dependency_array_results_keep_rank_and_separate_scalar_metadata() {
        let source = r#"package fixture;
        class Box<T> {
            T[] generic(){return null;}
            shared.Child nominal()[][]{return null;}
            T[] left[], right;
            <U> U[] inferred(U value){return null;}
        }
        record Spread(shared.Child... values) {}"#;
        let declaration = dependency_import_declaration(source, "fixture.Box", "fixture")
            .unwrap()
            .unwrap();
        for name in ["generic", "nominal", "inferred"] {
            let method = &declaration.value_types[&(name.into(), true)][0];
            assert!(method.result_type.is_none());
            assert!(method.path.is_none());
        }
        assert!(
            matches!(&declaration.value_types[&("generic".into(), true)][0].array_result_type,
            Some(DependencyResultType::Array(element, 1))
                if matches!(element.as_ref(), DependencyResultType::Parameter { owner, name }
                    if owner == "fixture.Box" && name == "T"))
        );
        assert!(
            matches!(&declaration.value_types[&("nominal".into(), true)][0].array_result_type,
            Some(DependencyResultType::Array(element, 2))
                if matches!(element.as_ref(), DependencyResultType::Named(name, _) if name == "shared.Child"))
        );
        assert!(
            matches!(&declaration.value_types[&("left".into(), false)][0].array_result_type,
            Some(DependencyResultType::Array(element, 1)) if matches!(element.as_ref(), DependencyResultType::Array(_, 1)))
        );
        assert!(matches!(
            &declaration.value_types[&("right".into(), false)][0].array_result_type,
            Some(DependencyResultType::Array(_, 1))
        ));
        let method = declaration.value_types[&("inferred".into(), true)][0]
            .method_signature
            .as_ref()
            .unwrap();
        assert!(method.result.is_none());
        assert!(
            matches!(&method.array_result, Some(DependencyResultType::Array(element, 1))
            if matches!(element.as_ref(), DependencyResultType::Parameter { owner, name }
                if owner == "@method" && name == "U"))
        );
        let spread = dependency_import_declaration(source, "fixture.Spread", "fixture")
            .unwrap()
            .unwrap();
        for method in [false, true] {
            let value = &spread.value_types[&("values".into(), method)][0];
            assert!(value.result_type.is_none());
            assert!(matches!(
                value.array_result_type,
                Some(DependencyResultType::Array(_, 1))
            ));
        }
    }

    #[test]
    fn dependency_results_preserve_class_arguments_and_bound_ownership() {
        let source = r#"package fixture;
        class Box<A,T extends shared.Child> {
            T get() { return null; }
            Box<String,T> nested() { return null; }
            <T> T shadow() { return null; }
            T[] array() { return null; }
        }
        class Sibling<T extends other.Child> {}"#;
        let declaration = dependency_import_declaration(source, "fixture.Box", "fixture")
            .unwrap()
            .unwrap();
        assert_eq!(declaration.type_parameters, ["A", "T"]);
        assert!(matches!(declaration.type_parameter_bounds.get("T"),
            Some(DependencyResultType::Named(path, arguments)) if path == "shared.Child" && arguments.is_empty()));
        assert!(
            matches!(&declaration.value_types[&("get".into(), true)][0].result_type,
            Some(DependencyResultType::Parameter { owner, name }) if owner == "fixture.Box" && name == "T")
        );
        assert!(
            matches!(&declaration.value_types[&("nested".into(), true)][0].result_type,
            Some(DependencyResultType::Named(path, arguments)) if path == "Box" && arguments.len() == 2
                && matches!(&arguments[1], Some(DependencyResultType::Parameter { owner, name })
                    if owner == "fixture.Box" && name == "T"))
        );
        for name in ["shadow", "array"] {
            assert!(declaration.value_types[&(name.into(), true)][0]
                .result_type
                .is_none());
        }
    }

    #[test]
    fn dependency_class_formals_keep_variable_owners_and_array_dimensions() {
        let source = r#"package fixture;
        class Box<T> {
            T direct(T value) { return value; }
            T array(T[][] value) { return null; }
            T postfix(T value[]) { return null; }
            T spread(T... values) { return null; }
            <T> Object shadow(T value) { return value; }
            <T extends String> Object shadowArray(T[] value) { return value; }
        }"#;
        let declaration = dependency_import_declaration(source, "fixture.Box", "fixture")
            .unwrap()
            .unwrap();
        for (method, dimensions) in [("direct", 0), ("array", 2), ("postfix", 1), ("spread", 1)] {
            let signature = &declaration.value_types[&(method.into(), true)][0];
            assert!(matches!(&signature.parameter_variables[0],
                Some((DependencyResultType::Parameter { owner, name }, count))
                    if owner == "fixture.Box" && name == "T" && *count == dimensions));
        }
        let shadow = &declaration.value_types[&("shadow".into(), true)][0];
        assert!(shadow.parameter_variables[0].is_none());
        assert!(shadow.parameters.as_ref().unwrap()[0].is_none());
        let shadow_array = &declaration.value_types[&("shadowArray".into(), true)][0];
        assert!(shadow_array.parameter_variables[0].is_none());
        assert!(shadow_array.parameters.as_ref().unwrap()[0].is_none());
    }

    #[test]
    fn dependency_method_variables_retain_separate_constraints_and_nested_results() {
        let source = r#"package fixture; class Box<T> {
            <T extends shared.Child> Box<T> get(T[] values) { return null; }
            <U extends T> U bound(U value) { return value; }
            <U extends shared.Child & Runnable> U intersection(U value) { return value; }
            <U> U projected(Box<Box<U>> values[]) { return null; }
        }"#;
        let declaration = dependency_import_declaration(source, "fixture.Box", "fixture")
            .unwrap()
            .unwrap();
        let signature = &declaration.value_types[&("get".into(), true)][0];
        // Existing class metadata retains its safety boundary.
        assert!(signature.parameter_variables[0].is_none());
        let method = signature.method_signature.as_ref().unwrap();
        assert_eq!(method.parameters.len(), 1);
        assert!(
            matches!(&method.parameters[0], Some((DependencyResultType::Parameter { owner, name }, 1))
            if owner == "@method" && name == "T")
        );
        assert!(
            matches!(&method.variables[0].1, Some(DependencyResultType::Named(path, _)) if path == "shared.Child")
        );
        assert!(
            matches!(&method.result, Some(DependencyResultType::Named(path, arguments))
            if path == "Box" && matches!(&arguments[0], Some(DependencyResultType::Parameter { owner, name })
                if owner == "@method" && name == "T"))
        );
        let bound = declaration.value_types[&("bound".into(), true)][0]
            .method_signature
            .as_ref()
            .unwrap();
        assert!(
            matches!(&bound.variables[0].1, Some(DependencyResultType::Parameter { owner, name })
            if owner == "fixture.Box" && name == "T")
        );
        let intersection = &declaration.value_types[&("intersection".into(), true)][0];
        assert!(intersection.method_generic);
        assert!(intersection.method_signature.is_none());
        let projected = declaration.value_types[&("projected".into(), true)][0]
            .method_signature
            .as_ref()
            .unwrap();
        assert_eq!(projected.parameters.len(), 1);
        assert!(
            matches!(&projected.parameters[0], Some((DependencyResultType::Named(name, arguments), 1))
            if name == "Box" && matches!(&arguments[0], Some(DependencyResultType::Named(inner, slots))
                if inner == "Box" && matches!(&slots[0], Some(DependencyResultType::Parameter { owner, name })
                    if owner == "@method" && name == "U")))
        );
    }

    #[test]
    fn dependency_record_members_keep_implicit_accessors_and_private_fields() {
        let source = r#"package fixture; import other.Child;
public record Carrier(Child value, Child other, Child[] array, Child... spread) {
    public Child value(int n) { return value; }
    public Child other() { return other; }
}"#;
        let declaration = dependency_import_declaration(source, "fixture.Carrier", "consumer")
            .unwrap()
            .unwrap();
        for name in ["value", "other", "array", "spread"] {
            let method = (name.to_owned(), true);
            let field = (name.to_owned(), false);
            assert!(declaration.instance_names.contains(&method), "{name}");
            assert!(declaration.declared_names.contains(&field), "{name}");
            assert!(
                declaration.private_instance_names.contains(&field),
                "{name}"
            );
            assert!(!declaration.instance_names.contains(&field), "{name}");
            assert!(!declaration.static_names.contains(&method), "{name}");
            assert!(!declaration.package_names.contains(&method), "{name}");
        }
        let values = &declaration.value_types[&("value".into(), true)];
        assert_eq!(values.len(), 2);
        assert!(values.iter().any(|value| value.arity == Some(0)
            && value.path.as_deref() == Some("Child")
            && value.position == source.find("Child value").unwrap()));
        assert!(values.iter().any(|value| value.arity == Some(1)));
        let tree = parse_tree(source, &JAVA_LANGUAGE).unwrap();
        let position = source.find("Child value").unwrap();
        let ty = tree
            .root_node()
            .descendant_for_byte_range(position, position + 1)
            .unwrap();
        assert_eq!(
            dependency_contexts(ty, source, "fixture"),
            ["fixture.Carrier"]
        );
        // A user-declared accessor replaces only its zero-argument signature.
        assert_eq!(declaration.value_types[&("other".into(), true)].len(), 1);
        for name in ["array", "spread"] {
            assert!(declaration.value_types[&(name.into(), true)][0]
                .path
                .is_none());
            assert!(declaration.value_types[&(name.into(), false)][0]
                .path
                .is_none());
        }
    }

    #[test]
    fn dependency_instance_contexts_preserve_static_and_enclosing_boundaries() {
        let source = r#"package fixture;
class Use extends base.Parent {
    int read() { return OPEN; }
    static int blocked() { return OPEN; }
    class Inner { int read() { return INNER + OUTER; } }
    static class Nested extends base.Parent { int read() { return NESTED; } }
    static void local() { class Local { int read() { return LOCAL; } } }
    java.util.function.IntSupplier lambda() { return () -> LAMBDA; }
}
"#;
        let syntax = dependency_syntax(source).unwrap();
        let local = format!("fixture.Use.Local@{}", source.find("class Local").unwrap());
        let instances = |name: &str, owners: &[&str]| {
            syntax
                .instance_contexts
                .get(&(
                    name.to_owned(),
                    false,
                    owners.iter().map(|name| (*name).to_owned()).collect(),
                ))
                .unwrap()
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
        };
        // Dependency ownership counts a valid use even if another occurrence
        // of the same name in the same class is in a static method.
        assert_eq!(instances("OPEN", &["fixture.Use"]), ["fixture.Use"]);
        assert_eq!(
            instances("INNER", &["fixture.Use.Inner", "fixture.Use"]),
            ["fixture.Use", "fixture.Use.Inner"]
        );
        assert_eq!(
            instances("NESTED", &["fixture.Use.Nested", "fixture.Use"]),
            ["fixture.Use.Nested"]
        );
        assert_eq!(
            instances("LOCAL", &[local.as_str(), "fixture.Use"]),
            [local.as_str()]
        );
        assert_eq!(instances("LAMBDA", &["fixture.Use"]), ["fixture.Use"]);
        let declaration = dependency_import_declaration(
            "package base; public class Parent { public int OPEN; protected int HUSH; public int instance(){return 1;} public static int STATIC; }",
            "base.Parent", "fixture").unwrap().unwrap();
        assert!(declaration.instance_names.contains(&("OPEN".into(), false)));
        assert!(declaration
            .instance_names
            .contains(&("instance".into(), true)));
        assert!(declaration
            .protected_instance_names
            .contains(&("HUSH".into(), false)));
        assert_eq!(
            declaration.static_names,
            std::collections::HashSet::from([("STATIC".into(), false)])
        );
    }

    #[test]
    fn dependency_syntax_keeps_imports_types_and_literal_boundaries() {
        let source = r#"package fixture;
import alpha.Outer;
import static beta.Tools.*;
@alpha.Mark class Use<T> {
    Outer.Inner nested;
    java.util.List<beta.Widget[]> values;
    String text = "alpha.Noise"; // beta.Noise
}
"#;
        let syntax = dependency_syntax(source).unwrap();
        assert_eq!(syntax.package, "fixture");
        assert_eq!(
            syntax.imports,
            vec![("alpha.Outer".into(), false), ("beta.Tools.*".into(), true)]
        );
        assert_eq!(
            syntax.types.into_iter().collect::<Vec<_>>(),
            vec![
                "Outer.Inner",
                "String",
                "alpha.Mark",
                "beta.Widget",
                "java.util.List"
            ]
        );
        assert!(syntax.declarations.contains("Use"));
        assert!(syntax.declarations.contains("T"));
    }

    #[test]
    fn test_parse_class() {
        let content = "public class UserService {\n}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "UserService" && s.kind == SymbolKind::Class));
    }

    #[test]
    fn test_parse_class_with_extends() {
        let content =
            "public class UserController extends BaseController implements Serializable {\n}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        let cls = symbols.iter().find(|s| s.name == "UserController").unwrap();
        assert!(cls
            .parents
            .iter()
            .any(|(p, k)| p == "BaseController" && k == "extends"));
        assert!(cls
            .parents
            .iter()
            .any(|(p, k)| p == "Serializable" && k == "implements"));
    }

    #[test]
    fn test_parse_interface() {
        let content = "public interface UserRepository extends JpaRepository {\n    User findByName(String name);\n}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        let iface = symbols.iter().find(|s| s.name == "UserRepository").unwrap();
        assert_eq!(iface.kind, SymbolKind::Interface);
        assert!(iface
            .parents
            .iter()
            .any(|(p, k)| p == "JpaRepository" && k == "extends"));
    }

    #[test]
    fn test_parse_enum() {
        let content = "public enum Status {\n    ACTIVE,\n    INACTIVE;\n}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "Status" && s.kind == SymbolKind::Enum));
    }

    #[test]
    fn test_parse_methods() {
        let content = r#"public class UserService {
    public List<User> getUsers() { return null; }
    private void validate(User user) {}
    protected String format(String input) { return input; }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "getUsers" && s.kind == SymbolKind::Function));
        assert!(symbols
            .iter()
            .any(|s| s.name == "validate" && s.kind == SymbolKind::Function));
        assert!(symbols
            .iter()
            .any(|s| s.name == "format" && s.kind == SymbolKind::Function));
    }

    #[test]
    fn test_parse_constructor() {
        let content = r#"public class User {
    private String name;
    public User(String name) {
        this.name = name;
    }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "User" && s.kind == SymbolKind::Class));
        // Constructor is indexed as a function with the class name
        assert!(symbols.iter().filter(|s| s.name == "User").count() >= 2);
    }

    #[test]
    fn test_parse_fields() {
        let content = r#"public class Config {
    private String apiUrl;
    public static final int MAX_RETRIES = 3;
    protected List<String> items;
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "apiUrl" && s.kind == SymbolKind::Property));
        assert!(symbols
            .iter()
            .any(|s| s.name == "MAX_RETRIES" && s.kind == SymbolKind::Property));
        assert!(symbols
            .iter()
            .any(|s| s.name == "items" && s.kind == SymbolKind::Property));
    }

    #[test]
    fn test_parse_annotations() {
        let content = r#"@RestController
@RequestMapping("/api")
public class UserController {
    @GetMapping("/users")
    public List<User> getUsers() { return null; }

    @Override
    public String toString() { return ""; }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "@RestController" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "@RequestMapping" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "@GetMapping" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "@Override" && s.kind == SymbolKind::Annotation));
    }

    #[test]
    fn test_spring_service() {
        let content = r#"@Service
public class PaymentService {
    @Autowired
    private PaymentRepository repository;

    @Transactional
    public Payment processPayment(PaymentRequest request) {
        return repository.save(request.toPayment());
    }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "@Service" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "@Autowired" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "@Transactional" && s.kind == SymbolKind::Annotation));
        assert!(symbols
            .iter()
            .any(|s| s.name == "PaymentService" && s.kind == SymbolKind::Class));
        assert!(symbols
            .iter()
            .any(|s| s.name == "processPayment" && s.kind == SymbolKind::Function));
        assert!(symbols
            .iter()
            .any(|s| s.name == "repository" && s.kind == SymbolKind::Property));
    }

    #[test]
    fn test_comments_ignored() {
        let content =
            "// class FakeClass {}\npublic class RealClass {}\n/* void fakeMethod() {} */\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols.iter().any(|s| s.name == "RealClass"));
        assert!(!symbols.iter().any(|s| s.name == "FakeClass"));
        assert!(!symbols.iter().any(|s| s.name == "fakeMethod"));
    }

    #[test]
    fn test_nonsignificant_annotations_skipped() {
        let content = r#"@SuppressWarnings("unchecked")
public class Foo {
    @Deprecated
    public void bar() {}
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        // SuppressWarnings and Deprecated are not in SIGNIFICANT_ANNOTATIONS
        assert!(!symbols.iter().any(|s| s.name == "@SuppressWarnings"));
        assert!(!symbols.iter().any(|s| s.name == "@Deprecated"));
        // But class and method should still be indexed
        assert!(symbols
            .iter()
            .any(|s| s.name == "Foo" && s.kind == SymbolKind::Class));
        assert!(symbols
            .iter()
            .any(|s| s.name == "bar" && s.kind == SymbolKind::Function));
    }

    #[test]
    fn test_generic_class_inheritance() {
        let content = "public class UserRepo extends CrudRepository<User, Long> implements UserRepository {\n}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        let cls = symbols.iter().find(|s| s.name == "UserRepo").unwrap();
        assert!(cls
            .parents
            .iter()
            .any(|(p, k)| p == "CrudRepository" && k == "extends"));
        assert!(cls
            .parents
            .iter()
            .any(|(p, k)| p == "UserRepository" && k == "implements"));
    }

    #[test]
    fn test_parse_record() {
        let content = r#"public record UserDto(String id, String name) implements Serializable {
    public String displayName() { return name; }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        let rec = symbols.iter().find(|s| s.name == "UserDto").unwrap();
        assert_eq!(rec.kind, SymbolKind::Class);
        assert!(rec
            .parents
            .iter()
            .any(|(p, k)| p == "Serializable" && k == "implements"));
        assert!(symbols
            .iter()
            .any(|s| s.name == "displayName" && s.kind == SymbolKind::Function));
        assert!(symbols.iter().any(|s| s.name == "id"
            && s.kind == SymbolKind::Property
            && s.signature == "String id"));
        assert!(symbols.iter().any(|s| s.name == "name"
            && s.kind == SymbolKind::Property
            && s.signature == "String name"));
        assert!(symbols.iter().any(|s| s.name == "id"
            && s.kind == SymbolKind::Function
            && s.signature == "String id()"));
        assert!(symbols.iter().any(|s| s.name == "name"
            && s.kind == SymbolKind::Function
            && s.signature == "String name()"));
    }

    #[test]
    fn test_parse_empty_record() {
        let content = "public record Empty() {}\n";
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.name == "Empty" && s.kind == SymbolKind::Class));
        assert_eq!(
            symbols
                .iter()
                .filter(|s| s.kind == SymbolKind::Property)
                .count(),
            0
        );
        assert_eq!(
            symbols
                .iter()
                .filter(|s| s.kind == SymbolKind::Function)
                .count(),
            0
        );
    }

    #[test]
    fn test_record_accessor_override_does_not_duplicate_synthetic() {
        let content = r#"public record Foo(String name) {
    public String name() { return name.toUpperCase(); }
}
"#;
        let symbols = JAVA_PARSER.parse_symbols(content).unwrap();
        assert!(symbols.iter().any(|s| s.name == "name"
            && s.kind == SymbolKind::Property
            && s.signature == "String name"));
        assert_eq!(
            symbols
                .iter()
                .filter(|s| s.name == "name" && s.kind == SymbolKind::Function)
                .count(),
            1
        );
        assert!(symbols.iter().any(|s| s.name == "name"
            && s.kind == SymbolKind::Function
            && s.signature == "public String name() { return name.toUpperCase(); }"));
    }
}
