//! Conservative syntax evidence for Java graph resolution, not type inference.
use std::collections::{HashMap, HashSet};
use std::ops::Range;
use std::sync::LazyLock;

use anyhow::Result;
use tree_sitter::{Language, Node};

use crate::parsers::treesitter::java::pattern_flow_scopes;
use crate::parsers::treesitter::{parse_tree, walk_tree_preorder, WalkControl};

static LANGUAGE: LazyLock<Language> = LazyLock::new(|| tree_sitter_java::LANGUAGE.into());

#[derive(Clone, Copy)]
pub(super) enum TypeAccess {
    Public,
    Package,
    Private,
    Protected,
}

pub(super) struct TypeDeclaration {
    pub site: usize,
    pub access: TypeAccess,
    pub static_member: bool,
    pub local: bool,
    pub local_scope: Option<Range<usize>>,
}

/// Synthetic accessors may lack an indexed syntax site. Only unique metadata
/// can bind them; colliding name/line entries must never borrow another owner.
fn declaration_metadata<'a, T>(
    entries: &'a HashMap<(String, i64, usize), T>,
    name: &str,
    line: i64,
    site: Option<usize>,
) -> Option<&'a T> {
    if let Some(site) = site {
        return entries.get(&(name.to_owned(), line, site));
    }
    let mut matches = entries
        .iter()
        .filter(|((candidate, row, _), _)| candidate == name && *row == line);
    let (_, first) = matches.next()?;
    matches.next().is_none().then_some(first)
}

#[derive(Clone, PartialEq, Eq)]
pub(super) enum InvocationArgument {
    Type(String),
    Field(JavaReceiver),
    Value(JavaReceiver),
    Lambda(usize),
}

type InvocationArguments = Vec<Option<InvocationArgument>>;
type InvocationSignature = (Vec<Option<String>>, bool);

#[derive(Clone, PartialEq, Eq)]
pub(super) struct InvocationOwner {
    pub name: String,
    pub line: i64,
    pub ordinal: usize,
}

#[derive(Default)]
pub(super) struct JavaSource {
    pub package: String,
    pub imports: Vec<String>,
    pub static_imports: Vec<String>,
    declarations: HashMap<(String, i64), Vec<TypeDeclaration>>,
    /// Source order bridges indexed rows to exact syntax occurrences. Local
    /// type names, lines and qualified names are not declaration identities.
    symbol_sites: HashMap<(String, i64, String), Vec<usize>>,
    site_containers: HashMap<usize, Option<usize>>,
    site_ranges: HashMap<usize, Range<usize>>,
    declaration_ranges: HashMap<(String, i64), Option<Range<usize>>>,
    member_containers: HashMap<(String, i64), Option<(String, i64)>>,
    type_positions: HashMap<(i64, String), Vec<usize>>,
    value_receivers: HashSet<(i64, String)>,
    lexical_receivers: HashMap<(i64, String), Vec<String>>,
    /// Reference rows carry a line and name, not a byte position. Colliding
    /// paths or value/type uses on one line remain explicit negative evidence.
    types: HashMap<(i64, String), Option<String>>,
    type_owners: HashMap<(i64, String), Option<(String, i64)>>,
    /// Graph binding needs the full syntax path; legacy inheritance rows may
    /// contain only its short name.
    parents: HashMap<usize, Vec<String>>,
    parent_receivers: HashMap<usize, Vec<(JavaReceiver, ReceiverTypeSite)>>,
    /// Callable identity, reference line and name; None means colliding or
    /// unsupported invocations. Never confidently choose the first on a line.
    invocations: HashMap<(String, i64, i64, String), Option<ParameterCall>>,
    direct_calls: HashMap<(String, i64, i64, String), Option<usize>>,
    bare_calls: HashMap<(String, i64, i64, String), Option<usize>>,
    parameters: HashMap<(String, i64), Option<(usize, bool)>>,
    returns: HashMap<(String, i64, usize), Option<String>>,
    return_receivers: HashMap<(String, i64, usize), JavaReceiver>,
    member_receivers: HashMap<(String, i64, usize), JavaReceiver>,
    member_invocation_types: HashMap<(String, i64, usize), Option<String>>,
    getter_receivers: HashMap<(String, i64, String), Option<(i64, JavaReceiver)>>,
    type_parameters: HashMap<(String, i64, usize), Vec<String>>,
    return_parameters: HashMap<(String, i64, usize), String>,
    type_bounds: HashMap<(String, i64, usize), HashMap<String, String>>,
    expressions: HashMap<(String, i64, i64, String), Option<ExpressionCall>>,
    expression_variants: HashMap<(String, i64, i64, String), Vec<ExpressionCall>>,
    constructors: HashMap<(String, i64, i64, String), Option<ConstructorCall>>,
    constructor_types: HashMap<(String, i64), Vec<Vec<String>>>,
    callback_parameters: HashMap<(String, i64), Vec<Option<JavaReceiver>>>,
    reference_parameters: HashMap<(String, i64), Vec<Option<JavaReceiver>>>,
    static_methods: HashSet<(String, i64)>,
    constructor_declarations: HashSet<(String, i64)>,
    canonical_types: HashMap<String, Vec<String>>,
    creation_types: HashMap<(String, i64, i64, String), Vec<Option<String>>>,
    invocation_types: HashMap<(String, i64, i64, String), Option<InvocationArguments>>,
    invocation_parameters: HashMap<(String, i64), Vec<Option<String>>>,
    /// Declaration order distinguishes overloads with the same name and line.
    invocation_signatures: HashMap<(String, i64), Vec<InvocationSignature>>,
    /// Syntax owners of call sites, before reference rows collapse byte positions.
    invocation_owners: HashMap<(i64, String), Vec<InvocationOwner>>,
}

#[derive(Clone, PartialEq, Eq, Hash)]
pub(super) enum JavaReceiver {
    Declared {
        receiver: Box<JavaReceiver>,
        site: ReceiverTypeSite,
    },
    /// A graph-local identity after declaration-site syntax binding. Retaining
    /// the index avoids rebinding local types through their nonunique names.
    BoundType {
        class: u32,
        arguments: Option<Vec<Option<JavaReceiver>>>,
    },
    Type(String),
    Parameter(String),
    Parameterized {
        path: String,
        arguments: Vec<Option<JavaReceiver>>,
    },
    Array(Option<String>),
    Field {
        receiver: Box<JavaReceiver>,
        name: String,
    },
    Element {
        receiver: Box<JavaReceiver>,
        operation: String,
    },
    This,
    Super,
    Invocation {
        receiver: Option<Box<JavaReceiver>>,
        name: String,
        line: i64,
        arguments: usize,
        first_argument: Option<Box<JavaReceiver>>,
    },
    MethodProjection {
        input: Box<JavaReceiver>,
        receiver: Box<JavaReceiver>,
        qualifier: Option<String>,
        name: String,
        line: i64,
    },
    CollectedMap {
        stream: Box<JavaReceiver>,
        collector: Box<JavaReceiver>,
        key: Box<JavaReceiver>,
        value: Box<JavaReceiver>,
    },
    StreamFactory {
        qualifier: Box<JavaReceiver>,
        elements: Vec<JavaReceiver>,
        invocation: Box<JavaReceiver>,
    },
    Callback {
        receiver: Option<Box<JavaReceiver>>,
        name: String,
        line: i64,
        arguments: usize,
        parameter: usize,
        input: usize,
        class_literals: Vec<Option<String>>,
        context: Option<Box<JavaReceiver>>,
    },
    CapturedField {
        value: Box<JavaReceiver>,
        name: String,
        boundary: String,
    },
    /// An undeclared syntax name may be an inherited field or a type qualifier.
    /// Only graph binding can prove the field exists.
    LexicalName {
        name: String,
        boundary: String,
    },
    Identity,
    Unknown,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ExpressionCall {
    pub owner_site: usize,
    pub receiver: JavaReceiver,
    pub receiver_site: Option<ReceiverTypeSite>,
    /// A method reference has no invocation argument list.
    pub arguments: Option<usize>,
    pub reference_context: Option<JavaReceiver>,
    /// A syntax candidate; qualified names must still resolve as types, not fields.
    pub reference_type: Option<String>,
}

#[derive(Clone, PartialEq, Eq, Hash)]
pub(super) struct ReceiverTypeSite {
    pub path: String,
    /// Exact nominal type site, including all explicit generic arguments.
    pub position: usize,
    pub owner: String,
    pub owner_line: i64,
    pub owner_site: usize,
    pub owner_ordinal: Option<usize>,
}

#[derive(Clone, Copy)]
pub(super) enum TypeReference<'a> {
    Occurrences(i64, &'a str),
    Position(usize),
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ParameterCall {
    pub receiver: String,
    pub arguments: usize,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct ConstructorCall {
    pub receiver: String,
    pub arguments: Option<usize>,
}

struct VariableBinding {
    position: usize,
    field: bool,
    declared: Option<String>,
    inferred: Option<JavaReceiver>,
    invocation_type: Option<String>,
    site: Option<ReceiverTypeSite>,
}

type VariableScopes = HashMap<usize, HashMap<String, Vec<VariableBinding>>>;

/// Temporary per-file lexical inventory, discarded after deriving calls.
/// Index scopes once instead of rescanning every declaration for each call.
fn variable_scopes(
    root: Node<'_>,
    source: &str,
    declarations: &HashMap<usize, InvocationOwner>,
) -> VariableScopes {
    let mut scopes = VariableScopes::new();
    walk_tree_preorder(&root, |declaration| {
        if declaration.kind() == "catch_formal_parameter" {
            if let (Some(name), Some(clause)) = (
                declaration.child_by_field_name("name"),
                declaration.parent(),
            ) {
                let mut cursor = declaration.walk();
                let ty = declaration
                    .named_children(&mut cursor)
                    .find(|node| node.kind() == "catch_type");
                // A multi-catch least upper bound needs semantic evidence. Still
                // record its binding so it cannot borrow an enclosing field.
                let declared = ty.and_then(|ty| {
                    let mut cursor = ty.walk();
                    let types: Vec<_> = ty
                        .named_children(&mut cursor)
                        .filter(|node| !node.is_extra())
                        .collect();
                    (types.len() == 1)
                        .then(|| type_name(types[0], source))
                        .flatten()
                });
                scopes
                    .entry(clause.id())
                    .or_default()
                    .entry(text(name, source).to_owned())
                    .or_default()
                    .push(VariableBinding {
                        position: name.start_byte(),
                        field: false,
                        invocation_type: declared.clone(),
                        declared,
                        inferred: None,
                        site: None,
                    });
            }
        }
        if declaration.kind() == "resource" {
            if let (Some(name), Some(ty), Some(statement)) = (
                declaration.child_by_field_name("name"),
                declaration.child_by_field_name("type"),
                declaration.parent().and_then(|spec| spec.parent()),
            ) {
                // Resource bindings cover subsequent initializers and the try
                // body, but never sibling catch/finally clauses.
                for scope in [declaration.parent(), statement.child_by_field_name("body")]
                    .into_iter()
                    .flatten()
                {
                    scopes
                        .entry(scope.id())
                        .or_default()
                        .entry(text(name, source).to_owned())
                        .or_default()
                        .push(VariableBinding {
                            position: name.start_byte(),
                            field: false,
                            invocation_type: declared_invocation_type(ty, declaration, source),
                            declared: (text(ty, source) != "var")
                                .then(|| type_name(ty, source))
                                .flatten(),
                            inferred: generic_receiver(ty, declaration, source),
                            site: receiver_type_site(ty, source, declarations),
                        });
                }
            }
        }
        if declaration.kind() == "formal_parameter" {
            if let (Some(name), Some(ty), Some(owner)) = (
                declaration.child_by_field_name("name"),
                declaration.child_by_field_name("type"),
                declaration
                    .parent()
                    .and_then(|parameters| parameters.parent()),
            ) {
                if matches!(
                    owner.kind(),
                    "method_declaration" | "constructor_declaration"
                ) {
                    scopes
                        .entry(owner.id())
                        .or_default()
                        .entry(text(name, source).to_owned())
                        .or_default()
                        .push(VariableBinding {
                            position: name.start_byte(),
                            field: false,
                            invocation_type: declared_invocation_type(ty, declaration, source),
                            declared: type_name(ty, source),
                            inferred: generic_receiver(ty, owner, source),
                            site: receiver_type_site(ty, source, declarations),
                        });
                }
            }
        }
        if declaration.kind() == "instanceof_expression" {
            if let (Some(name), Some(ty)) = (
                declaration.child_by_field_name("name"),
                declaration.child_by_field_name("right"),
            ) {
                for (scope, position) in pattern_flow_scopes(declaration, source) {
                    scopes
                        .entry(scope.id())
                        .or_default()
                        .entry(text(name, source).to_owned())
                        .or_default()
                        .push(VariableBinding {
                            position,
                            field: false,
                            invocation_type: declared_invocation_type(ty, declaration, source),
                            declared: type_name(ty, source),
                            inferred: generic_receiver(ty, declaration, source),
                            site: receiver_type_site(ty, source, declarations),
                        });
                }
            }
        }
        if declaration.kind() == "type_pattern" {
            let mut cursor = declaration.walk();
            let children: Vec<_> = declaration.named_children(&mut cursor).collect();
            if let (Some(ty), Some(name)) = (children.first(), children.last()) {
                let mut ancestor = declaration.parent();
                while let Some(scope) = ancestor {
                    if scope.kind() == "switch_rule" {
                        scopes
                            .entry(scope.id())
                            .or_default()
                            .entry(text(*name, source).to_owned())
                            .or_default()
                            .push(VariableBinding {
                                position: name.start_byte(),
                                field: false,
                                invocation_type: declared_invocation_type(*ty, declaration, source),
                                declared: type_name(*ty, source),
                                inferred: generic_receiver(*ty, scope, source),
                                site: receiver_type_site(*ty, source, declarations),
                            });
                        break;
                    }
                    if matches!(scope.kind(), "method_declaration" | "class_body") {
                        break;
                    }
                    ancestor = scope.parent();
                }
            }
        }
        if declaration.kind() == "enhanced_for_statement" {
            if let Some(name) = declaration.child_by_field_name("name") {
                scopes
                    .entry(declaration.id())
                    .or_default()
                    .entry(text(name, source).to_owned())
                    .or_default()
                    .push(VariableBinding {
                        position: name.start_byte(),
                        field: false,
                        invocation_type: declaration
                            .child_by_field_name("type")
                            .and_then(|ty| declared_invocation_type(ty, declaration, source)),
                        declared: declaration
                            .child_by_field_name("type")
                            .and_then(|ty| type_name(ty, source)),
                        site: declaration
                            .child_by_field_name("type")
                            .and_then(|ty| receiver_type_site(ty, source, declarations)),
                        inferred: declaration
                            .child_by_field_name("type")
                            .and_then(|ty| generic_receiver(ty, declaration, source)),
                    });
            }
        }
        if declaration.kind() == "record_declaration" {
            if let (Some(body), Some(parameters)) = (
                declaration.child_by_field_name("body"),
                declaration.child_by_field_name("parameters"),
            ) {
                let mut cursor = parameters.walk();
                for component in parameters.named_children(&mut cursor) {
                    if let Some(name) = component.child_by_field_name("name") {
                        scopes
                            .entry(body.id())
                            .or_default()
                            .entry(text(name, source).to_owned())
                            .or_default()
                            .push(VariableBinding {
                                position: name.start_byte(),
                                field: true,
                                invocation_type: component
                                    .child_by_field_name("type")
                                    .and_then(|ty| declared_invocation_type(ty, component, source)),
                                declared: component
                                    .child_by_field_name("type")
                                    .and_then(|ty| type_name(ty, source)),
                                site: component
                                    .child_by_field_name("type")
                                    .and_then(|ty| receiver_type_site(ty, source, declarations)),
                                inferred: component
                                    .child_by_field_name("type")
                                    .and_then(|ty| generic_receiver(ty, declaration, source)),
                            });
                    }
                }
            }
        }
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
                    invocation_type: declared_invocation_type(declared_type, variable, source),
                    declared,
                    inferred: generic_receiver(declared_type, declaration, source),
                    site: (!array_suffix)
                        .then(|| receiver_type_site(declared_type, source, declarations))
                        .flatten(),
                });
        }
        WalkControl::Continue
    });
    walk_tree_preorder(&root, |declaration| {
        if declaration.kind() == "enhanced_for_statement"
            && declaration
                .child_by_field_name("type")
                .is_some_and(|ty| text(ty, source) == "var")
        {
            if let (Some(owner), Some(name), Some(value)) = (
                callable(declaration),
                declaration.child_by_field_name("name"),
                declaration.child_by_field_name("value"),
            ) {
                let receiver = expression_receiver(value, owner, source, &scopes, 0);
                if let Some(bindings) = scopes
                    .get_mut(&declaration.id())
                    .and_then(|scope| scope.get_mut(text(name, source)))
                {
                    for binding in bindings {
                        binding.inferred = Some(JavaReceiver::Element {
                            receiver: Box::new(receiver.clone()),
                            operation: "iterable-element".to_owned(),
                        });
                    }
                }
            }
        }
        if !matches!(
            declaration.kind(),
            "local_variable_declaration" | "resource"
        ) || !declaration
            .child_by_field_name("type")
            .is_some_and(|ty| text(ty, source) == "var")
        {
            return WalkControl::Continue;
        }
        let Some(owner) = callable(declaration) else {
            return WalkControl::Continue;
        };
        let mut cursor = declaration.walk();
        let variables: Vec<_> = if declaration.kind() == "resource" {
            vec![declaration]
        } else {
            declaration.named_children(&mut cursor).collect()
        };
        for variable in variables {
            if let (Some(name), Some(value)) = (
                variable.child_by_field_name("name"),
                variable.child_by_field_name("value"),
            ) {
                let inferred = expression_receiver(value, owner, source, &scopes, 0);
                let site = expression_receiver_site(value, source, &scopes, declarations);
                let invocation_type = invocation_argument_type(value, owner, source, &scopes, 0);
                for scope in scopes.values_mut() {
                    if let Some(bindings) = scope.get_mut(text(name, source)) {
                        for binding in bindings
                            .iter_mut()
                            .filter(|binding| binding.position == name.start_byte())
                        {
                            binding.inferred = Some(inferred.clone());
                            binding.site = site.clone();
                            binding.invocation_type = invocation_type.clone();
                        }
                    }
                }
            }
        }
        WalkControl::Continue
    });
    scopes
}

/// Check whether lexical field capture must stop at this scope.
fn blocks_enclosing_fields(node: Node<'_>, source: &str) -> bool {
    if !matches!(node.kind(), "class_body" | "interface_body" | "enum_body") {
        return false;
    }
    let Some(class) = node.parent() else {
        return true;
    };
    if class.kind() != "class_declaration" || class.child_by_field_name("superclass").is_some() {
        return true;
    }
    let mut cursor = class.walk();
    let blocked = class.named_children(&mut cursor).any(|child| {
        child.kind() == "modifiers"
            && text(child, source)
                .split_whitespace()
                .any(|word| word == "static")
    });
    blocked
}

fn variable_binding<'a>(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &'a VariableScopes,
    source: &str,
) -> Option<&'a VariableBinding> {
    let mut ancestor = Some(call);
    let mut fields_blocked = false;
    while let Some(node) = ancestor {
        if let Some(bindings) = scopes.get(&node.id()).and_then(|scope| scope.get(name)) {
            if let Some(binding) = bindings
                .iter()
                .filter(|binding| {
                    (!fields_only || binding.field)
                        && (!fields_blocked || !binding.field)
                        && (binding.field || binding.position < call.start_byte())
                })
                .max_by_key(|binding| binding.position)
            {
                return Some(binding);
            }
        }
        if blocks_enclosing_fields(node, source) {
            fields_blocked = true;
        }
        ancestor = node.parent();
    }
    None
}

fn variable_receiver(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &VariableScopes,
    source: &str,
) -> Option<JavaReceiver> {
    let binding = variable_binding(call, name, fields_only, scopes, source)?;
    let receiver = binding.inferred.clone().or_else(|| {
        binding.site.as_ref()?;
        binding.declared.clone().map(JavaReceiver::Type)
    })?;
    Some(if let Some(site) = &binding.site {
        JavaReceiver::Declared {
            receiver: Box::new(receiver),
            site: site.clone(),
        }
    } else {
        receiver
    })
}

fn variable_inferred<'a>(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &'a VariableScopes,
    source: &str,
) -> Option<&'a JavaReceiver> {
    variable_binding(call, name, fields_only, scopes, source)?
        .inferred
        .as_ref()
}

fn variable_type<'a>(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &'a VariableScopes,
    source: &str,
) -> Option<Option<&'a str>> {
    Some(
        variable_binding(call, name, fields_only, scopes, source)?
            .declared
            .as_deref(),
    )
}

fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}

/// Keep nominal and generic variable types in their declaring lexical scope.
/// Array projection remains separate evidence.
fn receiver_type_site(
    ty: Node<'_>,
    source: &str,
    declarations: &HashMap<usize, InvocationOwner>,
) -> Option<ReceiverTypeSite> {
    if !matches!(
        ty.kind(),
        "type_identifier" | "scoped_type_identifier" | "generic_type"
    ) {
        return None;
    }
    if ty.parent().is_some_and(|declaration| {
        let mut cursor = declaration.walk();
        declaration.kind() == "spread_parameter"
            || declaration
                .named_children(&mut cursor)
                .any(|node| node.kind() == "dimensions")
    }) {
        return None;
    }
    let path = type_name(ty, source)?;
    if type_parameter(ty, path.split("::").next()?, source) || path == "var" {
        return None;
    }
    let mut ancestor = ty.parent();
    while let Some(owner) = ancestor {
        if matches!(
            owner.kind(),
            "method_declaration"
                | "constructor_declaration"
                | "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
        ) {
            let name = owner.child_by_field_name("name")?;
            return Some(ReceiverTypeSite {
                path,
                position: ty.start_byte(),
                owner: text(name, source).to_owned(),
                owner_line: name.start_position().row as i64 + 1,
                owner_site: name.start_byte(),
                owner_ordinal: declarations.get(&owner.id()).map(|owner| owner.ordinal),
            });
        }
        ancestor = owner.parent();
    }
    None
}

fn expression_receiver_site(
    call: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
    declarations: &HashMap<usize, InvocationOwner>,
) -> Option<ReceiverTypeSite> {
    let (name, fields_only) = match call.kind() {
        "identifier" => (text(call, source), false),
        "field_access"
            if call
                .child_by_field_name("object")
                .is_some_and(|node| node.kind() == "this") =>
        {
            (text(call.child_by_field_name("field")?, source), true)
        }
        "object_creation_expression" | "cast_expression" => {
            return receiver_type_site(call.child_by_field_name("type")?, source, declarations)
        }
        "parenthesized_expression" => {
            return expression_receiver_site(call.named_child(0)?, source, scopes, declarations)
        }
        _ => return None,
    };
    let mut ancestor = Some(call);
    let mut fields_blocked = false;
    while let Some(node) = ancestor {
        // An untyped lambda binding must not borrow an outer variable's site.
        if !fields_only && node.kind() == "lambda_expression" {
            if let Some(parameters) = node.child_by_field_name("parameters") {
                let mut shadowed = false;
                walk_tree_preorder(&parameters, |binding| {
                    shadowed |= binding.kind() == "identifier" && text(binding, source) == name;
                    WalkControl::Continue
                });
                if shadowed {
                    return None;
                }
            }
        }
        if let Some(bindings) = scopes.get(&node.id()).and_then(|scope| scope.get(name)) {
            if let Some(binding) = bindings
                .iter()
                .filter(|binding| {
                    (!fields_only || binding.field)
                        && (!fields_blocked || !binding.field)
                        && (binding.field || binding.position < call.start_byte())
                })
                .max_by_key(|binding| binding.position)
            {
                return binding.site.clone();
            }
        }
        if blocks_enclosing_fields(node, source) {
            fields_blocked = true;
        }
        ancestor = node.parent();
    }
    None
}

fn type_name(node: Node<'_>, source: &str) -> Option<String> {
    if matches!(
        node.kind(),
        "integral_type" | "floating_point_type" | "boolean_type"
    ) {
        return Some(text(node, source).to_owned());
    }
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
        if parent.kind() == "variable_declarator"
            && parent
                .parent()
                .is_some_and(|parent| parent.kind() == "field_declaration")
        {
            return Some(parent);
        }
        if matches!(
            parent.kind(),
            "method_declaration"
                | "constructor_declaration"
                | "compact_constructor_declaration"
                | "enum_constant"
        ) {
            return Some(parent);
        }
        if parent.kind() == "class_body" {
            return None;
        }
        node = parent;
    }
    None
}

/// Retain source-order callable identities instead of breaking line-range ties.
fn callable_declarations(root: Node<'_>, source: &str) -> HashMap<usize, InvocationOwner> {
    let mut declarations = HashMap::new();
    let mut ordinals: HashMap<(String, i64), usize> = HashMap::new();
    walk_tree_preorder(&root, |node| {
        if matches!(
            node.kind(),
            "method_declaration"
                | "constructor_declaration"
                | "compact_constructor_declaration"
                | "annotation_type_element_declaration"
        ) {
            if let Some(name) = node.child_by_field_name("name") {
                let line = name.start_position().row as i64 + 1;
                let name = text(name, source).to_owned();
                let next = ordinals.entry((name.clone(), line)).or_default();
                declarations.insert(
                    node.id(),
                    InvocationOwner {
                        name,
                        line,
                        ordinal: *next,
                    },
                );
                *next += 1;
            }
        }
        WalkControl::Continue
    });
    declarations
}

/// Attribute invocations with the same callable identities as receiver sites.
fn invocation_owners(
    root: Node<'_>,
    source: &str,
    declarations: &HashMap<usize, InvocationOwner>,
) -> HashMap<(i64, String), Vec<InvocationOwner>> {
    let mut sites: HashMap<(i64, String), Vec<InvocationOwner>> = HashMap::new();
    walk_tree_preorder(&root, |node| {
        if node.has_error() {
            return WalkControl::Continue;
        }
        let identifier = match node.kind() {
            "method_invocation" => node.child_by_field_name("name"),
            "method_reference" => node
                .named_child(node.named_child_count().saturating_sub(1) as u32)
                .filter(|name| name.kind() == "identifier"),
            "object_creation_expression" => node.child_by_field_name("type").map(|mut ty| {
                while let Some(child) = ty.child_by_field_name("name").or_else(|| ty.named_child(0))
                {
                    ty = child;
                }
                ty
            }),
            "explicit_constructor_invocation" => node.child_by_field_name("constructor"),
            _ => None,
        };
        if let (Some(identifier), Some(owner)) = (
            identifier,
            callable(node).and_then(|owner| declarations.get(&owner.id())),
        ) {
            let owners = sites
                .entry((
                    identifier.start_position().row as i64 + 1,
                    text(identifier, source).to_owned(),
                ))
                .or_default();
            if !owners.contains(owner) {
                owners.push(owner.clone());
            }
        }
        WalkControl::Continue
    });
    sites
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

fn type_parameter_bound(mut owner: Node<'_>, name: &str, source: &str) -> Option<String> {
    loop {
        if let Some(parameters) = owner.child_by_field_name("type_parameters") {
            let mut cursor = parameters.walk();
            for parameter in parameters.named_children(&mut cursor) {
                if parameter
                    .named_child(0)
                    .is_some_and(|identifier| text(identifier, source) == name)
                {
                    let mut children = parameter.walk();
                    return parameter
                        .named_children(&mut children)
                        .find(|child| child.kind() == "type_bound")
                        .and_then(|bound| bound.named_child(0))
                        .and_then(|bound| type_name(bound, source));
                }
            }
        }
        owner = owner.parent()?;
    }
}

fn generic_receiver(node: Node<'_>, owner: Node<'_>, source: &str) -> Option<JavaReceiver> {
    generic_receiver_at(node, owner, source, 0, false)
}

fn generic_receiver_at(
    node: Node<'_>,
    owner: Node<'_>,
    source: &str,
    depth: usize,
    preserve_parameters: bool,
) -> Option<JavaReceiver> {
    if depth >= 16 {
        return None;
    }
    if node.kind() == "array_type" {
        let element = node
            .child_by_field_name("element")
            .or_else(|| node.named_child(0));
        return Some(JavaReceiver::Array(
            element
                .and_then(|element| type_name(element, source))
                .filter(|path| {
                    !type_parameter(owner, path.split("::").next().unwrap_or_default(), source)
                }),
        ));
    }
    if node.kind() != "generic_type" {
        return type_name(node, source)
            .filter(|path| type_parameter(owner, path, source))
            .map(|parameter| {
                if preserve_parameters {
                    return JavaReceiver::Parameter(parameter);
                }
                type_parameter_bound(owner, &parameter, source)
                    .map(JavaReceiver::Type)
                    .unwrap_or(JavaReceiver::Parameter(parameter))
            });
    }
    let path = type_name(node, source)?;
    let arguments = node.named_child(node.named_child_count().saturating_sub(1) as u32)?;
    if arguments.kind() != "type_arguments" {
        return None;
    }
    let mut cursor = arguments.walk();
    let arguments = arguments
        .named_children(&mut cursor)
        .map(|argument| {
            if argument.kind() == "wildcard" {
                return None;
            }
            generic_receiver_at(argument, owner, source, depth + 1, preserve_parameters).or_else(
                || {
                    type_name(argument, source)
                        .filter(|path| {
                            !type_parameter(
                                owner,
                                path.split("::").next().unwrap_or_default(),
                                source,
                            )
                        })
                        .map(JavaReceiver::Type)
                },
            )
        })
        .collect();
    Some(JavaReceiver::Parameterized { path, arguments })
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

fn parameter_type(parameter: Node<'_>) -> Option<Node<'_>> {
    parameter.child_by_field_name("type").or_else(|| {
        if parameter.kind() != "spread_parameter" {
            return None;
        }
        // Java's grammar gives a spread parameter an unnamed element type.
        let mut cursor = parameter.walk();
        let ty = parameter.named_children(&mut cursor).find(|child| {
            matches!(
                child.kind(),
                "integral_type"
                    | "floating_point_type"
                    | "boolean_type"
                    | "type_identifier"
                    | "scoped_type_identifier"
                    | "generic_type"
                    | "array_type"
            )
        });
        ty
    })
}

/// Check explicit SAM targets and guarded JDK callback input projections.
fn method_reference_context(
    node: Node<'_>,
    owner: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
) -> Option<JavaReceiver> {
    let mut expression = node;
    loop {
        let parent = expression.parent()?;
        match parent.kind() {
            "parenthesized_expression" => expression = parent,
            "variable_declarator" => {
                let declaration = parent.parent()?;
                let ty = declaration.child_by_field_name("type")?;
                return generic_receiver(ty, declaration, source)
                    .or_else(|| type_name(ty, source).map(JavaReceiver::Type));
            }
            "cast_expression" => {
                let ty = parent.child_by_field_name("type")?;
                return generic_receiver(ty, parent, source)
                    .or_else(|| type_name(ty, source).map(JavaReceiver::Type));
            }
            "return_statement" => {
                let owner = callable(parent)?;
                let ty = owner.child_by_field_name("type")?;
                return generic_receiver(ty, owner, source)
                    .or_else(|| type_name(ty, source).map(JavaReceiver::Type));
            }
            "argument_list" => {
                let constant = parent.parent()?;
                if constant.kind() == "method_invocation" {
                    let method = text(constant.child_by_field_name("name")?, source);
                    let object = constant.child_by_field_name("object")?;
                    let mut cursor = parent.walk();
                    let arguments: Vec<_> = parent
                        .named_children(&mut cursor)
                        .filter(|child| !child.is_extra())
                        .collect();
                    let future = matches!(
                        method,
                        "thenApply"
                            | "thenCompose"
                            | "thenAccept"
                            | "thenApplyAsync"
                            | "thenComposeAsync"
                            | "thenAcceptAsync"
                    );
                    let arity = arguments.len() == 1
                        || future && method.ends_with("Async") && arguments.len() == 2;
                    if !arity
                        || arguments.first()?.id() != expression.id()
                        || !(future
                            || matches!(method, "map" | "flatMap" | "forEach" | "ifPresent"))
                    {
                        return None;
                    }
                    let function = if matches!(
                        method,
                        "forEach" | "ifPresent" | "thenAccept" | "thenAcceptAsync"
                    ) {
                        "Consumer"
                    } else {
                        "Function"
                    };
                    return Some(JavaReceiver::Parameterized {
                        path: format!("java::util::function::{function}"),
                        arguments: vec![
                            Some(JavaReceiver::Element {
                                receiver: Box::new(expression_receiver(
                                    object, owner, source, scopes, 0,
                                )),
                                operation: if future {
                                    "future-element"
                                } else if method == "forEach" {
                                    "forEach"
                                } else {
                                    "stream-element"
                                }
                                .to_owned(),
                            }),
                            None,
                        ],
                    });
                }
                if constant.kind() != "enum_constant" {
                    return None;
                }
                let mut cursor = parent.walk();
                let arguments: Vec<_> = parent
                    .named_children(&mut cursor)
                    .filter(|child| !child.is_extra())
                    .collect();
                let index = arguments
                    .iter()
                    .position(|child| child.id() == expression.id())?;
                let body = constant.parent()?;
                let mut cursor = body.walk();
                let mut context = None;
                for constructor in body
                    .named_children(&mut cursor)
                    .filter(|child| child.kind() == "enum_body_declarations")
                    .flat_map(|declarations| {
                        let mut cursor = declarations.walk();
                        declarations.named_children(&mut cursor).collect::<Vec<_>>()
                    })
                    .filter(|child| child.kind() == "constructor_declaration")
                {
                    let parameters = constructor.child_by_field_name("parameters")?;
                    let mut cursor = parameters.walk();
                    let parameters: Vec<_> = parameters
                        .named_children(&mut cursor)
                        .filter(|child| child.kind() == "formal_parameter")
                        .collect();
                    if parameters.len() != arguments.len() {
                        continue;
                    }
                    let ty = parameters[index].child_by_field_name("type")?;
                    let candidate = generic_receiver(ty, constructor, source)
                        .or_else(|| type_name(ty, source).map(JavaReceiver::Type))?;
                    if context
                        .as_ref()
                        .is_some_and(|previous| *previous != candidate)
                    {
                        return None;
                    }
                    context = Some(candidate);
                }
                return context;
            }
            _ => return None,
        }
    }
}

/// Check a reference qualifier without confusing a declared value with a type.
fn method_reference_type(
    node: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
    depth: usize,
) -> Option<String> {
    if depth >= 16 {
        return None;
    }
    match node.kind() {
        "type_identifier" | "scoped_type_identifier" | "generic_type" => type_name(node, source),
        "identifier"
            if variable_type(node, text(node, source), false, scopes, source).is_none()
                && variable_inferred(node, text(node, source), false, scopes, source).is_none() =>
        {
            Some(text(node, source).to_owned())
        }
        "field_access" => {
            let prefix = method_reference_type(
                node.child_by_field_name("object")?,
                source,
                scopes,
                depth + 1,
            )?;
            let name = text(node.child_by_field_name("field")?, source);
            Some(format!("{prefix}::{name}"))
        }
        _ => None,
    }
}

fn class_literal_arguments(node: Node<'_>, source: &str) -> Vec<Option<String>> {
    let Some(arguments) = node.child_by_field_name("arguments") else {
        return Vec::new();
    };
    let mut cursor = arguments.walk();
    arguments
        .named_children(&mut cursor)
        .filter(|node| !node.is_extra())
        .map(|node| {
            (node.kind() == "class_literal")
                .then(|| node.named_child(0).and_then(|ty| type_name(ty, source)))
                .flatten()
        })
        .collect()
}

fn creation_argument_types(
    node: Node<'_>,
    owner: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
) -> Vec<Option<String>> {
    let Some(arguments) = node.child_by_field_name("arguments") else {
        return Vec::new();
    };
    let mut cursor = arguments.walk();
    arguments
        .named_children(&mut cursor)
        .filter(|argument| !argument.is_extra())
        .map(|argument| match argument.kind() {
            "string_literal" => Some("java::lang::String".to_owned()),
            "decimal_integer_literal"
            | "hex_integer_literal"
            | "octal_integer_literal"
            | "binary_integer_literal" => Some(
                if text(argument, source).ends_with(['L', 'l']) {
                    "long"
                } else {
                    "int"
                }
                .to_owned(),
            ),
            "true" | "false" => Some("boolean".to_owned()),
            "character_literal" => Some("char".to_owned()),
            "decimal_floating_point_literal" | "hex_floating_point_literal" => Some(
                if text(argument, source).ends_with(['F', 'f']) {
                    "float"
                } else {
                    "double"
                }
                .to_owned(),
            ),
            "null_literal" => Some("null".to_owned()),
            // Receiver inference retains declaration-site wrappers for named
            // values. Use invocation types so known constructor arguments do
            // not become unknown merely because their lexical scope is kept.
            _ => invocation_argument_type(argument, owner, source, scopes, 0),
        })
        .collect()
}

fn invocation_type(node: Node<'_>, source: &str) -> Option<String> {
    if node.has_error() {
        return None;
    }
    if node.kind() == "array_type" {
        let element = invocation_type(node.child_by_field_name("element")?, source)?;
        let dimensions = node.child_by_field_name("dimensions")?;
        return Some(format!(
            "{element}{}",
            "[]".repeat(array_dimensions(dimensions))
        ));
    }
    // Outer<A>.Inner<B> cannot be represented by erasing the outer arguments.
    let mut generic_qualifier = false;
    walk_tree_preorder(&node, |child| {
        if child.kind() == "type_arguments" {
            return WalkControl::SkipChildren;
        }
        generic_qualifier |= child.id() != node.id() && child.kind() == "generic_type";
        WalkControl::Continue
    });
    if generic_qualifier {
        return None;
    }
    if node.kind() == "generic_type" {
        let mut cursor = node.walk();
        let arguments = node
            .named_children(&mut cursor)
            .find(|child| child.kind() == "type_arguments")?;
        let mut cursor = arguments.walk();
        let arguments: Option<Vec<_>> = arguments
            .named_children(&mut cursor)
            .filter(|child| !child.is_extra())
            .map(|child| invocation_type(child, source))
            .collect();
        let arguments = arguments?;
        // Diamond inference and wildcard capture need semantic evidence.
        if arguments.is_empty() {
            return None;
        }
        return Some(format!(
            "{}<{}>",
            type_name(node, source)?,
            arguments.join(",")
        ));
    }
    if node.kind() == "wildcard" {
        return (text(node, source).trim() == "?").then(|| "?".to_owned());
    }
    type_name(node, source)
}

fn invocation_has_type_parameter(ty: Node<'_>, owner: Node<'_>, source: &str) -> bool {
    let mut found = false;
    walk_tree_preorder(&ty, |child| {
        found |=
            child.kind() == "type_identifier" && type_parameter(owner, text(child, source), source);
        WalkControl::Continue
    });
    found
}

fn array_dimensions(node: Node<'_>) -> usize {
    let mut cursor = node.walk();
    node.children(&mut cursor)
        .filter(|child| child.kind() == "[")
        .count()
}

fn declared_invocation_type(ty: Node<'_>, declaration: Node<'_>, source: &str) -> Option<String> {
    if invocation_has_type_parameter(ty, declaration, source) {
        return None;
    }
    let mut ty = invocation_type(ty, source)?;
    if ty == "var" {
        return None;
    }
    let mut cursor = declaration.walk();
    let dimensions: usize = declaration
        .named_children(&mut cursor)
        .filter(|child| child.kind() == "dimensions")
        .map(array_dimensions)
        .sum();
    ty.push_str(&"[]".repeat(dimensions + usize::from(declaration.kind() == "spread_parameter")));
    Some(ty)
}

fn variable_invocation_type(
    call: Node<'_>,
    name: &str,
    fields_only: bool,
    scopes: &VariableScopes,
    source: &str,
) -> Option<Option<String>> {
    let mut ancestor = Some(call);
    let mut fields_blocked = false;
    while let Some(node) = ancestor {
        if !fields_only && node.kind() == "lambda_expression" {
            if let Some(parameters) = node.child_by_field_name("parameters") {
                let mut shadowed = false;
                let mut typed = None;
                walk_tree_preorder(&parameters, |binding| {
                    shadowed |= binding.kind() == "identifier" && text(binding, source) == name;
                    if binding.kind() == "formal_parameter"
                        && binding
                            .child_by_field_name("name")
                            .is_some_and(|identifier| text(identifier, source) == name)
                    {
                        typed = binding
                            .child_by_field_name("type")
                            .and_then(|ty| declared_invocation_type(ty, binding, source));
                    }
                    WalkControl::Continue
                });
                if shadowed {
                    return Some(typed);
                }
            }
        }
        if let Some(bindings) = scopes.get(&node.id()).and_then(|scope| scope.get(name)) {
            if let Some(binding) = bindings
                .iter()
                .filter(|binding| {
                    (!fields_only || binding.field)
                        && (!fields_blocked || !binding.field)
                        && (binding.field || binding.position < call.start_byte())
                })
                .max_by_key(|binding| binding.position)
            {
                return Some(binding.invocation_type.clone());
            }
        }
        if blocks_enclosing_fields(node, source) {
            fields_blocked = true;
        }
        ancestor = node.parent();
    }
    None
}

/// Preserve argument types independently of receiver inference.
fn invocation_argument_type(
    node: Node<'_>,
    owner: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
    depth: usize,
) -> Option<String> {
    if depth >= 16 || node.has_error() {
        return None;
    }
    let recurse = |child| invocation_argument_type(child, owner, source, scopes, depth + 1);
    let promote = |ty: String| match ty.as_str() {
        "byte" | "short" | "char" => Some("int".to_owned()),
        "int" | "long" | "float" | "double" => Some(ty),
        _ => None,
    };
    if node.kind() == "identifier" {
        if let Some(ty) = variable_invocation_type(node, text(node, source), false, scopes, source)
        {
            return ty;
        }
    }
    if node.kind() == "field_access"
        && node
            .child_by_field_name("object")
            .is_some_and(|object| object.kind() == "this")
    {
        if let Some(ty) = node.child_by_field_name("field").and_then(|field| {
            variable_invocation_type(node, text(field, source), true, scopes, source)
        }) {
            return ty;
        }
    }
    match node.kind() {
        "string_literal" => Some("java::lang::String".to_owned()),
        "decimal_integer_literal"
        | "hex_integer_literal"
        | "octal_integer_literal"
        | "binary_integer_literal" => Some(
            if text(node, source).ends_with(['L', 'l']) {
                "long"
            } else {
                "int"
            }
            .to_owned(),
        ),
        "decimal_floating_point_literal" | "hex_floating_point_literal" => Some(
            if text(node, source).ends_with(['F', 'f']) {
                "float"
            } else {
                "double"
            }
            .to_owned(),
        ),
        "true" | "false" => Some("boolean".to_owned()),
        "character_literal" => Some("char".to_owned()),
        "null_literal" => Some("null".to_owned()),
        "parenthesized_expression" => recurse(node.named_child(0)?),
        "array_access" => recurse(node.child_by_field_name("array")?)?
            .strip_suffix("[]")
            .map(str::to_owned),
        "cast_expression" | "object_creation_expression" => {
            let ty = node.child_by_field_name("type")?;
            (!invocation_has_type_parameter(ty, owner, source))
                .then(|| invocation_type(ty, source))
                .flatten()
        }
        "array_creation_expression" => {
            let element = invocation_type(node.child_by_field_name("type")?, source)?;
            let mut cursor = node.walk();
            let count: usize = node
                .named_children(&mut cursor)
                .map(|child| match child.kind() {
                    "dimensions_expr" => 1,
                    "dimensions" => array_dimensions(child),
                    _ => 0,
                })
                .sum();
            (count > 0).then(|| format!("{element}{}", "[]".repeat(count)))
        }
        "unary_expression" => {
            let operator = text(node.child_by_field_name("operator")?, source);
            let ty = recurse(node.child_by_field_name("operand")?)?;
            match operator {
                "+" | "-" | "~" => promote(ty),
                "!" if ty == "boolean" => Some(ty),
                _ => None,
            }
        }
        "binary_expression" => {
            let left = recurse(node.child_by_field_name("left")?)?;
            let right = recurse(node.child_by_field_name("right")?)?;
            let operator = text(node.child_by_field_name("operator")?, source);
            if operator == "+" && (left == "java::lang::String" || right == "java::lang::String") {
                return Some("java::lang::String".to_owned());
            }
            if matches!(
                operator,
                "==" | "!=" | "<" | ">" | "<=" | ">=" | "&&" | "||"
            ) {
                return Some("boolean".to_owned());
            }
            if left == "boolean" && right == "boolean" && matches!(operator, "&" | "|" | "^") {
                return Some(left);
            }
            let (left, right) = (promote(left)?, promote(right)?);
            if matches!(operator, "<<" | ">>" | ">>>") {
                return Some(left);
            }
            if !matches!(operator, "+" | "-" | "*" | "/" | "%" | "&" | "|" | "^") {
                return None;
            }
            ["double", "float", "long", "int"]
                .into_iter()
                .find(|ty| left == *ty || right == *ty)
                .map(str::to_owned)
        }
        _ => match expression_receiver(node, owner, source, scopes, 0) {
            JavaReceiver::Type(ty) => Some(ty),
            JavaReceiver::Array(Some(element)) => Some(format!("{element}[]")),
            _ => None,
        },
    }
}

fn identity_projection(node: Node<'_>, source: &str) -> bool {
    if node.kind() != "lambda_expression" {
        return false;
    }
    let (Some(parameters), Some(body)) = (
        node.child_by_field_name("parameters"),
        node.child_by_field_name("body"),
    ) else {
        return false;
    };
    let parameter = if parameters.kind() == "identifier" {
        Some(parameters)
    } else if parameters.named_child_count() == 1 {
        parameters
            .named_child(0)
            .map(|parameter| parameter.child_by_field_name("name").unwrap_or(parameter))
    } else {
        None
    };
    parameter.is_some_and(|parameter| {
        body.kind() == "identifier" && text(parameter, source) == text(body, source)
    })
}

fn captured_field(
    node: Node<'_>,
    name: &str,
    scopes: &VariableScopes,
    source: &str,
) -> Option<JavaReceiver> {
    let mut ancestor = node.parent();
    let mut boundary = None;
    while let Some(scope) = ancestor {
        if let Some(bindings) = scopes.get(&scope.id()).and_then(|scope| scope.get(name)) {
            if let Some(binding) = bindings
                .iter()
                .filter(|binding| binding.field || binding.position < node.start_byte())
                .max_by_key(|binding| binding.position)
            {
                if !binding.field {
                    return None;
                }
                let value = binding
                    .inferred
                    .clone()
                    .or_else(|| binding.declared.clone().map(JavaReceiver::Type))?;
                return Some(JavaReceiver::CapturedField {
                    value: Box::new(value),
                    name: name.to_owned(),
                    boundary: boundary?,
                });
            }
        }
        if let Some(creation) = scope.parent().filter(|parent| {
            scope.kind() == "class_body" && parent.kind() == "object_creation_expression"
        }) {
            boundary = creation
                .child_by_field_name("type")
                .and_then(|ty| type_name(ty, source));
        } else if blocks_enclosing_fields(scope, source) {
            return None;
        }
        ancestor = scope.parent();
    }
    None
}

fn is_static(node: Node<'_>, source: &str) -> bool {
    let mut cursor = node.walk();
    let found = node.named_children(&mut cursor).any(|child| {
        child.kind() == "modifiers"
            && text(child, source)
                .split_whitespace()
                .any(|word| word == "static")
    });
    found
}

fn instance_context(mut node: Node<'_>, source: &str) -> bool {
    loop {
        if node.kind() == "static_initializer"
            || matches!(node.kind(), "method_declaration" | "field_declaration")
                && is_static(node, source)
        {
            return false;
        }
        if matches!(node.kind(), "class_body" | "interface_body" | "enum_body") {
            return true;
        }
        let Some(parent) = node.parent() else {
            return false;
        };
        node = parent;
    }
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
        name.and_then(|name| {
            if type_parameter(owner, name.split("::").next().unwrap_or_default(), source) {
                type_parameter_bound(owner, &name, source)
            } else {
                Some(name)
            }
        })
        .map(JavaReceiver::Type)
        .unwrap_or(JavaReceiver::Unknown)
    };
    match node.kind() {
        "this" => {
            if instance_context(node, source) {
                JavaReceiver::This
            } else {
                JavaReceiver::Unknown
            }
        }
        "super" => {
            if instance_context(node, source) {
                JavaReceiver::Super
            } else {
                JavaReceiver::Unknown
            }
        }
        "identifier" => {
            let mut ancestor = node.parent();
            while let Some(scope) = ancestor {
                if scope.id() == owner.id() {
                    break;
                }
                if scope.kind() == "lambda_expression"
                    && scope
                        .child_by_field_name("parameters")
                        .is_some_and(|parameters| {
                            let mut shadowed = false;
                            walk_tree_preorder(&parameters, |binding| {
                                shadowed |= binding.kind() == "identifier"
                                    && text(binding, source) == text(node, source);
                                WalkControl::Continue
                            });
                            shadowed
                        })
                {
                    let parameters = scope.child_by_field_name("parameters");
                    let mut bindings = Vec::new();
                    if let Some(parameters) = parameters {
                        if parameters.kind() == "identifier" {
                            bindings.push(parameters);
                        } else {
                            let mut cursor = parameters.walk();
                            bindings.extend(parameters.named_children(&mut cursor).filter_map(
                                |parameter| {
                                    if parameter.kind() == "identifier" {
                                        Some(parameter)
                                    } else {
                                        parameter.child_by_field_name("name")
                                    }
                                },
                            ));
                        }
                    }
                    let Some(index) = bindings
                        .iter()
                        .position(|binding| text(*binding, source) == text(node, source))
                    else {
                        return JavaReceiver::Unknown;
                    };
                    if let Some(ty) = bindings[index]
                        .parent()
                        .filter(|parent| parent.kind() == "formal_parameter")
                        .and_then(|parameter| parameter.child_by_field_name("type"))
                    {
                        return generic_receiver(ty, owner, source)
                            .unwrap_or_else(|| typed(type_name(ty, source)));
                    }
                    let mut parent = scope.parent();
                    while let Some(node) = parent {
                        if node.kind() != "argument_list" {
                            break;
                        }
                        parent = node.parent();
                    }
                    if let Some(creation) =
                        parent.filter(|parent| parent.kind() == "object_creation_expression")
                    {
                        if let (Some(ty), Some(arguments)) = (
                            creation.child_by_field_name("type"),
                            creation.child_by_field_name("arguments"),
                        ) {
                            let mut cursor = arguments.walk();
                            let values: Vec<_> = arguments
                                .named_children(&mut cursor)
                                .filter(|n| !n.is_extra())
                                .collect();
                            if let (Some(path), Some(parameter)) = (
                                type_name(ty, source),
                                values.iter().position(|value| value.id() == scope.id()),
                            ) {
                                let name = path.rsplit("::").next().unwrap_or(&path).to_owned();
                                return JavaReceiver::Callback {
                                    receiver: Some(Box::new(
                                        generic_receiver(ty, owner, source)
                                            .unwrap_or(JavaReceiver::Type(path)),
                                    )),
                                    name,
                                    line: ty.start_position().row as i64 + 1,
                                    arguments: values.len(),
                                    parameter,
                                    input: index,
                                    class_literals: class_literal_arguments(creation, source),
                                    context: None,
                                };
                            }
                        }
                    }
                    if let Some(invocation) =
                        parent.filter(|parent| parent.kind() == "method_invocation")
                    {
                        if invocation.child_by_field_name("object").is_none() {
                            if let (Some(name), Some(arguments)) = (
                                invocation.child_by_field_name("name"),
                                invocation.child_by_field_name("arguments"),
                            ) {
                                let mut cursor = arguments.walk();
                                let values: Vec<_> = arguments
                                    .named_children(&mut cursor)
                                    .filter(|n| !n.is_extra())
                                    .collect();
                                if let Some(parameter) =
                                    values.iter().position(|value| value.id() == scope.id())
                                {
                                    return JavaReceiver::Callback {
                                        receiver: None,
                                        name: text(name, source).to_owned(),
                                        line: name.start_position().row as i64 + 1,
                                        arguments: values.len(),
                                        parameter,
                                        input: index,
                                        class_literals: class_literal_arguments(invocation, source),
                                        context: None,
                                    };
                                }
                            }
                        }
                        if let (Some(object), Some(method)) = (
                            invocation.child_by_field_name("object"),
                            invocation.child_by_field_name("name"),
                        ) {
                            if matches!(text(method, source), "toMap" | "toUnmodifiableMap")
                                && index <= 1
                            {
                                if let Some(collect) = invocation
                                    .parent()
                                    .and_then(|args| args.parent())
                                    .filter(|collect| {
                                        collect.kind() == "method_invocation"
                                            && collect
                                                .child_by_field_name("name")
                                                .is_some_and(|name| text(name, source) == "collect")
                                    })
                                {
                                    return JavaReceiver::Element {
                                        receiver: Box::new(expression_receiver(
                                            collect
                                                .child_by_field_name("object")
                                                .unwrap_or(collect),
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        )),
                                        operation: "stream-element".to_owned(),
                                    };
                                }
                            }
                            if text(method, source) == "forEach"
                                && bindings.len() == 2
                                && index <= 1
                            {
                                return JavaReceiver::Element {
                                    receiver: Box::new(expression_receiver(
                                        object,
                                        owner,
                                        source,
                                        scopes,
                                        depth + 1,
                                    )),
                                    operation: if index == 0 { "map-key" } else { "map-value" }
                                        .to_owned(),
                                };
                            }
                            if matches!(text(method, source), "max" | "min" | "sorted")
                                && index <= 1
                            {
                                return JavaReceiver::Element {
                                    receiver: Box::new(expression_receiver(
                                        object,
                                        owner,
                                        source,
                                        scopes,
                                        depth + 1,
                                    )),
                                    operation: "stream-element".to_owned(),
                                };
                            }
                            if index > 0 {
                                return JavaReceiver::Unknown;
                            }
                            if !matches!(
                                text(method, source),
                                "forEach"
                                    | "map"
                                    | "filter"
                                    | "flatMap"
                                    | "anyMatch"
                                    | "allMatch"
                                    | "noneMatch"
                                    | "peek"
                                    | "sorted"
                                    | "ifPresent"
                                    | "ifPresentOrElse"
                                    | "thenAccept"
                                    | "thenApply"
                                    | "thenCompose"
                                    | "thenAcceptAsync"
                                    | "thenApplyAsync"
                                    | "thenComposeAsync"
                                    | "whenComplete"
                                    | "whenCompleteAsync"
                                    | "handle"
                                    | "handleAsync"
                            ) {
                                if let Some(arguments) = invocation.child_by_field_name("arguments")
                                {
                                    let mut cursor = arguments.walk();
                                    let values: Vec<_> = arguments
                                        .named_children(&mut cursor)
                                        .filter(|n| !n.is_extra())
                                        .collect();
                                    if let Some(parameter) =
                                        values.iter().position(|value| value.id() == scope.id())
                                    {
                                        return JavaReceiver::Callback {
                                            receiver: Some(Box::new(expression_receiver(
                                                object,
                                                owner,
                                                source,
                                                scopes,
                                                depth + 1,
                                            ))),
                                            name: text(method, source).to_owned(),
                                            line: method.start_position().row as i64 + 1,
                                            arguments: values.len(),
                                            parameter,
                                            input: index,
                                            class_literals: class_literal_arguments(
                                                invocation, source,
                                            ),
                                            context: invocation
                                                .parent()
                                                .and_then(|args| args.parent())
                                                .filter(|outer| {
                                                    outer.kind() == "method_invocation"
                                                        && outer
                                                            .child_by_field_name("name")
                                                            .is_some_and(|name| {
                                                                matches!(
                                                                    text(name, source),
                                                                    "max" | "min" | "sorted"
                                                                )
                                                            })
                                                })
                                                .and_then(|outer| {
                                                    outer.child_by_field_name("object")
                                                })
                                                .map(|object| {
                                                    Box::new(expression_receiver(
                                                        object,
                                                        owner,
                                                        source,
                                                        scopes,
                                                        depth + 1,
                                                    ))
                                                }),
                                        };
                                    }
                                }
                                return JavaReceiver::Unknown;
                            }
                            let mut object = object;
                            while object.kind() == "method_invocation"
                                && object.child_by_field_name("name").is_some_and(|name| {
                                    matches!(
                                        text(name, source),
                                        "filter"
                                            | "peek"
                                            | "sorted"
                                            | "limit"
                                            | "skip"
                                            | "distinct"
                                    )
                                })
                            {
                                let Some(base) = object.child_by_field_name("object") else {
                                    break;
                                };
                                object = base;
                            }
                            let receiver =
                                expression_receiver(object, owner, source, scopes, depth + 1);
                            let stream = object
                                .child_by_field_name("name")
                                .is_some_and(|name| text(name, source) == "stream")
                                || matches!(&receiver, JavaReceiver::Parameterized { path, .. } if path.rsplit("::").next() == Some("Stream"));
                            let operation = text(method, source);
                            return JavaReceiver::Element {
                                receiver: Box::new(receiver),
                                operation:
                                    if matches!(operation, "ifPresent" | "ifPresentOrElse") {
                                        "optional-element"
                                    } else if matches!(
                                        operation,
                                        "thenAccept"
                                            | "thenApply"
                                            | "thenCompose"
                                            | "thenAcceptAsync"
                                            | "thenApplyAsync"
                                            | "thenComposeAsync"
                                            | "whenComplete"
                                            | "whenCompleteAsync"
                                            | "handle"
                                            | "handleAsync"
                                    ) {
                                        "future-element"
                                    } else if operation != "forEach" || stream {
                                        "stream-element"
                                    } else {
                                        operation
                                    }
                                    .to_owned(),
                            };
                        }
                    }
                    return JavaReceiver::Unknown;
                }
                ancestor = scope.parent();
            }
            if let Some(capture) = captured_field(node, text(node, source), scopes, source) {
                return capture;
            }
            if let Some(receiver) =
                variable_receiver(node, text(node, source), false, scopes, source)
            {
                return receiver;
            }
            if let Some(parameters) = owner.child_by_field_name("parameters") {
                let mut cursor = parameters.walk();
                for parameter in parameters.named_children(&mut cursor) {
                    if parameter
                        .child_by_field_name("name")
                        .is_some_and(|name| text(name, source) == text(node, source))
                    {
                        let mut children = parameter.walk();
                        if parameter
                            .named_children(&mut children)
                            .any(|child| child.kind() == "dimensions")
                        {
                            return JavaReceiver::Array(
                                parameter
                                    .child_by_field_name("type")
                                    .and_then(|ty| type_name(ty, source)),
                            );
                        }
                        if let Some(receiver) = parameter
                            .child_by_field_name("type")
                            .and_then(|ty| generic_receiver(ty, owner, source))
                        {
                            return receiver;
                        }
                        return typed(
                            parameter
                                .child_by_field_name("type")
                                .and_then(|ty| type_name(ty, source)),
                        );
                    }
                }
            }
            match variable_type(node, text(node, source), false, scopes, source) {
                Some(binding) => typed(binding.map(str::to_owned)),
                None => {
                    let fallback = typed(Some(text(node, source).to_owned()));
                    if !instance_context(node, source) {
                        return fallback;
                    }
                    let mut ancestor = node.parent();
                    while let Some(class) = ancestor {
                        if matches!(
                            class.kind(),
                            "class_declaration" | "enum_declaration" | "record_declaration"
                        ) {
                            if let Some(name) = class.child_by_field_name("name") {
                                return JavaReceiver::LexicalName {
                                    name: text(node, source).to_owned(),
                                    boundary: text(name, source).to_owned(),
                                };
                            }
                        }
                        ancestor = class.parent();
                    }
                    fallback
                }
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
            if !instance_context(node, source) {
                return JavaReceiver::Unknown;
            }
            if let Some(receiver) = node.child_by_field_name("field").and_then(|field| {
                variable_receiver(node, text(field, source), true, scopes, source)
            }) {
                return receiver;
            }
            let binding = node
                .child_by_field_name("field")
                .and_then(|field| variable_type(node, text(field, source), true, scopes, source));
            match binding {
                Some(binding) => typed(binding.map(str::to_owned)),
                None => JavaReceiver::Field {
                    receiver: Box::new(JavaReceiver::This),
                    name: node
                        .child_by_field_name("field")
                        .map(|field| text(field, source).to_owned())
                        .unwrap_or_default(),
                },
            }
        }
        "field_access" => match (
            node.child_by_field_name("object"),
            node.child_by_field_name("field"),
        ) {
            (Some(object), Some(field)) => JavaReceiver::Field {
                receiver: Box::new(expression_receiver(
                    object,
                    owner,
                    source,
                    scopes,
                    depth + 1,
                )),
                name: text(field, source).to_owned(),
            },
            _ => JavaReceiver::Unknown,
        },
        "array_access" => JavaReceiver::Element {
            receiver: Box::new(
                node.child_by_field_name("array")
                    .map(|array| expression_receiver(array, owner, source, scopes, depth + 1))
                    .unwrap_or(JavaReceiver::Unknown),
            ),
            operation: "array-index".to_owned(),
        },
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
            if text(name, source) == "of" {
                if let (Some(object), Some(values)) = (
                    node.child_by_field_name("object"),
                    node.child_by_field_name("arguments"),
                ) {
                    let qualifier = expression_receiver(object, owner, source, scopes, depth + 1);
                    let nominal = match &qualifier {
                        JavaReceiver::CapturedField { value, .. } => value.as_ref(),
                        value => value,
                    };
                    if matches!(nominal, JavaReceiver::Type(path)
                        | JavaReceiver::LexicalName { name: path, .. }
                        if path == "Stream" || path == "java::util::stream::Stream")
                    {
                        let mut cursor = values.walk();
                        let elements: Vec<_> = values
                            .named_children(&mut cursor)
                            .filter(|value| !value.is_extra())
                            .map(|value| {
                                expression_receiver(value, owner, source, scopes, depth + 1)
                            })
                            .collect();
                        let invocation = JavaReceiver::Invocation {
                            receiver: Some(Box::new(qualifier.clone())),
                            name: "of".to_owned(),
                            line: name.start_position().row as i64 + 1,
                            arguments,
                            first_argument: elements.first().cloned().map(Box::new),
                        };
                        return JavaReceiver::StreamFactory {
                            qualifier: Box::new(qualifier),
                            elements,
                            invocation: Box::new(invocation),
                        };
                    }
                }
            }
            if text(name, source) == "collect" && arguments == 1 {
                if let (Some(stream), Some(values)) = (
                    node.child_by_field_name("object"),
                    node.child_by_field_name("arguments"),
                ) {
                    if let Some(collector) = values.named_child(0) {
                        if collector.kind() == "method_invocation"
                            && collector.child_by_field_name("name").is_some_and(|name| {
                                matches!(text(name, source), "toMap" | "toUnmodifiableMap")
                            })
                        {
                            if let (Some(object), Some(inputs)) = (
                                collector.child_by_field_name("object"),
                                collector.child_by_field_name("arguments"),
                            ) {
                                let mut cursor = inputs.walk();
                                let inputs: Vec<_> = inputs
                                    .named_children(&mut cursor)
                                    .filter(|input| !input.is_extra())
                                    .collect();
                                if matches!(inputs.len(), 2..=4) {
                                    let stream = expression_receiver(
                                        stream,
                                        owner,
                                        source,
                                        scopes,
                                        depth + 1,
                                    );
                                    return JavaReceiver::CollectedMap {
                                        collector: Box::new(expression_receiver(
                                            object,
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        )),
                                        key: Box::new(collector_projection(
                                            inputs[0],
                                            &stream,
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        )),
                                        value: Box::new(collector_projection(
                                            inputs[1],
                                            &stream,
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        )),
                                        stream: Box::new(stream),
                                    };
                                }
                            }
                        }
                    }
                }
            }
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
                first_argument: node.child_by_field_name("arguments").and_then(|arguments| {
                    let mut cursor = arguments.walk();
                    let first = arguments
                        .named_children(&mut cursor)
                        .find(|argument| !argument.is_extra());
                    first.map(|argument| {
                        Box::new(
                            if text(name, source) == "map" && argument_count(node) == Some(1) {
                                if identity_projection(argument, source) {
                                    return Box::new(JavaReceiver::Identity);
                                }
                                node.child_by_field_name("object")
                                    .map(|object| {
                                        let stream = expression_receiver(
                                            object,
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        );
                                        collector_projection(
                                            argument,
                                            &stream,
                                            owner,
                                            source,
                                            scopes,
                                            depth + 1,
                                        )
                                    })
                                    .unwrap_or(JavaReceiver::Unknown)
                            } else {
                                expression_receiver(argument, owner, source, scopes, depth + 1)
                            },
                        )
                    })
                }),
            }
        }
        _ => JavaReceiver::Unknown,
    }
}

/// Check the source form of a map collector's single-input projection.
fn collector_projection(
    node: Node<'_>,
    stream: &JavaReceiver,
    owner: Node<'_>,
    source: &str,
    scopes: &VariableScopes,
    depth: usize,
) -> JavaReceiver {
    let element = || JavaReceiver::Element {
        receiver: Box::new(stream.clone()),
        operation: "stream-element".to_owned(),
    };
    if node.kind() == "method_reference" {
        if let Some(name) = node.named_child(node.named_child_count().saturating_sub(1) as u32) {
            if name.kind() == "identifier" {
                if let Some(qualifier) = node.named_child(0) {
                    return JavaReceiver::MethodProjection {
                        input: Box::new(element()),
                        receiver: Box::new(expression_receiver(
                            qualifier,
                            owner,
                            source,
                            scopes,
                            depth + 1,
                        )),
                        qualifier: method_reference_type(qualifier, source, scopes, 0),
                        name: text(name, source).to_owned(),
                        line: name.start_position().row as i64 + 1,
                    };
                }
            }
        }
    }
    if node.kind() == "lambda_expression" {
        if let (Some(parameters), Some(body)) = (
            node.child_by_field_name("parameters"),
            node.child_by_field_name("body"),
        ) {
            let parameter = if parameters.kind() == "identifier" {
                Some(parameters)
            } else {
                parameters.named_child(0)
            };
            if parameter.is_some_and(|parameter| {
                body.kind() == "identifier" && text(parameter, source) == text(body, source)
            }) {
                return element();
            }
            if matches!(
                body.kind(),
                "object_creation_expression" | "method_invocation"
            ) {
                return expression_receiver(body, owner, source, scopes, depth + 1);
            }
        }
    }
    JavaReceiver::Unknown
}

/// Value members reuse graph variable scopes and exact declaration sites.
/// Source result chains retain explicit class arguments and initializer scope;
/// Invocation argument types remain explicit; unknown inference stays unresolved.
pub(crate) struct DependencyValueMember {
    pub path: String,
    pub position: usize,
    pub contexts: Vec<String>,
    pub use_contexts: Vec<String>,
    pub name: String,
    pub method: bool,
    pub chain: Option<DependencyValueReceiver>,
    pub arguments: Option<Vec<DependencyInvocationArgument>>,
}

pub(crate) struct DependencyInvocationArgument {
    pub ty: Option<String>,
    pub position: usize,
    pub contexts: Vec<String>,
}

pub(crate) enum DependencyValueReceiver {
    Nominal {
        signature: crate::parsers::treesitter::java::DependencyResultType,
        position: usize,
        contexts: Vec<String>,
        instance: bool,
    },
    Lexical {
        instances: std::collections::BTreeSet<String>,
        explicit: bool,
    },
    Super,
    Initializer {
        receiver: Box<DependencyValueReceiver>,
        contexts: Vec<String>,
    },
    Member {
        receiver: Box<DependencyValueReceiver>,
        name: String,
        arity: Option<usize>,
        arguments: Option<Vec<DependencyInvocationArgument>>,
    },
}

pub(crate) fn dependency_value_members(
    source: &str,
    package: &str,
) -> Result<Vec<DependencyValueMember>> {
    use crate::parsers::treesitter::java::{dependency_contexts, dependency_instances};
    fn arguments(
        node: Node<'_>,
        source: &str,
        scopes: &VariableScopes,
        tree: &tree_sitter::Tree,
        declarations: &HashMap<usize, InvocationOwner>,
        package: &str,
    ) -> Option<Vec<DependencyInvocationArgument>> {
        let owner = callable(node).unwrap_or(node);
        Some(
            node.child_by_field_name("arguments")?
                .named_children(&mut node.child_by_field_name("arguments")?.walk())
                .filter(|child| !child.is_extra())
                .map(|argument| {
                    let position = expression_receiver_site(argument, source, scopes, declarations)
                        .map_or(argument.start_byte(), |site| site.position);
                    let binding = tree
                        .root_node()
                        .descendant_for_byte_range(position, position + 1)
                        .unwrap_or(argument);
                    DependencyInvocationArgument {
                        ty: invocation_argument_type(argument, owner, source, scopes, 0),
                        position,
                        contexts: dependency_contexts(binding, source, package),
                    }
                })
                .collect(),
        )
    }

    fn available(call: Node<'_>, ty: Node<'_>, source: &str) -> bool {
        let mut declaration = Some(ty);
        while let Some(node) = declaration {
            let record_component = node.kind() == "record_declaration"
                && node
                    .child_by_field_name("parameters")
                    .is_some_and(|parameters| parameters.byte_range().contains(&ty.start_byte()));
            if node.kind() == "field_declaration" || record_component {
                if !record_component
                    && (!node.child_by_field_name("type").is_some_and(|field_type| {
                        field_type.byte_range().contains(&ty.start_byte())
                    }) || is_static(node, source))
                {
                    return true;
                }
                let body = if record_component {
                    node.child_by_field_name("body")
                } else {
                    node.parent()
                }
                .map(|body| body.id());
                let mut context = Some(call);
                while let Some(node) = context {
                    if Some(node.id()) == body {
                        return true;
                    }
                    if node.kind() == "static_initializer"
                        || matches!(node.kind(), "method_declaration" | "field_declaration")
                            && is_static(node, source)
                    {
                        return false;
                    }
                    context = node.parent();
                }
                return false;
            }
            declaration = node.parent();
        }
        true
    }

    fn parameter_receiver(
        node: Node<'_>,
        source: &str,
        tree: &tree_sitter::Tree,
        scopes: &VariableScopes,
        package: &str,
    ) -> Option<DependencyValueReceiver> {
        let (name, fields_only) = match node.kind() {
            "identifier" => (text(node, source), false),
            "field_access" if node.child_by_field_name("object")?.kind() == "this" => {
                (text(node.child_by_field_name("field")?, source), true)
            }
            _ => return None,
        };
        let binding = variable_binding(node, name, fields_only, scopes, source)?;
        if binding.site.is_some() || binding.declared.is_none() {
            return None;
        }
        let name = tree
            .root_node()
            .descendant_for_byte_range(binding.position, binding.position + 1)?;
        let declaration = name.parent()?;
        let declaration = if declaration.kind() == "variable_declarator" {
            declaration.parent()?
        } else {
            declaration
        };
        let ty = declaration.child_by_field_name("type")?;
        let signature =
            crate::parsers::treesitter::java::dependency_result_type(ty, source, package, 0)?;
        if !matches!(
            signature,
            crate::parsers::treesitter::java::DependencyResultType::Parameter { .. }
        ) || !available(node, ty, source)
        {
            return None;
        }
        Some(DependencyValueReceiver::Nominal {
            signature,
            position: ty.start_byte(),
            contexts: dependency_contexts(ty, source, package),
            instance: true,
        })
    }
    fn chain_receiver(
        node: Node<'_>,
        source: &str,
        tree: &tree_sitter::Tree,
        scopes: &VariableScopes,
        declarations: &HashMap<usize, InvocationOwner>,
        package: &str,
        depth: usize,
    ) -> Option<DependencyValueReceiver> {
        if depth >= 16 || node.has_error() {
            return None;
        }
        if let Some(parameter) = parameter_receiver(node, source, tree, scopes, package) {
            return Some(parameter);
        }
        let owner = callable(node).unwrap_or(node);
        let inferred = expression_receiver(node, owner, source, scopes, 0);
        let site = expression_receiver_site(node, source, scopes, declarations);
        let nominal = match &inferred {
            JavaReceiver::Declared { receiver, site } => match receiver.as_ref() {
                JavaReceiver::Type(path)
                | JavaReceiver::Parameterized { path, .. }
                | JavaReceiver::Parameter(path) => Some((path, site.position)),
                _ => None,
            },
            JavaReceiver::Type(path)
            | JavaReceiver::Parameterized { path, .. }
            | JavaReceiver::Parameter(path) => site.as_ref().map(|site| (path, site.position)),
            _ => None,
        };
        if let Some((_, position)) = nominal {
            let mut ty = tree
                .root_node()
                .descendant_for_byte_range(position, position + 1)?;
            while let Some(parent) = ty.parent().filter(|parent| {
                parent.start_byte() == position
                    && matches!(parent.kind(), "generic_type" | "scoped_type_identifier")
            }) {
                ty = parent;
            }
            let signature =
                crate::parsers::treesitter::java::dependency_result_type(ty, source, package, 0)?;
            return available(node, ty, source).then(|| DependencyValueReceiver::Nominal {
                signature,
                position,
                contexts: dependency_contexts(ty, source, package),
                instance: true,
            });
        }
        let nested =
            |node| chain_receiver(node, source, tree, scopes, declarations, package, depth + 1);
        if node.kind() == "field_access" {
            if let Some(path) = method_reference_type(node, source, scopes, 0) {
                return Some(DependencyValueReceiver::Nominal {
                    signature: crate::parsers::treesitter::java::DependencyResultType::Named(
                        path.replace("::", "."),
                        Vec::new(),
                    ),
                    position: node.start_byte(),
                    contexts: dependency_contexts(node, source, package),
                    instance: false,
                });
            }
        }
        match node.kind() {
            "parenthesized_expression" => nested(node.named_child(0)?),
            "this" => instance_context(node, source).then(|| DependencyValueReceiver::Lexical {
                instances: dependency_instances(node, source, package),
                explicit: true,
            }),
            "super" => instance_context(node, source).then_some(DependencyValueReceiver::Super),
            "method_invocation" => Some(DependencyValueReceiver::Member {
                receiver: Box::new(match node.child_by_field_name("object") {
                    Some(object) => nested(object)?,
                    None => DependencyValueReceiver::Lexical {
                        instances: dependency_instances(node, source, package),
                        explicit: false,
                    },
                }),
                name: text(node.child_by_field_name("name")?, source).to_owned(),
                arity: Some(argument_count(node)?),
                arguments: arguments(node, source, scopes, tree, declarations, package),
            }),
            "field_access" => Some(DependencyValueReceiver::Member {
                receiver: Box::new(nested(node.child_by_field_name("object")?)?),
                name: text(node.child_by_field_name("field")?, source).to_owned(),
                arity: None,
                arguments: None,
            }),
            // An unbound syntax name is a type qualifier only when receiver
            // inference did not detect a value shadow or unknown binding.
            "identifier" | "scoped_identifier" => match inferred {
                JavaReceiver::Type(path) => Some(DependencyValueReceiver::Nominal {
                    signature: crate::parsers::treesitter::java::DependencyResultType::Named(
                        path.replace("::", "."),
                        Vec::new(),
                    ),
                    position: node.start_byte(),
                    contexts: dependency_contexts(node, source, package),
                    instance: false,
                }),
                _ => {
                    // Resolve an inferred local at its initializer, where every
                    // qualifier and local type had its original lexical binding.
                    let binding =
                        variable_binding(node, text(node, source), false, scopes, source)?;
                    if binding.declared.is_some() || binding.field {
                        return None;
                    }
                    let name = tree
                        .root_node()
                        .descendant_for_byte_range(binding.position, binding.position + 1)?;
                    let variable = name
                        .parent()
                        .filter(|parent| parent.kind() == "variable_declarator")?;
                    let initializer = variable.child_by_field_name("value")?;
                    Some(DependencyValueReceiver::Initializer {
                        receiver: Box::new(nested(initializer)?),
                        contexts: dependency_contexts(initializer, source, package),
                    })
                }
            },
            _ => None,
        }
    }
    let tree = parse_tree(source, &LANGUAGE)?;
    let declarations = callable_declarations(tree.root_node(), source);
    let scopes = variable_scopes(tree.root_node(), source, &declarations);
    let mut result = Vec::new();
    walk_tree_preorder(&tree.root_node(), |node| {
        let (object, member, method) = match node.kind() {
            "method_invocation" => (
                node.child_by_field_name("object"),
                node.child_by_field_name("name"),
                true,
            ),
            "field_access" => (
                node.child_by_field_name("object"),
                node.child_by_field_name("field"),
                false,
            ),
            "method_reference" => (node.named_child(0), node.named_child(1), true),
            _ => return WalkControl::Continue,
        };
        let (Some(object), Some(member)) = (object, member) else {
            return WalkControl::Continue;
        };
        let owner = callable(node).unwrap_or(node);
        let receiver = expression_receiver(object, owner, source, &scopes, 0);
        let site = expression_receiver_site(object, source, &scopes, &declarations);
        let nominal = match &receiver {
            JavaReceiver::Declared { receiver, site } => match receiver.as_ref() {
                JavaReceiver::Type(path) | JavaReceiver::Parameterized { path, .. } => {
                    Some((path.clone(), site.position))
                }
                _ => None,
            },
            JavaReceiver::Type(path) | JavaReceiver::Parameterized { path, .. } => {
                site.as_ref().map(|site| (path.clone(), site.position))
            }
            _ => None,
        };
        let inferred_local = object.kind() == "identifier"
            && variable_binding(object, text(object, source), false, &scopes, source)
                .is_some_and(|binding| !binding.field && binding.declared.is_none());
        let parameter = parameter_receiver(object, source, &tree, &scopes, package);
        if let Some((path, position)) = nominal {
            // Bind at the actual type node, not a later use in a nested callable.
            if let Some(ty) = tree
                .root_node()
                .descendant_for_byte_range(position, position + 1)
            {
                if !available(node, ty, source) {
                    return WalkControl::Continue;
                }
                result.push(DependencyValueMember {
                    path: path.replace("::", "."),
                    position,
                    contexts: dependency_contexts(ty, source, package),
                    use_contexts: dependency_contexts(node, source, package),
                    name: text(member, source).to_owned(),
                    method,
                    chain: None,
                    arguments: arguments(node, source, &scopes, &tree, &declarations, package),
                });
            }
        } else if let Some(chain) = parameter.or_else(|| {
            (matches!(
                object.kind(),
                "method_invocation" | "field_access" | "parenthesized_expression"
            ) || inferred_local)
                .then(|| chain_receiver(object, source, &tree, &scopes, &declarations, package, 0))
                .flatten()
        }) {
            result.push(DependencyValueMember {
                path: String::new(),
                position: node.start_byte(),
                contexts: Vec::new(),
                use_contexts: dependency_contexts(node, source, package),
                name: text(member, source).to_owned(),
                method,
                chain: Some(chain),
                arguments: arguments(node, source, &scopes, &tree, &declarations, package),
            });
        }
        WalkControl::Continue
    });
    Ok(result)
}

#[cfg(test)]
mod dependency_value_tests {
    #[test]
    fn bounded_record_receivers_keep_parameter_owner_and_instance_guards() {
        let source = r#"record Probe<T extends shared.Child>(T value) {
            int valid() { return value.instance(); }
            static int invalid() { return value.instance(); }
            java.util.function.IntSupplier capture() { return () -> value.instance(); }
            static class Peer { int invalid() { return value.instance(); } }
        }"#;
        let members = super::dependency_value_members(source, "").unwrap();
        assert_eq!(members.len(), 2);
        for member in members {
            assert_eq!(member.name, "instance");
            assert!(
                matches!(member.chain, Some(super::DependencyValueReceiver::Nominal {
                signature: crate::parsers::treesitter::java::DependencyResultType::Parameter { owner, name }, ..
            }) if owner == "Probe" && name == "T")
            );
        }
    }

    #[test]
    fn inferred_chains_keep_initializer_scope_and_explicit_arguments() {
        use super::DependencyValueReceiver;
        let source = r#"class Box<T> { T get(){return null;} }
        class Use { int run(Box<shared.Child> b) {
            var c=b.get(); class Child {} return c.instance();
        }}"#;
        let members = super::dependency_value_members(source, "").unwrap();
        let member = members
            .iter()
            .find(|member| member.name == "instance")
            .unwrap();
        let Some(DependencyValueReceiver::Initializer { receiver, contexts }) = &member.chain
        else {
            panic!("inferred receiver lost its initializer");
        };
        assert_eq!(contexts.first().map(String::as_str), Some("Use"));
        let DependencyValueReceiver::Member {
            receiver,
            name,
            arity,
            ..
        } = receiver.as_ref()
        else {
            panic!("inferred invocation lost its source chain");
        };
        assert_eq!(name, "get");
        assert_eq!(*arity, Some(0));
        let DependencyValueReceiver::Nominal {
            signature,
            position,
            ..
        } = receiver.as_ref()
        else {
            panic!("parameter receiver lost its declaration");
        };
        let crate::parsers::treesitter::java::DependencyResultType::Named(path, arguments) =
            signature
        else {
            panic!("parameter receiver lost its generic signature");
        };
        assert_eq!(path, "Box");
        assert_eq!(*position, source.find("Box<shared.Child>").unwrap());
        assert!(
            matches!(arguments.as_slice(), [Some(crate::parsers::treesitter::java::DependencyResultType::Named(path, args))]
            if path == "shared.Child" && args.is_empty())
        );
    }

    #[test]
    fn record_component_receivers_require_the_declaring_instance() {
        let source = r#"record Probe(shared.Child value) {
            int valid() { return value.instance(); }
            static int invalid() { return value.instance(); }
            java.util.function.IntSupplier capture() { return () -> value.instance(); }
            static class Peer { int invalid() { return value.instance(); } }
            static int parameter(shared.Child value) { return value.instance(); }
        }"#;
        let members = super::dependency_value_members(source, "").unwrap();
        assert_eq!(members.len(), 3);
        assert!(members
            .iter()
            .all(|member| member.path == "shared.Child" && member.name == "instance"));
        assert_eq!(members[0].position, members[1].position);
        assert_ne!(members[0].position, members[2].position);
    }

    #[test]
    fn static_fields_and_local_instances_keep_their_own_type_sites() {
        let source = r#"class Probe {
            static shared.Child value;
            static int use() { return value.instance(); }
            static int local() {
                class Local {
                    shared.Child value;
                    int read() { return value.instance(); }
                }
                return 0;
            }
            static int parameter(shared.Child value) { return value.instance(); }
        }"#;
        let members = super::dependency_value_members(source, "").unwrap();
        assert_eq!(members.len(), 3);
        for member in &members {
            assert_eq!(member.path, "shared.Child");
            assert_eq!(member.name, "instance");
        }
        assert!(members[1].contexts[0].starts_with("Probe.Local@"));
        assert_eq!(members[1].contexts, members[1].use_contexts);
        assert_ne!(members[0].position, members[1].position);
        assert_ne!(members[0].position, members[2].position);
    }

    #[test]
    fn nominal_sites_do_not_borrow_shadowed_or_static_bindings() {
        let source = r#"class Probe {
            shared.Child value;
            int use(shared.Child value) { return value.instance(); }
            int field() { return this.value.OPEN; }
            static int invalid() { return value.instance(); }
            java.util.function.ToIntFunction<Object> invalidLambda() {
                return value -> value.instance();
            }
            java.util.function.IntSupplier reference(shared.Child value) {
                return value::instance;
            }
        }"#;
        let members = super::dependency_value_members(source, "").unwrap();
        assert_eq!(
            members.len(),
            3,
            "{:?}",
            members
                .iter()
                .map(|member| (member.name.as_str(), member.position))
                .collect::<Vec<_>>()
        );
        assert_eq!(
            members
                .iter()
                .map(|member| member.name.as_str())
                .collect::<Vec<_>>(),
            ["instance", "OPEN", "instance"]
        );
        for member in &members {
            assert_eq!(member.path, "shared.Child");
            assert!(source[member.position..].starts_with("shared.Child"));
            assert_eq!(member.contexts, ["Probe"]);
            assert_eq!(member.use_contexts, ["Probe"]);
        }
        assert_ne!(members[0].position, members[1].position);
        assert_ne!(members[0].position, members[2].position);
    }
}

impl JavaSource {
    pub fn parse(source: &str) -> Result<Self> {
        let tree = parse_tree(source, &LANGUAGE)?;
        let declarations = callable_declarations(tree.root_node(), source);
        let scopes = variable_scopes(tree.root_node(), source, &declarations);
        let mut result = Self {
            invocation_owners: invocation_owners(tree.root_node(), source, &declarations),
            ..Self::default()
        };
        let mut implicit_accessors = Vec::new();
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
        walk_tree_preorder(&tree.root_node(), |class| {
            if !matches!(class.kind(), "class_declaration" | "enum_declaration") {
                return WalkControl::Continue;
            }
            let (Some(name), Some(body)) = (
                class.child_by_field_name("name"),
                class.child_by_field_name("body"),
            ) else {
                return WalkControl::Continue;
            };
            let annotation = |node: Node<'_>| -> Option<bool> {
                let mut cursor = node.walk();
                let modifiers = node
                    .named_children(&mut cursor)
                    .find(|child| child.kind() == "modifiers")?;
                let mut cursor = modifiers.walk();
                for annotation in modifiers
                    .named_children(&mut cursor)
                    .filter(|child| matches!(child.kind(), "annotation" | "marker_annotation"))
                {
                    let Some(path) = annotation
                        .child_by_field_name("name")
                        .and_then(|name| type_name(name, source))
                    else {
                        continue;
                    };
                    if path == "lombok::Getter"
                        || (path == "Getter"
                            && result
                                .imports
                                .iter()
                                .any(|import| import == "lombok::Getter"))
                    {
                        return Some(!text(annotation, source).contains("NONE"));
                    }
                }
                None
            };
            let class_getter = annotation(class).unwrap_or(false);
            let mut cursor = body.walk();
            for field in body
                .named_children(&mut cursor)
                .filter(|node| node.kind() == "field_declaration")
            {
                if !annotation(field).unwrap_or(class_getter) {
                    continue;
                }
                let mut children = field.walk();
                if field.named_children(&mut children).any(|child| {
                    child.kind() == "modifiers"
                        && text(child, source)
                            .split_whitespace()
                            .any(|word| word == "static")
                }) {
                    continue;
                }
                let Some(ty) = field.child_by_field_name("type") else {
                    continue;
                };
                let Some(receiver) = generic_receiver(ty, class, source).or_else(|| {
                    type_name(ty, source)
                        .filter(|path| !type_parameter(class, path, source))
                        .map(JavaReceiver::Type)
                }) else {
                    continue;
                };
                let mut children = field.walk();
                for variable in field
                    .named_children(&mut children)
                    .filter(|node| node.kind() == "variable_declarator")
                {
                    let Some(field_name) = variable.child_by_field_name("name") else {
                        continue;
                    };
                    let raw = text(field_name, source);
                    let mut characters = raw.chars();
                    let Some(first) = characters.next() else {
                        continue;
                    };
                    let suffix: String = first.to_uppercase().chain(characters).collect();
                    let getter = if text(ty, source) == "boolean" {
                        if raw.starts_with("is")
                            && raw.chars().nth(2).is_some_and(char::is_uppercase)
                        {
                            raw.to_owned()
                        } else {
                            format!("is{suffix}")
                        }
                    } else {
                        format!("get{suffix}")
                    };
                    let mut dimensions = variable.walk();
                    let receiver = if variable
                        .named_children(&mut dimensions)
                        .any(|child| child.kind() == "dimensions")
                    {
                        JavaReceiver::Array(type_name(ty, source))
                    } else {
                        receiver.clone()
                    };
                    let value = Some((field_name.start_position().row as i64 + 1, receiver));
                    result
                        .getter_receivers
                        .entry((
                            text(name, source).to_owned(),
                            name.start_position().row as i64 + 1,
                            getter,
                        ))
                        .and_modify(|previous| {
                            if *previous != value {
                                *previous = None;
                            }
                        })
                        .or_insert(value);
                }
            }
            WalkControl::Continue
        });
        let mut type_sites = HashSet::new();
        walk_tree_preorder(&tree.root_node(), |node| {
            if let (Some(name), Some(parameters)) = (
                node.child_by_field_name("name"),
                node.child_by_field_name("type_parameters"),
            ) {
                let key = (
                    text(name, source).to_owned(),
                    name.start_position().row as i64 + 1,
                    name.start_byte(),
                );
                let mut declared = Vec::new();
                let mut cursor = parameters.walk();
                for parameter in parameters.named_children(&mut cursor) {
                    let mut children = parameter.walk();
                    let identifier = parameter
                        .named_children(&mut children)
                        .find(|child| matches!(child.kind(), "identifier" | "type_identifier"));
                    if let Some(identifier) = identifier {
                        let parameter_name = text(identifier, source).to_owned();
                        declared.push(parameter_name.clone());
                        let mut children = parameter.walk();
                        let bound = parameter
                            .named_children(&mut children)
                            .find(|child| child.kind() == "type_bound")
                            .and_then(|bound| bound.named_child(0))
                            .and_then(|bound| type_name(bound, source));
                        if let Some(bound) = bound {
                            result
                                .type_bounds
                                .entry(key.clone())
                                .or_default()
                                .insert(parameter_name, bound);
                        }
                    }
                }
                result.type_parameters.insert(key, declared);
            }
            if node.kind() == "field_declaration" {
                if let Some(ty) = node.child_by_field_name("type") {
                    let receiver = generic_receiver_at(ty, node, source, 0, true)
                        .or_else(|| {
                            type_name(ty, source)
                                .filter(|path| {
                                    !type_parameter(
                                        node,
                                        path.split("::").next().unwrap_or_default(),
                                        source,
                                    )
                                })
                                .map(JavaReceiver::Type)
                        })
                        .unwrap_or(JavaReceiver::Unknown);
                    let mut cursor = node.walk();
                    for variable in node
                        .named_children(&mut cursor)
                        .filter(|variable| variable.kind() == "variable_declarator")
                    {
                        if let Some(name) = variable.child_by_field_name("name") {
                            result.member_invocation_types.insert(
                                (
                                    text(name, source).to_owned(),
                                    name.start_position().row as i64 + 1,
                                    name.start_byte(),
                                ),
                                declared_invocation_type(ty, variable, source),
                            );
                            result.member_receivers.insert(
                                (
                                    text(name, source).to_owned(),
                                    name.start_position().row as i64 + 1,
                                    name.start_byte(),
                                ),
                                receiver.clone(),
                            );
                        }
                    }
                }
            }
            if node.kind() == "enum_constant" {
                if let (Some(name), Some(declaration)) = (
                    node.child_by_field_name("name"),
                    node.parent()
                        .and_then(|body| body.parent())
                        .and_then(|declaration| declaration.child_by_field_name("name")),
                ) {
                    result.member_receivers.insert(
                        (
                            text(name, source).to_owned(),
                            name.start_position().row as i64 + 1,
                            name.start_byte(),
                        ),
                        JavaReceiver::Type(text(declaration, source).to_owned()),
                    );
                }
            }
            if matches!(
                node.kind(),
                "class_declaration"
                    | "interface_declaration"
                    | "enum_declaration"
                    | "record_declaration"
                    | "annotation_type_declaration"
            ) {
                if let Some(name) = node.child_by_field_name("name") {
                    let mut cursor = node.walk();
                    let modifiers = node
                        .named_children(&mut cursor)
                        .find(|child| child.kind() == "modifiers");
                    let has_modifier = |value| {
                        modifiers.is_some_and(|modifiers| {
                            let mut cursor = modifiers.walk();
                            let found = modifiers
                                .children(&mut cursor)
                                .any(|child| child.kind() == value);
                            found
                        })
                    };
                    let member = node.parent().filter(|parent| {
                        matches!(
                            parent.kind(),
                            "class_body"
                                | "interface_body"
                                | "enum_body"
                                | "enum_body_declarations"
                                | "annotation_type_body"
                        )
                    });
                    let interface_member = member.is_some_and(|body| {
                        matches!(body.kind(), "interface_body" | "annotation_type_body")
                    });
                    let access = if has_modifier("public") || interface_member {
                        TypeAccess::Public
                    } else if has_modifier("private") {
                        TypeAccess::Private
                    } else if has_modifier("protected") {
                        TypeAccess::Protected
                    } else {
                        TypeAccess::Package
                    };
                    let mut path = vec![text(name, source)];
                    let mut ancestor = node.parent();
                    while let Some(parent) = ancestor {
                        if matches!(
                            parent.kind(),
                            "class_declaration"
                                | "interface_declaration"
                                | "annotation_type_declaration"
                                | "enum_declaration"
                                | "record_declaration"
                        ) {
                            if let Some(name) = parent.child_by_field_name("name") {
                                path.push(text(name, source));
                            }
                        }
                        ancestor = parent.parent();
                    }
                    path.reverse();
                    let qualified = if result.package.is_empty() {
                        path.join("::")
                    } else {
                        format!("{}::{}", result.package, path.join("::"))
                    };
                    result
                        .declarations
                        .entry((qualified, name.start_position().row as i64 + 1))
                        .or_default()
                        .push(TypeDeclaration {
                            site: name.start_byte(),
                            access,
                            local: node.parent().is_some_and(|parent| {
                                matches!(
                                    parent.kind(),
                                    "block" | "constructor_body" | "switch_block_statement_group"
                                )
                            }),
                            local_scope: {
                                let mut ancestor = Some(node);
                                let mut scope = None;
                                while let Some(declaration) = ancestor {
                                    if matches!(
                                        declaration.kind(),
                                        "class_declaration"
                                            | "interface_declaration"
                                            | "enum_declaration"
                                            | "record_declaration"
                                    ) {
                                        if let Some(block) = declaration.parent().filter(|parent| {
                                            matches!(
                                                parent.kind(),
                                                "block"
                                                    | "constructor_body"
                                                    | "switch_block_statement_group"
                                            )
                                        }) {
                                            scope =
                                                Some(declaration.start_byte()..block.end_byte());
                                            break;
                                        }
                                    }
                                    ancestor = declaration.parent();
                                }
                                scope
                            },
                            static_member: member.is_some()
                                && (has_modifier("static")
                                    || interface_member
                                    || matches!(
                                        node.kind(),
                                        "interface_declaration"
                                            | "annotation_type_declaration"
                                            | "enum_declaration"
                                            | "record_declaration"
                                    )),
                        });
                    let mut parents = Vec::new();
                    let mut parent_receivers = Vec::new();
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
                                        parents.push(path.clone());
                                        let receiver =
                                            generic_receiver_at(parent, node, source, 0, true)
                                                .unwrap_or_else(|| {
                                                    JavaReceiver::Type(path.clone())
                                                });
                                        parent_receivers.push((
                                            receiver,
                                            ReceiverTypeSite {
                                                path,
                                                position: parent.start_byte(),
                                                owner: text(name, source).to_owned(),
                                                owner_line: name.start_position().row as i64 + 1,
                                                owner_site: name.start_byte(),
                                                owner_ordinal: None,
                                            },
                                        ));
                                    }
                                    return WalkControl::SkipChildren;
                                }
                                WalkControl::Continue
                            });
                        }
                    }
                    result.parents.insert(name.start_byte(), parents);
                    result
                        .parent_receivers
                        .insert(name.start_byte(), parent_receivers);
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
                if let Some(declaration) = parent {
                    let mut ancestor = declaration.parent();
                    let mut path = Vec::new();
                    let mut owner_line = None;
                    let mut owner_site = None;
                    while let Some(owner) = ancestor {
                        if matches!(
                            owner.kind(),
                            "class_declaration"
                                | "interface_declaration"
                                | "enum_declaration"
                                | "record_declaration"
                                | "annotation_type_declaration"
                        ) {
                            if let Some(name) = owner.child_by_field_name("name") {
                                owner_line.get_or_insert(name.start_position().row as i64 + 1);
                                owner_site.get_or_insert(name.start_byte());
                                path.push(text(name, source));
                            }
                        }
                        ancestor = owner.parent();
                    }
                    path.reverse();
                    let container = owner_line.map(|line| {
                        (
                            if result.package.is_empty() {
                                path.join("::")
                            } else {
                                format!("{}::{}", result.package, path.join("::"))
                            },
                            line,
                        )
                    });
                    result
                        .member_containers
                        .entry((
                            text(node, source).to_owned(),
                            node.start_position().row as i64 + 1,
                        ))
                        .and_modify(|previous| {
                            if *previous != container {
                                *previous = None;
                            }
                        })
                        .or_insert(container);
                    // A field's type precedes its variable declarator. Its
                    // lexical range must include that type site, especially
                    // when it refers to the enclosing block-local class.
                    let range = declaration
                        .parent()
                        .filter(|parent| {
                            declaration.kind() == "variable_declarator"
                                && parent.kind() == "field_declaration"
                        })
                        .unwrap_or(declaration)
                        .byte_range();
                    let kind = match declaration.kind() {
                        "class_declaration" | "record_declaration" => Some("class"),
                        "interface_declaration" | "annotation_type_declaration" => {
                            Some("interface")
                        }
                        "enum_declaration" => Some("enum"),
                        "method_declaration"
                        | "constructor_declaration"
                        | "compact_constructor_declaration" => Some("function"),
                        "enum_constant" => Some("constant"),
                        "variable_declarator"
                            if declaration
                                .parent()
                                .is_some_and(|p| p.kind() == "field_declaration") =>
                        {
                            Some("property")
                        }
                        "formal_parameter"
                            if declaration
                                .parent()
                                .and_then(|p| p.parent())
                                .is_some_and(|p| p.kind() == "record_declaration") =>
                        {
                            Some("property")
                        }
                        _ => None,
                    };
                    if let Some(kind) = kind {
                        let site = node.start_byte();
                        result
                            .symbol_sites
                            .entry((
                                text(node, source).to_owned(),
                                node.start_position().row as i64 + 1,
                                kind.to_owned(),
                            ))
                            .or_default()
                            .push(site);
                        result.site_containers.insert(site, owner_site);
                        result.site_ranges.insert(site, range.clone());
                    }
                    result
                        .declaration_ranges
                        .entry((
                            text(node, source).to_owned(),
                            node.start_position().row as i64 + 1,
                        ))
                        .and_modify(|previous| {
                            if previous.as_ref() != Some(&range) {
                                *previous = None;
                            }
                        })
                        .or_insert(Some(range));
                }
                return WalkControl::Continue;
            }
            let annotation = parent
                .is_some_and(|parent| matches!(parent.kind(), "annotation" | "marker_annotation"));
            // An unbound simple invocation qualifier occupies a type-name
            // site. A variable or captured field with the same spelling does
            // not, even when its declared type has that name.
            let invocation_receiver = node.kind() == "identifier"
                && parent.is_some_and(|parent| {
                    parent.kind() == "method_invocation"
                        && parent
                            .child_by_field_name("object")
                            .is_some_and(|object| object.id() == node.id())
                });
            let type_receiver = invocation_receiver
                && variable_type(node, text(node, source), false, &scopes, source).is_none()
                && callable(node).is_some_and(|owner| {
                    let receiver = expression_receiver(node, owner, source, &scopes, 0);
                    if let JavaReceiver::LexicalName { boundary, .. } = &receiver {
                        result
                            .lexical_receivers
                            .entry((
                                node.start_position().row as i64 + 1,
                                text(node, source).to_owned(),
                            ))
                            .or_default()
                            .push(boundary.clone());
                    }
                    matches!(receiver,
                        JavaReceiver::Type(path) | JavaReceiver::LexicalName { name: path, .. }
                        if path == text(node, source))
                });
            let typed = node.kind() == "type_identifier" || annotation || invocation_receiver;
            let key = (
                node.start_position().row as i64 + 1,
                text(node, source).to_owned(),
            );
            if invocation_receiver && !type_receiver {
                result.value_receivers.insert(key.clone());
            }
            let binding = if typed {
                type_sites.insert(key.clone());
                result
                    .type_positions
                    .entry(key.clone())
                    .or_default()
                    .push(node.start_byte());
                // Line ranges cannot distinguish a class header from a method
                // signature on the same line. Keep syntax ownership for type
                // sites, and retain collisions as unresolved rather than
                // attributing several declarations to the first symbol.
                let mut ancestor = node.parent();
                let mut path = Vec::new();
                let mut owner_line = None;
                while let Some(parent) = ancestor {
                    if parent.kind() == "field_declaration" && owner_line.is_none() {
                        let mut cursor = parent.walk();
                        let names: Vec<_> = parent
                            .named_children(&mut cursor)
                            .filter(|child| child.kind() == "variable_declarator")
                            .filter_map(|child| child.child_by_field_name("name"))
                            .collect();
                        if let [name] = names.as_slice() {
                            owner_line = Some(name.start_position().row as i64 + 1);
                            path.push(text(*name, source));
                        } else {
                            break;
                        }
                    }
                    if matches!(
                        parent.kind(),
                        "method_declaration"
                            | "constructor_declaration"
                            | "compact_constructor_declaration"
                            | "class_declaration"
                            | "interface_declaration"
                            | "enum_declaration"
                            | "record_declaration"
                            | "annotation_type_declaration"
                    ) {
                        if let Some(name) = parent.child_by_field_name("name") {
                            if owner_line.is_none() {
                                owner_line = Some(name.start_position().row as i64 + 1);
                                path.push(text(name, source));
                            } else if !matches!(
                                parent.kind(),
                                "method_declaration"
                                    | "constructor_declaration"
                                    | "compact_constructor_declaration"
                            ) {
                                path.push(text(name, source));
                            }
                        }
                    }
                    ancestor = parent.parent();
                }
                path.reverse();
                let owner = owner_line.map(|line| {
                    let qualified = if result.package.is_empty() {
                        path.join("::")
                    } else {
                        format!("{}::{}", result.package, path.join("::"))
                    };
                    (qualified, line)
                });
                result
                    .type_owners
                    .entry(key.clone())
                    .and_modify(|previous| {
                        if *previous != owner {
                            *previous = None;
                        }
                    })
                    .or_insert(owner);
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
                        && (!invocation_receiver || type_receiver)
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
            if matches!(
                node.kind(),
                "constructor_declaration" | "compact_constructor_declaration"
            ) {
                if let Some(name) = node.child_by_field_name("name") {
                    result.constructor_declarations.insert((
                        text(name, source).to_owned(),
                        name.start_position().row as i64 + 1,
                    ));
                }
            }
            if node.kind() == "record_declaration" {
                if let Some(parameters) = node.child_by_field_name("parameters") {
                    if let Some(name) = node.child_by_field_name("name") {
                        let mut cursor = parameters.walk();
                        let types = parameters
                            .named_children(&mut cursor)
                            .filter_map(|parameter| {
                                parameter
                                    .child_by_field_name("type")
                                    .and_then(|ty| type_name(ty, source))
                            })
                            .collect();
                        result
                            .canonical_types
                            .insert(text(name, source).to_owned(), types);
                    }
                    let mut cursor = parameters.walk();
                    for component in parameters.named_children(&mut cursor) {
                        let Some(name) = component.child_by_field_name("name") else {
                            continue;
                        };
                        result.member_invocation_types.insert(
                            (
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                                name.start_byte(),
                            ),
                            component
                                .child_by_field_name("type")
                                .and_then(|ty| declared_invocation_type(ty, component, source)),
                        );
                        if let Some(receiver) =
                            component.child_by_field_name("type").and_then(|ty| {
                                generic_receiver(ty, node, source).or_else(|| {
                                    type_name(ty, source)
                                        .filter(|path| {
                                            !type_parameter(
                                                node,
                                                path.split("::").next().unwrap_or_default(),
                                                source,
                                            )
                                        })
                                        .map(JavaReceiver::Type)
                                })
                            })
                        {
                            result.member_receivers.insert(
                                (
                                    text(name, source).to_owned(),
                                    name.start_position().row as i64 + 1,
                                    name.start_byte(),
                                ),
                                receiver,
                            );
                        }
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
                            implicit_accessors.push((
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                                name.start_byte(),
                            ));
                            if let Some(parameter) = component
                                .child_by_field_name("type")
                                .and_then(|ty| type_name(ty, source))
                                .filter(|parameter| {
                                    !parameter.contains("::")
                                        && type_parameter(node, parameter, source)
                                })
                            {
                                result.return_parameters.insert(
                                    (
                                        text(name, source).to_owned(),
                                        name.start_position().row as i64 + 1,
                                        name.start_byte(),
                                    ),
                                    parameter,
                                );
                            }
                            if let Some(receiver) = component
                                .child_by_field_name("type")
                                .and_then(|ty| generic_receiver(ty, node, source))
                            {
                                result.return_receivers.insert(
                                    (
                                        text(name, source).to_owned(),
                                        name.start_position().row as i64 + 1,
                                        name.start_byte(),
                                    ),
                                    receiver,
                                );
                            }
                            result.returns.insert(
                                (
                                    text(name, source).to_owned(),
                                    name.start_position().row as i64 + 1,
                                    name.start_byte(),
                                ),
                                component
                                    .child_by_field_name("type")
                                    .and_then(|ty| type_name(ty, source))
                                    .filter(|path| {
                                        !type_parameter(
                                            node,
                                            path.split("::").next().unwrap_or_default(),
                                            source,
                                        )
                                    }),
                            );
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
                    if let Some(receiver) = node
                        .child_by_field_name("type")
                        .and_then(|ty| generic_receiver(ty, node, source))
                    {
                        result.return_receivers.insert(
                            (
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                                name.start_byte(),
                            ),
                            receiver,
                        );
                    }
                    if let Some(parameter) = node
                        .child_by_field_name("type")
                        .and_then(|ty| type_name(ty, source))
                        .filter(|parameter| {
                            !parameter.contains("::") && type_parameter(node, parameter, source)
                        })
                    {
                        result.return_parameters.insert(
                            (
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                                name.start_byte(),
                            ),
                            parameter,
                        );
                    }
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
                            name.start_byte(),
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
                    let mut modifier_cursor = node.walk();
                    if node
                        .named_children(&mut modifier_cursor)
                        .find(|child| child.kind() == "modifiers")
                        .is_some_and(|modifiers| {
                            let mut cursor = modifiers.walk();
                            let is_static = modifiers
                                .children(&mut cursor)
                                .any(|child| child.kind() == "static");
                            is_static
                        })
                    {
                        result.static_methods.insert(key.clone());
                    }
                    let mut cursor = parameters.walk();
                    let invocation_parameters: Vec<Option<String>> = parameters
                        .named_children(&mut cursor)
                        .filter(|p| matches!(p.kind(), "formal_parameter" | "spread_parameter"))
                        .map(|p| declared_invocation_type(parameter_type(p)?, p, source))
                        .collect();
                    result
                        .invocation_signatures
                        .entry(key.clone())
                        .or_default()
                        .push((invocation_parameters.clone(), variadic));
                    result
                        .invocation_parameters
                        .insert(key.clone(), invocation_parameters);
                    let mut cursor = parameters.walk();
                    result.reference_parameters.insert(
                        key.clone(),
                        parameters
                            .named_children(&mut cursor)
                            .filter(|p| matches!(p.kind(), "formal_parameter" | "spread_parameter"))
                            .map(|p| {
                                parameter_type(p).and_then(|ty| {
                                    generic_receiver_at(ty, node, source, 0, true)
                                        .or_else(|| type_name(ty, source).map(JavaReceiver::Type))
                                })
                            })
                            .collect(),
                    );
                    let mut cursor = parameters.walk();
                    result.callback_parameters.insert(
                        key.clone(),
                        parameters
                            .named_children(&mut cursor)
                            .filter(|p| matches!(p.kind(), "formal_parameter" | "spread_parameter"))
                            .map(|p| {
                                p.child_by_field_name("type")
                                    .and_then(|ty| generic_receiver_at(ty, node, source, 0, true))
                            })
                            .collect(),
                    );
                    result
                        .parameters
                        .entry(key)
                        .and_modify(|previous| *previous = None)
                        .or_insert(Some((count, variadic)));
                    if node.kind() == "constructor_declaration" {
                        let mut cursor = parameters.walk();
                        let types = parameters
                            .named_children(&mut cursor)
                            .filter_map(|parameter| {
                                parameter
                                    .child_by_field_name("type")
                                    .and_then(|ty| type_name(ty, source))
                            })
                            .collect();
                        result
                            .constructor_types
                            .entry((
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                            ))
                            .or_default()
                            .push(types);
                    }
                }
            }
            if node.kind() == "compact_constructor_declaration" {
                if let (Some(name), Some(record)) = (
                    node.child_by_field_name("name"),
                    node.parent().and_then(|parent| parent.parent()),
                ) {
                    if let Some(parameters) = record.child_by_field_name("parameters") {
                        let mut cursor = parameters.walk();
                        let count = parameters
                            .named_children(&mut cursor)
                            .filter(|parameter| {
                                matches!(parameter.kind(), "formal_parameter" | "spread_parameter")
                            })
                            .count();
                        result.parameters.insert(
                            (
                                text(name, source).to_owned(),
                                name.start_position().row as i64 + 1,
                            ),
                            Some((count, false)),
                        );
                    }
                }
            }
            if node.kind() == "explicit_constructor_invocation" && !node.has_error() {
                if let (Some(owner), Some(constructor)) =
                    (callable(node), node.child_by_field_name("constructor"))
                {
                    if let Some(owner_name) = owner.child_by_field_name("name") {
                        let key = (
                            text(owner_name, source).to_owned(),
                            owner_name.start_position().row as i64 + 1,
                            constructor.start_position().row as i64 + 1,
                            text(constructor, source).to_owned(),
                        );
                        result.constructors.insert(
                            key,
                            Some(ConstructorCall {
                                receiver: text(constructor, source).to_owned(),
                                arguments: argument_count(node),
                            }),
                        );
                    }
                }
            }
            if node.kind() == "enum_constant" && !node.has_error() {
                let mut ancestor = node.parent();
                while let Some(declaration) = ancestor {
                    if declaration.kind() == "enum_declaration" {
                        if let (Some(owner), Some(ty)) = (
                            node.child_by_field_name("name"),
                            declaration.child_by_field_name("name"),
                        ) {
                            let line = owner.start_position().row as i64 + 1;
                            let key = (
                                text(owner, source).to_owned(),
                                line,
                                line,
                                text(ty, source).to_owned(),
                            );
                            result.creation_types.insert(
                                key.clone(),
                                creation_argument_types(node, node, source, &scopes),
                            );
                            result.constructors.insert(
                                key,
                                Some(ConstructorCall {
                                    receiver: text(ty, source).to_owned(),
                                    arguments: Some(argument_count(node).unwrap_or(0)),
                                }),
                            );
                        }
                        break;
                    }
                    ancestor = declaration.parent();
                }
            }
            let creation = node.kind() == "object_creation_expression";
            let constructor_reference = node.kind() == "method_reference"
                && node
                    .child(node.child_count().saturating_sub(1) as u32)
                    .is_some_and(|last| last.kind() == "new");
            if (creation || constructor_reference) && !node.has_error() {
                if let (Some(owner), Some(ty), Some(arguments)) = (
                    callable(node),
                    if creation {
                        node.child_by_field_name("type")
                    } else {
                        node.named_child(0)
                    },
                    if creation {
                        argument_count(node).map(Some)
                    } else {
                        Some(None)
                    },
                ) {
                    if let (Some(owner_name), Some(declared)) =
                        (owner.child_by_field_name("name"), type_name(ty, source))
                    {
                        let mut identifier = ty;
                        while let Some(child) = identifier
                            .child_by_field_name("name")
                            .or_else(|| identifier.named_child(0))
                        {
                            identifier = child;
                        }
                        let key = (
                            text(owner_name, source).to_owned(),
                            owner_name.start_position().row as i64 + 1,
                            identifier.start_position().row as i64 + 1,
                            declared.rsplit("::").next().unwrap_or_default().to_owned(),
                        );
                        let call = Some(ConstructorCall {
                            receiver: declared,
                            arguments,
                        });
                        if node.child_by_field_name("arguments").is_some() {
                            result.creation_types.insert(
                                key.clone(),
                                creation_argument_types(node, owner, source, &scopes),
                            );
                        }
                        result
                            .constructors
                            .entry(key)
                            .and_modify(|previous| {
                                if *previous != call {
                                    *previous = None;
                                }
                            })
                            .or_insert(call);
                    }
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
                    owner_site: owner_name.start_byte(),
                    receiver: expression_receiver(object, owner, source, &scopes, 0),
                    receiver_site: expression_receiver_site(object, source, &scopes, &declarations),
                    arguments: if reference {
                        None
                    } else {
                        argument_count(node)
                    },
                    reference_context: reference
                        .then(|| method_reference_context(node, owner, source, &scopes))
                        .flatten(),
                    reference_type: reference
                        .then(|| method_reference_type(object, source, &scopes, 0))
                        .flatten(),
                });
                if let Some(call) = &call {
                    let variants = result.expression_variants.entry(key.clone()).or_default();
                    if !variants.contains(call) {
                        variants.push(call.clone());
                    }
                }
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
            let argument_types = node.child_by_field_name("arguments").map(|arguments| {
                let mut cursor = arguments.walk();
                arguments
                    .named_children(&mut cursor)
                    .filter(|argument| !argument.is_extra())
                    .map(|argument| {
                        if argument.kind() == "lambda_expression" {
                            let parameters = argument.child_by_field_name("parameters")?;
                            let arity = if parameters.kind() == "identifier" {
                                1
                            } else {
                                let mut cursor = parameters.walk();
                                parameters
                                    .named_children(&mut cursor)
                                    .filter(|child| !child.is_extra())
                                    .count()
                            };
                            return Some(InvocationArgument::Lambda(arity));
                        }
                        invocation_argument_type(argument, owner, source, &scopes, 0)
                            .map(InvocationArgument::Type)
                            .or_else(|| {
                                let receiver =
                                    expression_receiver(argument, owner, source, &scopes, 0);
                                if matches!(receiver, JavaReceiver::Field { .. }) {
                                    Some(InvocationArgument::Field(receiver))
                                } else if !matches!(receiver, JavaReceiver::Unknown) {
                                    Some(InvocationArgument::Value(receiver))
                                } else {
                                    None
                                }
                            })
                    })
                    .collect()
            });
            result
                .invocation_types
                .entry(key.clone())
                .and_modify(|previous| {
                    if *previous != argument_types {
                        *previous = None;
                    }
                })
                .or_insert(argument_types);
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
                                if parameter
                                    .child_by_field_name("type")
                                    .is_some_and(|ty| ty.kind() == "generic_type")
                                {
                                    return None;
                                }
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
                                    if type_parameter_bound(owner, &name, source).is_some() {
                                        known_binding = false;
                                    }
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
                if variable_inferred(node, receiver, fields_only, &scopes, source).is_some() {
                    known_binding = true;
                    return None;
                }
                let declared = variable_type(node, receiver, fields_only, &scopes, source)?;
                known_binding = true;
                let declared = declared?;
                if type_parameter(owner, declared.split("::").next()?, source) {
                    if type_parameter_bound(owner, declared, source).is_some() {
                        known_binding = false;
                    }
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
            if known_binding
                && callable(node).is_some_and(|owner| {
                    let mut ancestor = node.parent();
                    while let Some(scope) = ancestor {
                        if scope.id() == owner.id() {
                            return true;
                        }
                        if scope.kind() == "lambda_expression" {
                            return false;
                        }
                        ancestor = scope.parent();
                    }
                    true
                })
            {
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
        // The parser appends synthetic accessors after explicit methods. Match
        // that ordinal order while retaining each record component's byte site.
        for (name, line, site) in implicit_accessors {
            result
                .symbol_sites
                .entry((name.clone(), line, "function".to_owned()))
                .or_default()
                .push(site);
            result
                .invocation_signatures
                .entry((name, line))
                .or_default()
                .push((Vec::new(), false));
        }
        Ok(result)
    }

    pub fn is_static_method(&self, name: &str, line: i64) -> bool {
        self.static_methods.contains(&(name.to_owned(), line))
    }

    pub fn invocation_owners(&self, line: i64, name: &str) -> Option<&[InvocationOwner]> {
        self.invocation_owners
            .get(&(line, name.to_owned()))
            .map(Vec::as_slice)
    }

    pub fn callback_parameter(&self, name: &str, line: i64, index: usize) -> Option<&JavaReceiver> {
        self.callback_parameters
            .get(&(name.to_owned(), line))?
            .get(index)?
            .as_ref()
    }

    pub fn reference_parameter(
        &self,
        name: &str,
        line: i64,
        index: usize,
    ) -> Option<&JavaReceiver> {
        self.reference_parameters
            .get(&(name.to_owned(), line))?
            .get(index)?
            .as_ref()
    }

    pub fn parent_types_at(&self, site: Option<usize>) -> Option<&[String]> {
        self.parents.get(&site?).map(Vec::as_slice)
    }

    #[cfg(test)]
    pub fn parent_types(&self, name: &str, line: i64) -> Option<&[String]> {
        let site = self
            .symbol_site(name, line, "class", 0)
            .or_else(|| self.symbol_site(name, line, "interface", 0))?;
        self.parent_types_at(Some(site))
    }

    pub fn constructor_call(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&Option<ConstructorCall>> {
        self.constructors
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
    }

    pub fn constructor_aliases(&self) -> impl Iterator<Item = (&str, i64)> {
        self.constructors
            .keys()
            .map(|(_, _, line, name)| (name.as_str(), *line))
    }

    pub fn accepts_creation(
        &self,
        declaration: (&str, i64, usize),
        call_file: &Self,
        owner: &str,
        owner_line: i64,
        call_line: i64,
    ) -> bool {
        let (name, line, ordinal) = declaration;
        let Some(parameters) = self.constructor_parameters(name, line, ordinal) else {
            return true;
        };
        let canonical_collision = self.canonical_types.get(name).is_some_and(|canonical| {
            canonical.len() == parameters.len() && canonical != parameters
        });
        if !canonical_collision {
            return true;
        }
        let Some(arguments) = call_file.creation_types.get(&(
            owner.to_owned(),
            owner_line,
            call_line,
            name.to_owned(),
        )) else {
            return false;
        };
        arguments
            .iter()
            .zip(parameters)
            .all(|(argument, parameter)| match argument {
                None => !canonical_collision,
                Some(argument) if argument == "null" => !matches!(
                    parameter.as_str(),
                    "int" | "long" | "boolean" | "float" | "double" | "short" | "byte" | "char"
                ),
                Some(argument) => {
                    argument.rsplit("::").next() == parameter.rsplit("::").next()
                        || matches!(
                            (argument.as_str(), parameter.as_str()),
                            ("int", "long" | "float" | "double")
                        )
                }
            })
    }

    pub fn constructor_parameters(
        &self,
        name: &str,
        line: i64,
        ordinal: usize,
    ) -> Option<&[String]> {
        self.constructor_types
            .get(&(name.to_owned(), line))
            .and_then(|types| types.get(ordinal))
            .map(Vec::as_slice)
    }

    pub fn is_constructor(&self, name: &str, line: i64) -> bool {
        self.constructor_declarations
            .contains(&(name.to_owned(), line))
    }

    pub fn creation_arguments(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&[Option<String>]> {
        self.creation_types
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
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

    pub fn expression_variants(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> &[ExpressionCall] {
        self.expression_variants
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    pub fn return_type(&self, name: &str, line: i64, site: Option<usize>) -> Option<&str> {
        declaration_metadata(&self.returns, name, line, site)?.as_deref()
    }

    pub fn return_receiver(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
    ) -> Option<&JavaReceiver> {
        declaration_metadata(&self.return_receivers, name, line, site)
    }

    pub fn member_receiver(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
    ) -> Option<&JavaReceiver> {
        declaration_metadata(&self.member_receivers, name, line, site)
    }

    pub fn generated_getter_receiver(
        &self,
        class: &str,
        line: i64,
        name: &str,
    ) -> Option<&(i64, JavaReceiver)> {
        self.getter_receivers
            .get(&(class.to_owned(), line, name.to_owned()))?
            .as_ref()
    }

    pub fn parameter_index(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
        parameter: &str,
    ) -> Option<usize> {
        declaration_metadata(&self.type_parameters, name, line, site)?
            .iter()
            .position(|name| name == parameter)
    }

    pub fn type_parameter_count(&self, name: &str, line: i64, site: Option<usize>) -> usize {
        declaration_metadata(&self.type_parameters, name, line, site).map_or(0, Vec::len)
    }

    pub fn parent_receivers(&self, site: Option<usize>) -> &[(JavaReceiver, ReceiverTypeSite)] {
        site.and_then(|site| self.parent_receivers.get(&site))
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    pub fn return_parameter(&self, name: &str, line: i64, site: Option<usize>) -> Option<&str> {
        declaration_metadata(&self.return_parameters, name, line, site).map(String::as_str)
    }

    pub fn type_bound(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
        parameter: &str,
    ) -> Option<&str> {
        declaration_metadata(&self.type_bounds, name, line, site)?
            .get(parameter)
            .map(String::as_str)
    }

    pub fn type_declaration_at(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
    ) -> Option<&TypeDeclaration> {
        let declarations = self.declarations.get(&(name.to_owned(), line))?;
        match site {
            Some(site) => declarations
                .iter()
                .find(|declaration| declaration.site == site),
            None => match declarations.as_slice() {
                [declaration] => Some(declaration),
                _ => None,
            },
        }
    }

    #[cfg(test)]
    pub fn type_declaration(&self, name: &str, line: i64) -> Option<&TypeDeclaration> {
        self.type_declaration_at(name, line, None)
    }

    pub fn symbol_site(&self, name: &str, line: i64, kind: &str, ordinal: usize) -> Option<usize> {
        self.symbol_sites
            .get(&(name.to_owned(), line, kind.to_owned()))?
            .get(ordinal)
            .copied()
    }

    pub fn site_container(&self, site: usize) -> Option<&Option<usize>> {
        self.site_containers.get(&site)
    }

    pub fn member_container(&self, name: &str, line: i64) -> Option<&(String, i64)> {
        self.member_containers
            .get(&(name.to_owned(), line))?
            .as_ref()
    }

    /// Local names must stay inside their declaring block, including members
    /// of local classes. Byte positions distinguish adjacent scopes on a line.
    pub fn type_in_scope(
        &self,
        declaration: &TypeDeclaration,
        owner: &str,
        owner_line: i64,
        owner_site: Option<usize>,
        reference: Option<TypeReference<'_>>,
    ) -> bool {
        let Some(scope) = &declaration.local_scope else {
            return true;
        };
        if let Some(TypeReference::Position(position)) = reference {
            return scope.contains(&position);
        }
        let key = (owner.to_owned(), owner_line);
        let Some(range) = owner_site
            .and_then(|site| self.site_ranges.get(&site))
            .or_else(|| self.declaration_ranges.get(&key).and_then(Option::as_ref))
        else {
            return false;
        };
        if let Some(positions) = reference.and_then(|reference| match reference {
            TypeReference::Occurrences(line, name) => {
                self.type_positions.get(&(line, name.to_owned()))
            }
            TypeReference::Position(_) => None,
        }) {
            let mut positions = positions
                .iter()
                .filter(|position| range.contains(position))
                .peekable();
            return positions.peek().is_some()
                && positions.all(|position| scope.contains(position));
        }
        (scope.start <= range.start && range.end <= scope.end)
            || (self.parameters.contains_key(&key)
                && range.start <= scope.start
                && scope.end <= range.end)
    }

    pub fn type_reference(&self, line: i64, name: &str) -> Option<&Option<String>> {
        self.types.get(&(line, name.to_owned()))
    }

    pub fn type_positions_at(
        &self,
        line: i64,
        name: &str,
        owner_site: usize,
    ) -> Option<Vec<usize>> {
        let range = self.site_ranges.get(&owner_site)?;
        Some(
            self.type_positions
                .get(&(line, name.to_owned()))?
                .iter()
                .copied()
                .filter(|position| range.contains(position))
                .collect(),
        )
    }

    pub fn value_receiver(&self, line: i64, name: &str) -> bool {
        self.value_receivers.contains(&(line, name.to_owned()))
    }

    pub fn lexical_receiver_boundaries(&self, line: i64, name: &str) -> &[String] {
        self.lexical_receivers
            .get(&(line, name.to_owned()))
            .map_or(&[], Vec::as_slice)
    }

    pub fn type_reference_owner(&self, line: i64, name: &str) -> Option<&Option<(String, i64)>> {
        self.type_owners.get(&(line, name.to_owned()))
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

    pub fn accepts_arguments_at(
        &self,
        name: &str,
        line: i64,
        ordinal: usize,
        arguments: usize,
    ) -> bool {
        if !self
            .invocation_signatures
            .contains_key(&(name.to_owned(), line))
        {
            return self.accepts_arguments(name, line, arguments);
        }
        self.invocation_signature_at(name, line, ordinal)
            .is_some_and(|(parameters, variadic)| {
                arguments == parameters.len()
                    || (variadic && arguments >= parameters.len().saturating_sub(1))
            })
    }

    pub fn invocation_arguments(
        &self,
        owner: &str,
        owner_line: i64,
        line: i64,
        name: &str,
    ) -> Option<&[Option<InvocationArgument>]> {
        self.invocation_types
            .get(&(owner.to_owned(), owner_line, line, name.to_owned()))?
            .as_deref()
    }

    pub fn member_invocation_type(
        &self,
        name: &str,
        line: i64,
        site: Option<usize>,
    ) -> Option<&str> {
        declaration_metadata(&self.member_invocation_types, name, line, site)?.as_deref()
    }

    pub fn invocation_signature(&self, name: &str, line: i64) -> Option<(&[Option<String>], bool)> {
        let (count, variadic) = self.parameters.get(&(name.to_owned(), line))?.as_ref()?;
        if *count == 0 {
            return Some((&[], *variadic));
        }
        Some((
            self.invocation_parameters.get(&(name.to_owned(), line))?,
            *variadic,
        ))
    }

    pub fn invocation_signature_at(
        &self,
        name: &str,
        line: i64,
        ordinal: usize,
    ) -> Option<(&[Option<String>], bool)> {
        let Some(signatures) = self.invocation_signatures.get(&(name.to_owned(), line)) else {
            return self.invocation_signature(name, line);
        };
        let (parameters, variadic) = signatures.get(ordinal)?;
        Some((parameters, *variadic))
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
    use super::{JavaSource, TypeReference};

    #[test]
    fn colliding_local_types_keep_sites_containers_and_parents() {
        let source = r#"class Probe { void run() { { class Leaf extends Left { int same() { return 1; } } } { class Leaf extends Right { int same() { return 2; } } } } }"#;
        let java = JavaSource::parse(source).unwrap();
        let first = java.symbol_site("Leaf", 1, "class", 0).unwrap();
        let second = java.symbol_site("Leaf", 1, "class", 1).unwrap();
        assert_ne!(first, second);
        assert!(java.type_declaration("Probe::Leaf", 1).is_none());
        for (ordinal, site, parent) in [(0, first, "Left"), (1, second, "Right")] {
            assert_eq!(java.parent_types_at(Some(site)).unwrap(), [parent]);
            let member = java.symbol_site("same", 1, "function", ordinal).unwrap();
            assert_eq!(java.site_container(member), Some(&Some(site)));
            let declaration = java
                .type_declaration_at("Probe::Leaf", 1, Some(site))
                .unwrap();
            assert!(java.type_in_scope(declaration, "same", 1, Some(member), None));
            let other = java
                .symbol_site("same", 1, "function", 1 - ordinal)
                .unwrap();
            assert!(!java.type_in_scope(declaration, "same", 1, Some(other), None));
        }
    }

    #[test]
    fn member_metadata_requires_its_declaration_site_when_names_collide() {
        let source = r#"class Probe { void run() { { class Holder { Alpha slot; Alpha read() { return null; } } } { class Holder { Beta slot; Beta read() { return null; } } } } }"#;
        let java = JavaSource::parse(source).unwrap();
        assert!(java.member_receiver("slot", 1, None).is_none());
        assert!(java.return_type("read", 1, None).is_none());
        for (ordinal, ty) in [(0, "Alpha"), (1, "Beta")] {
            let field = java.symbol_site("slot", 1, "property", ordinal);
            assert!(
                matches!(java.member_receiver("slot", 1, field), Some(super::JavaReceiver::Type(name)) if name == ty)
            );
            let method = java.symbol_site("read", 1, "function", ordinal);
            assert_eq!(java.return_type("read", 1, method), Some(ty));
        }
    }

    #[test]
    fn generic_metadata_requires_exact_class_and_method_sites() {
        let source = r#"class Probe { void run() { { class Holder<T extends Alpha,X> { T read() { return null; } } } { class Holder<X,T extends Beta> { X read() { return null; } } } } }"#;
        let java = JavaSource::parse(source).unwrap();
        assert!(java.parameter_index("Holder", 1, None, "T").is_none());
        assert!(java.type_bound("Holder", 1, None, "T").is_none());
        assert!(java.return_parameter("read", 1, None).is_none());
        for (ordinal, index, bound, parameter) in [(0, 0, "Alpha", "T"), (1, 1, "Beta", "X")] {
            let class = java.symbol_site("Holder", 1, "class", ordinal);
            let method = java.symbol_site("read", 1, "function", ordinal);
            assert_eq!(java.parameter_index("Holder", 1, class, "T"), Some(index));
            assert_eq!(java.type_bound("Holder", 1, class, "T"), Some(bound));
            assert_eq!(java.return_parameter("read", 1, method), Some(parameter));
            assert!(java.parameter_index("Holder", 1, method, "T").is_none());
            assert!(java.return_parameter("read", 1, class).is_none());
        }
    }

    #[test]
    fn implicit_generic_accessors_keep_their_record_component_sites() {
        let source = r#"class Probe { void run() { { record Holder<T>(T read) {} } { record Holder<X,T>(T read) {} } } }"#;
        let java = JavaSource::parse(source).unwrap();
        assert!(java.return_parameter("read", 1, None).is_none());
        for ordinal in 0..2 {
            let component = java.symbol_site("read", 1, "property", ordinal);
            let accessor = java.symbol_site("read", 1, "function", ordinal);
            assert_eq!(accessor, component);
            assert_eq!(java.return_parameter("read", 1, accessor), Some("T"));
            assert!(java.accepts_arguments_at("read", 1, ordinal, 0));
            assert!(!java.accepts_arguments_at("read", 1, ordinal, 1));
            let class = java.symbol_site("Holder", 1, "class", ordinal);
            assert_eq!(java.site_container(accessor.unwrap()), Some(&class));
        }
    }

    #[test]
    fn receiver_type_sites_distinguish_positions_on_a_shared_line() {
        let source = "package fixture;\nclass Probe { int use() { Leaf before = null; class Leaf {} Leaf after = null; return 0; } }\n";
        let java = JavaSource::parse(source).unwrap();
        let declaration = java.type_declaration("fixture::Probe::Leaf", 2).unwrap();
        let before = source.find("Leaf before").unwrap();
        let after = source.find("Leaf after").unwrap();
        assert!(!java.type_in_scope(
            declaration,
            "use",
            2,
            None,
            Some(TypeReference::Position(before))
        ));
        assert!(java.type_in_scope(
            declaration,
            "use",
            2,
            None,
            Some(TypeReference::Position(after))
        ));
        // A line-only reference cannot establish which of the two sites was
        // meant. Preserve that conservative contract for stored reference rows.
        assert!(!java.type_in_scope(
            declaration,
            "use",
            2,
            None,
            Some(TypeReference::Occurrences(2, "Leaf"))
        ));
    }

    #[test]
    fn unresolved_lexical_qualifiers_remain_type_candidates() {
        let java = JavaSource::parse(
            r#"class Probe extends Base {
 int typeCall() { return Leaf.marker(); }
 int valueCall(Object Leaf) { return Leaf.hashCode(); }
}
"#,
        )
        .unwrap();
        assert!(!java.value_receiver(2, "Leaf"));
        assert_eq!(
            java.type_reference(2, "Leaf"),
            Some(&Some("Leaf".to_owned()))
        );
        assert_eq!(java.lexical_receiver_boundaries(2, "Leaf"), &["Probe"]);
        assert!(java.value_receiver(3, "Leaf"));
        assert!(java.lexical_receiver_boundaries(3, "Leaf").is_empty());
    }

    #[test]
    fn local_class_field_scope_includes_its_declared_type() {
        let java = JavaSource::parse(
            "package fixture;\nclass Probe {\n void method() {\n  class Leaf {\n   Leaf first, second;\n  }\n }\n}\n",
        )
        .unwrap();
        let declaration = java.type_declaration("fixture::Probe::Leaf", 4).unwrap();
        for field in ["first", "second"] {
            assert!(java.type_in_scope(
                declaration,
                field,
                5,
                None,
                Some(TypeReference::Occurrences(5, "Leaf"))
            ));
            assert!(java.type_in_scope(declaration, field, 5, None, None));
        }
        assert!(!java.type_in_scope(declaration, "Probe", 2, None, None));
    }

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
    fn type_reference_ownership_distinguishes_headers_and_same_line_methods() {
        let java = JavaSource::parse(
            r#"class Probe extends Base { Member use(Member input) { return input; } }
class Collision { Member first(Member x) { return x; } Member second(Member x) { return x; } }
class Fields { Member value; }
"#,
        )
        .unwrap();
        assert_eq!(
            java.type_reference_owner(1, "Base"),
            Some(&Some(("Probe".to_owned(), 1)))
        );
        assert_eq!(
            java.type_reference_owner(1, "Member"),
            Some(&Some(("Probe::use".to_owned(), 1)))
        );
        assert_eq!(java.type_reference_owner(2, "Member"), Some(&None));
        assert_eq!(
            java.type_reference_owner(3, "Member"),
            Some(&Some(("Fields::value".to_owned(), 3)))
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
