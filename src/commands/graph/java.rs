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
    /// Callable identity, reference line and name; None means colliding or
    /// unsupported invocations. Never confidently choose the first on a line.
    invocations: HashMap<(String, i64, i64, String), Option<ParameterCall>>,
    parameters: HashMap<(String, i64), Option<(usize, bool)>>,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ParameterCall {
    pub receiver: String,
    pub arguments: usize,
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

impl JavaSource {
    pub fn parse(source: &str) -> Result<Self> {
        let tree = parse_tree(source, &LANGUAGE)?;
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
                    if !is_static && !parts.is_empty() {
                        result.imports.push(parts.join("::"));
                    }
                }
                _ => {}
            }
        }
        let mut tracked = HashSet::new();
        walk_tree_preorder(&tree.root_node(), |node| {
            if matches!(
                node.kind(),
                "method_declaration" | "constructor_declaration"
            ) {
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
            if node.kind() != "method_invocation" || node.has_error() {
                return WalkControl::Continue;
            }
            let Some(owner) = callable(node) else {
                return WalkControl::Continue;
            };
            let (Some(owner_name), Some(name)) = (
                owner.child_by_field_name("name"),
                node.child_by_field_name("name"),
            ) else {
                return WalkControl::Continue;
            };
            let key = (
                text(owner_name, source).to_owned(),
                owner_name.start_position().row as i64 + 1,
                name.start_position().row as i64 + 1,
                text(name, source).to_owned(),
            );
            let mut known_parameter = false;
            let inferred = node
                .child_by_field_name("object")
                .filter(|object| object.kind() == "identifier")
                .and_then(|object| {
                    let parameters = owner.child_by_field_name("parameters")?;
                    let mut cursor = parameters.walk();
                    for parameter in parameters.named_children(&mut cursor) {
                        if parameter.kind() != "formal_parameter" {
                            continue;
                        }
                        let Some(binding) = parameter.child_by_field_name("name") else {
                            continue;
                        };
                        if text(binding, source) == text(object, source) {
                            known_parameter = true;
                            let mut cursor = parameter.walk();
                            if parameter
                                .named_children(&mut cursor)
                                .any(|child| child.kind() == "dimensions")
                            {
                                return None;
                            }
                            let name = type_name(parameter.child_by_field_name("type")?, source)?;
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
                    None
                });
            if known_parameter {
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
        // Retain unknown parameter types and collisions as negative evidence.
        // Removing them would revive an unrelated name-based fallback.
        result.invocations.retain(|key, _| tracked.contains(key));
        Ok(result)
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
