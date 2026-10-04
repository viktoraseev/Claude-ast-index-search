//! Conservative syntax evidence for Java graph resolution, not type inference.
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use anyhow::Result;
use tree_sitter::{Language, Node};

use crate::parsers::treesitter::{parse_tree, walk_tree_preorder, WalkControl};

static LANGUAGE: LazyLock<Language> = LazyLock::new(|| tree_sitter_java::LANGUAGE.into());

#[derive(Default)]
pub(super) struct JavaSource {
    pub package: String,
    pub imports: Vec<String>,
    pub static_imports: Vec<String>,
    /// Reference rows carry a line and name, not a byte position. Colliding
    /// paths or value/type uses on one line remain explicit negative evidence.
    types: HashMap<(i64, String), Option<String>>,
    /// Graph binding needs the full syntax path; legacy inheritance rows may
    /// contain only its short name.
    parents: HashMap<(String, i64), Vec<String>>,
    /// Callable identity, reference line and name; None means colliding or
    /// unsupported invocations. Never confidently choose the first on a line.
    invocations: HashMap<(String, i64, i64, String), Option<ParameterCall>>,
    direct_calls: HashMap<(String, i64, i64, String), Option<usize>>,
    bare_calls: HashMap<(String, i64, i64, String), Option<usize>>,
    parameters: HashMap<(String, i64), Option<(usize, bool)>>,
    returns: HashMap<(String, i64), Option<String>>,
    expressions: HashMap<(String, i64, i64, String), Option<ExpressionCall>>,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) enum JavaReceiver {
    Type(String),
    This,
    Invocation {
        receiver: Option<Box<JavaReceiver>>,
        name: String,
        line: i64,
        arguments: usize,
    },
    Unknown,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ExpressionCall {
    pub receiver: JavaReceiver,
    /// A method reference has no invocation argument list.
    pub arguments: Option<usize>,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ParameterCall {
    pub receiver: String,
    pub arguments: usize,
}

struct VariableBinding {
    position: usize,
    field: bool,
    declared: Option<String>,
}

type VariableScopes = HashMap<usize, HashMap<String, Vec<VariableBinding>>>;

/// Temporary per-file lexical inventory, discarded after deriving calls.
/// Index scopes once instead of rescanning every declaration for each call.
fn variable_scopes(root: Node<'_>, source: &str) -> VariableScopes {
    let mut scopes = VariableScopes::new();
    walk_tree_preorder(&root, |declaration| {
        let field = declaration.kind() == "field_declaration";
        if !field && declaration.kind() != "local_variable_declaration" {
            return WalkControl::Continue;
        }
        let Some(declared_type) = declaration.child_by_field_name("type") else {
            return WalkControl::Continue;
        };
        let mut scope = declaration.parent();
        let scope_id = loop {
            let Some(node) = scope else {
                return WalkControl::Continue;
            };
            let class_body = matches!(node.kind(), "class_body" | "interface_body" | "enum_body");
            let local_scope = matches!(
                node.kind(),
                "block" | "constructor_body" | "for_statement" | "switch_block"
            );
            if (field && class_body) || (!field && local_scope) {
                break node.id();
            }
            if !field && class_body {
                return WalkControl::Continue;
            }
            scope = node.parent();
        };
        let mut cursor = declaration.walk();
        for variable in declaration.named_children(&mut cursor) {
            if variable.kind() != "variable_declarator" {
                continue;
            }
            let Some(name) = variable.child_by_field_name("name") else {
                continue;
            };
            let mut cursor = variable.walk();
            let array_suffix = variable
                .named_children(&mut cursor)
                .any(|child| child.kind() == "dimensions");
            let declared = if array_suffix
                || text(declared_type, source) == "var"
                || declared_type.has_error()
            {
                None
            } else {
                type_name(declared_type, source)
            };
            scopes
                .entry(scope_id)
                .or_default()
                .entry(text(name, source).to_owned())
                .or_default()
                .push(VariableBinding {
                    position: name.start_byte(),
                    field,
                    declared,
                });
        }
        WalkControl::Continue
    });
    scopes
}

fn variable_type<'a>(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &'a VariableScopes,
) -> Option<Option<&'a str>> {
    let mut ancestor = call.parent();
    while let Some(node) = ancestor {
        if let Some(bindings) = scopes.get(&node.id()).and_then(|scope| scope.get(name)) {
            if let Some(binding) = bindings
                .iter()
                .filter(|binding| {
                    (!fields_only || binding.field)
                        && (binding.field || binding.position < call.start_byte())
                })
                .max_by_key(|binding| binding.position)
            {
                return Some(binding.declared.as_deref());
            }
        }
        // Outer/inherited instance fields need an explicit capture/hierarchy
        // contract. Never borrow an enclosing class's field by name alone.
        if matches!(node.kind(), "class_body" | "interface_body" | "enum_body") {
            break;
        }
        ancestor = node.parent();
    }
    None
}

fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}

fn type_name(node: Node<'_>, source: &str) -> Option<String> {
    if node.kind() == "array_type" {
        return None;
    }
    let mut parts = Vec::new();
    walk_tree_preorder(&node, |child| match child.kind() {
        "type_arguments" | "annotation" | "marker_annotation" => WalkControl::SkipChildren,
        "identifier" | "type_identifier" => {
            parts.push(text(child, source));
            WalkControl::Continue
        }
        _ => WalkControl::Continue,
    });
    (!parts.is_empty()).then(|| parts.join("::"))
}

fn callable(mut node: Node<'_>) -> Option<Node<'_>> {
    while let Some(parent) = node.parent() {
        if matches!(
            parent.kind(),
            "method_declaration" | "constructor_declaration"
        ) {
            return Some(parent);
        }
        // A lambda parameter can hide an outer binding; inference across its
        // boundary needs a separate lexical contract.
        if matches!(parent.kind(), "lambda_expression" | "class_body") {
            return None;
        }
        node = parent;
    }
    None
}

fn type_parameter(mut owner: Node<'_>, name: &str, source: &str) -> bool {
    loop {
        if let Some(parameters) = owner.child_by_field_name("type_parameters") {
            let mut cursor = parameters.walk();
            for parameter in parameters.named_children(&mut cursor) {
                if let Some(identifier) = parameter.named_child(0) {
                    if text(identifier, source) == name {
                        return true;
                    }
                }
            }
        }
        let Some(parent) = owner.parent() else {
            return false;
        };
        owner = parent;
    }
}

fn argument_count(node: Node<'_>) -> Option<usize> {
    let arguments = node.child_by_field_name("arguments")?;
    let mut cursor = arguments.walk();
    let count = arguments
        .named_children(&mut cursor)
        .filter(|argument| !argument.is_extra())
        .count();
    Some(count)
}

fn expression_receiver(
    node: Node<'_>,
    owner: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
    depth: usize,
) -> JavaReceiver {
    if depth >= 16 || node.has_error() {
        return JavaReceiver::Unknown;
    }
    let typed = |name: Option<String>| {
        name.filter(|name| {
            !type_parameter(owner, name.split("::").next().unwrap_or_default(), source)
        })
        .map(JavaReceiver::Type)
        .unwrap_or(JavaReceiver::Unknown)
    };
    match node.kind() {
        "this" => JavaReceiver::This,
        "identifier" => {
            if let Some(parameters) = owner.child_by_field_name("parameters") {
                let mut cursor = parameters.walk();
                for parameter in parameters.named_children(&mut cursor) {
                    if parameter
                        .child_by_field_name("name")
                        .is_some_and(|name| text(name, source) == text(node, source))
                    {
                        return typed(
                            parameter
                                .child_by_field_name("type")
                                .and_then(|ty| type_name(ty, source)),
                        );
                    }
                }
            }
            match variable_type(node, text(node, source), false, scopes) {
                Some(binding) => typed(binding.map(str::to_owned)),
                None => typed(Some(text(node, source).to_owned())),
            }
        }
        "type_identifier" | "scoped_type_identifier" | "generic_type" => {
            typed(type_name(node, source))
        }
        "field_access"
            if node
                .child_by_field_name("object")
                .is_some_and(|base| base.kind() == "this") =>
        {
            let binding = node
                .child_by_field_name("field")
                .and_then(|field| variable_type(node, text(field, source), true, scopes))
                .flatten();
            typed(binding.map(str::to_owned))
        }
        "object_creation_expression" | "cast_expression" => typed(
            node.child_by_field_name("type")
                .and_then(|ty| type_name(ty, source)),
        ),
        "parenthesized_expression" => node
            .named_child(0)
            .map(|child| expression_receiver(child, owner, source, scopes, depth + 1))
            .unwrap_or(JavaReceiver::Unknown),
        "method_invocation" => {
            let (Some(name), Some(arguments)) =
                (node.child_by_field_name("name"), argument_count(node))
            else {
                return JavaReceiver::Unknown;
            };
            JavaReceiver::Invocation {
                receiver: node.child_by_field_name("object").map(|object| {
                    Box::new(expression_receiver(
                        object,
                        owner,
                        source,
                        scopes,
                        depth + 1,
                    ))
                }),
                name: text(name, source).to_owned(),
                line: name.start_position().row as i64 + 1,
                arguments,
            }
        }
        _ => JavaReceiver::Unknown,
    }
}

impl JavaSource {
    pub fn parse(source: &str) -> Result<Self> {
        let tree = parse_tree(source, &LANGUAGE)?;
        let scopes = variable_scopes(tree.root_node(), source);
        let mut result = Self::default();
        let mut cursor = tree.root_node().walk();
        for declaration in tree.root_node().named_children(&mut cursor) {
            match declaration.kind() {
                "package_declaration" => {
                    let mut children = declaration.walk();
                    if let Some(name) = declaration
                        .named_children(&mut children)
                        .find(|node| matches!(node.kind(), "identifier" | "scoped_identifier"))
                    {
                        result.package = type_name(name, source).unwrap_or_default();
                    };
                }
                "import_declaration" => {
                    let mut parts = Vec::new();
                    let mut is_static = false;
                    walk_tree_preorder(&declaration, |node| {
                        match node.kind() {
                            "identifier" | "asterisk" => parts.push(text(node, source)),
                            "static" => is_static = true,
                            _ => {}
                        }
                        WalkControl::Continue
                    });
                    if !parts.is_empty() {
                        if is_static {
                            result.static_imports.push(parts.join("::"));
                        } else {
                            result.imports.push(parts.join("::"));
                        }
                    }
                }
                _ => {}
            }
        }
        let mut type_sites = HashSet::new();
        walk_tree_preorder(&tree.root_node(), |node| {
            if matches!(
                node.kind(),
                "class_declaration"
                    | "interface_declaration"
                    | "enum_declaration"
                    | "record_declaration"
                    | "annotation_type_declaration"
            ) {
                if let Some(name) = node.child_by_field_name("name") {
                    let mut parents = Vec::new();
                    let mut cursor = node.walk();
                    for branch in node.named_children(&mut cursor) {
                        if matches!(
                            branch.kind(),
                            "superclass" | "super_interfaces" | "extends_interfaces"
                        ) {
                            walk_tree_preorder(&branch, |parent| {
                                if matches!(
                                    parent.kind(),
                                    "type_identifier" | "scoped_type_identifier" | "generic_type"
                                ) {
                                    if let Some(path) = type_name(parent, source) {
                                        parents.push(path);
                                    }
                                    return WalkControl::SkipChildren;
                                }
                                WalkControl::Continue
                            });
                        }
                    }
                    result.parents.insert(
                        (
                            text(name, source).to_owned(),
                            name.start_position().row as i64 + 1,
                        ),
                        parents,
                    );
                }
            }
            if matches!(
                node.kind(),
                "package_declaration"
                    | "import_declaration"
                    | "line_comment"
                    | "block_comment"
                    | "string_literal"
                    | "character_literal"
            ) {
                return WalkControl::SkipChildren;
            }
            if !matches!(node.kind(), "identifier" | "type_identifier") {
                return WalkControl::Continue;
            }
            let parent = node.parent();
            let declaration_name = parent.is_some_and(|parent| {
                (parent.kind().ends_with("_declaration")
                    || matches!(
                        parent.kind(),
                        "variable_declarator"
                            | "formal_parameter"
                            | "spread_parameter"
                            | "catch_formal_parameter"
                            | "type_parameter"
                            | "enum_constant"
                            | "enhanced_for_statement"
                            | "type_pattern"
                            | "instanceof_expression"
                    ))
                    && parent
                        .child_by_field_name("name")
                        .is_some_and(|name| name.id() == node.id())
            });
            if declaration_name {
                return WalkControl::Continue;
            }
            let annotation = parent
                .is_some_and(|parent| matches!(parent.kind(), "annotation" | "marker_annotation"));
            let typed = node.kind() == "type_identifier" || annotation;
            let key = (
                node.start_position().row as i64 + 1,
                text(node, source).to_owned(),
            );
            let binding = if typed {
                type_sites.insert(key.clone());
                let mut path = node;
                while let Some(parent) = path.parent() {
                    if parent.kind() != "scoped_type_identifier"
                        || !parent
                            .named_child(parent.named_child_count().saturating_sub(1) as u32)
                            .is_some_and(|name| name.id() == path.id())
                    {
                        break;
                    }
                    path = parent;
                }
                type_name(path, source).filter(|name| {
                    !path.has_error()
                        && !type_parameter(
                            node,
                            name.split("::").next().unwrap_or_default(),
                            source,
                        )
                })
            } else {
                None
            };
            result
                .types
                .entry(key)
                .and_modify(|previous| {
                    if *previous != binding {
                        *previous = None;
                    }
                })
                .or_insert(binding);
            WalkControl::Continue
        });
        result.types.retain(|key, _| type_sites.contains(key));
        let mut tracked = HashSet::new();
        walk_tree_preorder(&tree.root_node(), |node| {
            if node.kind() == "record_declaration" {
                if let Some(parameters) = node.child_by_field_name("parameters") {
                    let mut cursor = parameters.walk();
                    for component in parameters.named_children(&mut cursor) {
                        let Some(name) = component.child_by_field_name("name") else {
                            continue;
                        };
                        let explicit = node.child_by_field_name("body").is_some_and(|body| {
                            let mut cursor = body.walk();
                            let explicit = body.named_children(&mut cursor).any(|method| {
                                method.kind() == "method_declaration"
                                    && method.child_by_field_name("name").is_some_and(|other| {
                                        text(other, source) == text(name, source)
                                    })
                                    && method.child_by_field_name("parameters").is_some_and(
                                        |parameters| {
                                            let mut cursor = parameters.walk();
                                            let has_parameters = parameters
                                                .named_children(&mut cursor)
                                                .any(|parameter| {
                                                    matches!(
                                                        parameter.kind(),
                                                        "formal_parameter" | "spread_parameter"
                                                    )
                                                });
                                            !has_parameters
                                        },
                                    )
                            });
                            explicit
                        });
                        if !explicit {
                            result
                                .parameters
                                .entry((
                                    text(name, source).to_owned(),
                                    name.start_position().row as i64 + 1,
                                ))
                                .and_modify(|previous| *previous = None)
                                .or_insert(Some((0, false)));
                        }
                    }
                }
            }
            if matches!(
                node.kind(),
                "method_declaration" | "constructor_declaration"
            ) {
                if let Some(name) = node.child_by_field_name("name") {
                    let declared = node
                        .child_by_field_name("type")
                        .and_then(|ty| type_name(ty, source))
                        .filter(|ty| {
                            !type_parameter(node, ty.split("::").next().unwrap_or_default(), source)
                        });
                    result
                        .returns
                        .entry((
                            text(name, source).to_owned(),
                            name.start_position().row as i64 + 1,
                        ))
                        .and_modify(|previous| *previous = None)
                        .or_insert(declared);
                }
                if let (Some(name), Some(parameters)) = (
                    node.child_by_field_name("name"),
                    node.child_by_field_name("parameters"),
                ) {
                    let mut cursor = parameters.walk();
                    let mut count = 0;
                    let mut variadic = false;
                    for parameter in parameters.named_children(&mut cursor) {
                        if matches!(parameter.kind(), "formal_parameter" | "spread_parameter") {
                            count += 1;
                            variadic |= parameter.kind() == "spread_parameter";
                        }
                    }
                    let key = (
                        text(name, source).to_owned(),
                        name.start_position().row as i64 + 1,
                    );
                    result
                        .parameters
                        .entry(key)
                        .and_modify(|previous| *previous = None)
                        .or_insert(Some((count, variadic)));
                }
            }
            if !matches!(node.kind(), "method_invocation" | "method_reference") || node.has_error()
            {
                return WalkControl::Continue;
            }
            let Some(owner) = callable(node) else {
                return WalkControl::Continue;
            };
            let reference = node.kind() == "method_reference";
            let name = if reference {
                node.named_child(node.named_child_count().saturating_sub(1) as u32)
                    .filter(|name| name.kind() == "identifier")
            } else {
                node.child_by_field_name("name")
            };
            let (Some(owner_name), Some(name)) = (owner.child_by_field_name("name"), name) else {
                return WalkControl::Continue;
            };
            let key = (
                text(owner_name, source).to_owned(),
                owner_name.start_position().row as i64 + 1,
                name.start_position().row as i64 + 1,
                text(name, source).to_owned(),
            );
            let object = if reference {
                node.named_child(0)
            } else {
                node.child_by_field_name("object")
            };
            if let Some(object) = object {
                let call = Some(ExpressionCall {
                    receiver: expression_receiver(object, owner, source, &scopes, 0),
                    arguments: if reference {
                        None
                    } else {
                        argument_count(node)
                    },
                });
                result
                    .expressions
                    .entry(key.clone())
                    .and_modify(|previous| {
                        if *previous != call {
                            *previous = None;
                        }
                    })
                    .or_insert(call);
            }
            if reference {
                return WalkControl::Continue;
            }
            if node.child_by_field_name("object").is_none() {
                let arguments = node.child_by_field_name("arguments").map(|arguments| {
                    let mut cursor = arguments.walk();
                    arguments
                        .named_children(&mut cursor)
                        .filter(|argument| !argument.is_extra())
                        .count()
                });
                result
                    .bare_calls
                    .entry(key.clone())
                    .and_modify(|previous| {
                        if *previous != arguments {
                            *previous = None;
                        }
                    })
                    .or_insert(arguments);
            }
            if text(owner_name, source) == text(name, source) {
                let direct_call = node
                    .child_by_field_name("object")
                    .is_none_or(|object| object.kind() == "this")
                    .then(|| {
                        node.child_by_field_name("arguments").map(|arguments| {
                            let mut cursor = arguments.walk();
                            arguments
                                .named_children(&mut cursor)
                                .filter(|argument| !argument.is_extra())
                                .count()
                        })
                    })
                    .flatten();
                result
                    .direct_calls
                    .entry(key.clone())
                    .and_modify(|previous| {
                        if *previous != direct_call {
                            *previous = None;
                        }
                    })
                    .or_insert(direct_call);
            }
            let mut known_binding = false;
            let inferred = node.child_by_field_name("object").and_then(|object| {
                let (receiver, fields_only) = match object.kind() {
                    "identifier" => (text(object, source), false),
                    "field_access"
                        if object
                            .child_by_field_name("object")
                            .is_some_and(|base| base.kind() == "this") =>
                    {
                        (text(object.child_by_field_name("field")?, source), true)
                    }
                    _ => return None,
                };
                if !fields_only {
                    if let Some(parameters) = owner.child_by_field_name("parameters") {
                        let mut cursor = parameters.walk();
                        for parameter in parameters.named_children(&mut cursor) {
                            if parameter.kind() != "formal_parameter" {
                                continue;
                            }
                            let Some(binding) = parameter.child_by_field_name("name") else {
                                continue;
                            };
                            if text(binding, source) == receiver {
                                known_binding = true;
                                let mut cursor = parameter.walk();
                                if parameter
                                    .named_children(&mut cursor)
                                    .any(|child| child.kind() == "dimensions")
                                {
                                    return None;
                                }
                                let name =
                                    type_name(parameter.child_by_field_name("type")?, source)?;
                                if type_parameter(owner, name.split("::").next()?, source) {
                                    return None;
                                }
                                let arguments = node.child_by_field_name("arguments")?;
                                let mut cursor = arguments.walk();
                                return Some(ParameterCall {
                                    receiver: name,
                                    arguments: arguments
                                        .named_children(&mut cursor)
                                        .filter(|argument| !argument.is_extra())
                                        .count(),
                                });
                            }
                        }
                    }
                }
                let declared = variable_type(node, receiver, fields_only, &scopes)?;
                known_binding = true;
                let declared = declared?;
                if type_parameter(owner, declared.split("::").next()?, source) {
                    return None;
                }
                let arguments = node.child_by_field_name("arguments")?;
                let mut cursor = arguments.walk();
                Some(ParameterCall {
                    receiver: declared.to_owned(),
                    arguments: arguments
                        .named_children(&mut cursor)
                        .filter(|argument| !argument.is_extra())
                        .count(),
                })
            });
            if known_binding {
                tracked.insert(key.clone());
            }
            result
                .invocations
                .entry(key)
                .and_modify(|previous| {
                    if *previous != inferred {
                        *previous = None;
                    }
                })
                .or_insert(inferred);
            WalkControl::Continue
        });
        // Retain unknown binding types and collisions as negative evidence.
        // Removing them would revive an unrelated name-based fallback.
        result.invocations.retain(|key, _| tracked.contains(key));
        Ok(result)
    }

    pub fn parent_types(&self, name: &str, line: i64) -> Option<&[String]> {
        self.parents
            .get(&(name.to_owned(), line))
            .map(Vec::as_slice)
    }

    pub fn expression_call(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&Option<ExpressionCall>> {
        self.expressions
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
    }

    pub fn return_type(&self, name: &str, line: i64) -> Option<&str> {
        self.returns
            .get(&(name.to_owned(), line))
            .and_then(|ty| ty.as_deref())
    }

    pub fn type_reference(&self, line: i64, name: &str) -> Option<&Option<String>> {
        self.types.get(&(line, name.to_owned()))
    }

    pub fn parameter_call(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&Option<ParameterCall>> {
        self.invocations
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
    }

    pub fn recursive_arguments(&self, owner: &str, owner_line: i64, line: i64) -> Option<usize> {
        self.direct_calls
            .get(&(owner.to_owned(), owner_line, line, owner.to_owned()))
            .copied()
            .flatten()
    }

    pub fn bare_arguments(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<Option<usize>> {
        self.bare_calls
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
            .copied()
    }

    pub fn accepts_arguments(&self, name: &str, line: i64, arguments: usize) -> bool {
        self.parameters
            .get(&(name.to_owned(), line))
            .and_then(|value| *value)
            .is_some_and(|(count, variadic)| {
                arguments == count || (variadic && arguments >= count - 1)
            })
    }

    #[cfg(test)]
    pub fn receiver_type(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&str> {
        self.parameter_call(owner, owner_line, line, name)?
            .as_ref()
            .map(|call| call.receiver.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::JavaSource;

    #[test]
    fn static_imports_and_bare_call_arity_are_separate_from_type_imports() {
        let java = JavaSource::parse("import java.util.List;\nimport static java.util.function.Function.identity;\nimport static java.lang.Integer.*;\nclass Probe {\n Object library() { return identity(/* no arguments */); }\n Integer qualified() { return Integer.valueOf(1); }\n}\n").unwrap();
        assert_eq!(java.imports, ["java::util::List"]);
        assert_eq!(
            java.static_imports,
            [
                "java::util::function::Function::identity",
                "java::lang::Integer::*"
            ]
        );
        assert_eq!(
            java.bare_arguments("library", 5, 5, "identity"),
            Some(Some(0))
        );
        assert_eq!(java.bare_arguments("qualified", 6, 6, "valueOf"), None);
    }

    #[test]
    fn parent_paths_preserve_qualifiers_nesting_and_generic_erasure() {
        let java = JavaSource::parse(
            r#"class Child extends fixture.a.Leaf<String> implements fixture.b.Face, Outer.Inner {}
interface Face extends fixture.a.Base<String>, Other {}
"#,
        )
        .unwrap();
        assert_eq!(
            java.parent_types("Child", 1).unwrap(),
            ["fixture::a::Leaf", "fixture::b::Face", "Outer::Inner"]
        );
        assert_eq!(
            java.parent_types("Face", 2).unwrap(),
            ["fixture::a::Base", "Other"]
        );
    }

    #[test]
    fn type_references_preserve_qualified_paths_and_generic_shadows() {
        let java = JavaSource::parse(
            r#"class Probe<Leaf> {
 fixture.a.Leaf qualified;
 Leaf generic;
 Outer.Inner nested;
 java.util.List<fixture.a.Leaf[]> arguments;
}"#,
        )
        .unwrap();
        assert_eq!(
            java.type_reference(2, "Leaf"),
            Some(&Some("fixture::a::Leaf".to_owned()))
        );
        assert_eq!(java.type_reference(3, "Leaf"), Some(&None));
        assert_eq!(
            java.type_reference(4, "Outer"),
            Some(&Some("Outer".to_owned()))
        );
        assert_eq!(
            java.type_reference(4, "Inner"),
            Some(&Some("Outer::Inner".to_owned()))
        );
        assert_eq!(
            java.type_reference(5, "List"),
            Some(&Some("java::util::List".to_owned()))
        );
        assert_eq!(
            java.type_reference(5, "Leaf"),
            Some(&Some("fixture::a::Leaf".to_owned()))
        );
    }

    #[test]
    fn type_rows_keep_same_line_path_and_value_collisions_unresolved() {
        let java = JavaSource::parse(
            r#"class Probe {
 void collision(A value, other.A another) {}
 void value(A input) { Object A = null; consume(A); }
 void repeated(A first, A second) {}
}"#,
        )
        .unwrap();
        assert_eq!(java.type_reference(2, "A"), Some(&None));
        assert_eq!(java.type_reference(3, "A"), Some(&None));
        assert_eq!(java.type_reference(4, "A"), Some(&Some("A".to_owned())));
    }

    #[test]
    fn fields_and_locals_respect_declaration_order_and_scope() {
        let java = JavaSource::parse("class Probe {\n A receiver;\n void field() { receiver.leaf(); }\n void local() { B receiver = new B(); receiver.leaf(); }\n void before() { receiver.leaf(); B receiver = new B(); }\n void sibling() {\n  { B receiver = new B(); receiver.leaf(); }\n  receiver.leaf();\n }\n}\n").unwrap();
        assert_eq!(java.receiver_type("field", 3, 3, "leaf"), Some("A"));
        assert_eq!(java.receiver_type("local", 4, 4, "leaf"), Some("B"));
        assert_eq!(java.receiver_type("before", 5, 5, "leaf"), Some("A"));
        assert_eq!(java.receiver_type("sibling", 6, 7, "leaf"), Some("B"));
        assert_eq!(java.receiver_type("sibling", 6, 8, "leaf"), Some("A"));
    }

    #[test]
    fn this_field_does_not_borrow_a_parameter_or_local_binding() {
        let java = JavaSource::parse("class Probe {\n A receiver;\n void parameter(B receiver) { receiver.leaf(); }\n void explicit(B receiver) { this.receiver.leaf(); }\n void local() { B receiver = new B(); this.receiver.leaf(); }\n}\n").unwrap();
        assert_eq!(java.receiver_type("parameter", 3, 3, "leaf"), Some("B"));
        assert_eq!(java.receiver_type("explicit", 4, 4, "leaf"), Some("A"));
        assert_eq!(java.receiver_type("local", 5, 5, "leaf"), Some("A"));
    }

    #[test]
    fn array_and_inferred_bindings_remain_unresolved() {
        let java = JavaSource::parse("class Probe {\n A receiver[];\n void field() { receiver.leaf(); }\n void local() { A receiver[] = null; receiver.leaf(); }\n void inferred() { var receiver = factory(); receiver.leaf(); }\n}\n").unwrap();
        assert_eq!(java.receiver_type("field", 3, 3, "leaf"), None);
        assert_eq!(java.receiver_type("local", 4, 4, "leaf"), None);
        assert_eq!(java.receiver_type("inferred", 5, 5, "leaf"), None);
        assert!(java
            .parameter_call("field", 3, 3, "leaf")
            .unwrap()
            .is_none());
        assert!(java
            .parameter_call("inferred", 5, 5, "leaf")
            .unwrap()
            .is_none());
    }

    #[test]
    fn explicit_parameters_keep_the_callable_and_reference_identity() {
        let java = JavaSource::parse("package fixture;\nclass B {\n int leaf() { return 2; }\n int useA(A receiver) { return receiver.leaf(); }\n int useB(B receiver) { return receiver.leaf(); }\n}\n").unwrap();
        assert_eq!(java.package, "fixture");
        assert_eq!(java.receiver_type("useA", 4, 4, "leaf"), Some("A"));
        assert_eq!(java.receiver_type("useB", 5, 5, "leaf"), Some("B"));
        assert_eq!(java.receiver_type("useA", 4, 5, "leaf"), None);
    }

    #[test]
    fn colliding_same_line_calls_do_not_choose_the_first_receiver() {
        let java =
            JavaSource::parse("class Probe {\n void use(A a, B b) { a.leaf(); b.leaf(); }\n}\n")
                .unwrap();
        assert_eq!(java.receiver_type("use", 2, 2, "leaf"), None);
        assert!(java.parameter_call("use", 2, 2, "leaf").unwrap().is_none());
    }

    #[test]
    fn type_parameters_and_lambda_boundaries_are_not_class_guesses() {
        let java = JavaSource::parse("class Probe<A> {\n void use(A a) { a.leaf(); }\n void lambda(B b) { Runnable r = () -> b.leaf(); }\n}\n").unwrap();
        assert_eq!(java.receiver_type("use", 2, 2, "leaf"), None);
        assert!(java.parameter_call("use", 2, 2, "leaf").unwrap().is_none());
        assert_eq!(java.receiver_type("lambda", 3, 3, "leaf"), None);
    }

    #[test]
    fn arity_and_array_parameters_keep_conservative_syntax_evidence() {
        let java = JavaSource::parse("class Probe {\n void use(A receiver) { receiver.leaf(/* before */ 1 /* after */); }\n void leaf() {}\n void leaf(int value) {}\n void varargs(int... values) {}\n void prefix(A[] receiver) { receiver.leaf(); }\n void postfix(A receiver[]) { receiver.leaf(); }\n}\n").unwrap();
        assert_eq!(
            java.parameter_call("use", 2, 2, "leaf")
                .unwrap()
                .as_ref()
                .unwrap()
                .arguments,
            1
        );
        assert!(java.accepts_arguments("leaf", 3, 0));
        assert!(!java.accepts_arguments("leaf", 3, 1));
        assert!(java.accepts_arguments("leaf", 4, 1));
        assert!(java.accepts_arguments("varargs", 5, 0));
        assert!(java.accepts_arguments("varargs", 5, 3));
        assert!(java
            .parameter_call("prefix", 6, 6, "leaf")
            .unwrap()
            .is_none());
        assert!(java
            .parameter_call("postfix", 7, 7, "leaf")
            .unwrap()
            .is_none());
    }

    #[test]
    fn packages_and_nonstatic_imports_come_from_syntax() {
        let java = JavaSource::parse("package foo /* gap */ .bar;\nimport pkg /* gap */ .Type;\nimport other.*;\nimport static pkg.Type.leaf;\nclass Probe {}\n").unwrap();
        assert_eq!(java.package, "foo::bar");
        assert_eq!(java.imports, ["pkg::Type", "other::*"]);
    }
}
