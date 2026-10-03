//! Tree-sitter based Java parser

use anyhow::Result;
use std::collections::{HashMap, VecDeque};
use std::sync::LazyLock;
use tree_sitter::{Language, Query, QueryCursor, StreamingIterator};

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
    let mut imported_r = Vec::new();
    let mut aliases: HashMap<String, Vec<String>> = HashMap::new();
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
        let Some(r) = names.iter().rposition(|p| p == "R") else {
            continue;
        };
        if r == 0 {
            continue;
        }
        let namespace = names[..r].join(".");
        let mut cursor = declaration.walk();
        let is_static = declaration
            .children(&mut cursor)
            .any(|n| n.kind() == "static");
        match (&names[r + 1..], is_static) {
            ([], false) => imported_r.push(namespace),
            ([kind], false) => aliases.entry(kind.clone()).or_default().push(namespace),
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
            });
        };
        if node.kind() == "field_access" {
            if let Some(names) = parts(node, content) {
                let n = names.len();
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

/// Java dependency anchors retain qualified type spelling and import ownership.
#[derive(Default)]
pub(crate) struct DependencySyntax {
    pub package: String,
    pub imports: Vec<(String, bool)>,
    pub types: std::collections::BTreeSet<String>,
    pub declarations: std::collections::HashSet<String>,
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
    let mut result = DependencySyntax::default();
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
                        result.types.insert(spelling(node, content));
                    }
                }
            }
            "annotation" | "marker_annotation" => {
                if let Some(name) = node.child_by_field_name("name") {
                    result.types.insert(spelling(name, content));
                }
            }
            "method_invocation" | "field_access" => {
                if let Some(object) = node.child_by_field_name("object") {
                    if matches!(
                        object.kind(),
                        "identifier" | "scoped_identifier" | "field_access"
                    ) {
                        result.types.insert(spelling(object, content));
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
        let mut explicit_methods: std::collections::HashSet<(String, String)> =
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
                            if let Some(owner) = enclosing_type_name(content, &node_cap.node) {
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
                    let owner = enclosing_type_name(content, &node_cap.node).unwrap_or_default();

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
pub(crate) fn invocation_callers(
    content: &str,
    names: &[String],
    limit: usize,
) -> Result<Vec<Vec<(String, usize)>>> {
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
                    let site = (node_text(content, &name).to_string(), node_line(&name));
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
                let owner = enclosing_type_name(content, &capture.node).unwrap_or_default();
                accessors.push((owner, name.to_string(), node_line(&capture.node), qualified));
            } else if query.capture_names()[capture.index as usize] == "method_name" {
                if let Some(method) = capture.node.parent() {
                    if method
                        .child_by_field_name("parameters")
                        .is_some_and(|parameters| parameters.named_child_count() == 0)
                    {
                        if let Some(owner) = enclosing_type_name(content, &method) {
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

/// Return the nearest enclosing type declaration name (class/interface/enum/record).
fn enclosing_type_name(content: &str, node: &tree_sitter::Node) -> Option<String> {
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
                return Some(node_text(content, &name_node).to_string());
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
