//! Tree-sitter based Java parser

use anyhow::Result;
use tree_sitter::{Language, Query, QueryCursor, StreamingIterator};
use std::sync::LazyLock;

use crate::db::SymbolKind;
use crate::parsers::ParsedSymbol;
use super::{LanguageParser, parse_tree, node_text, node_line, line_text};

static JAVA_LANGUAGE: LazyLock<Language> = LazyLock::new(|| tree_sitter_java::LANGUAGE.into());

static JAVA_QUERY: LazyLock<Query> = LazyLock::new(|| {
    Query::new(&JAVA_LANGUAGE, include_str!("queries/java.scm"))
        .expect("Failed to compile Java tree-sitter query")
});

pub static JAVA_PARSER: JavaParser = JavaParser;

pub struct JavaParser;

/// Significant Java/Spring annotations to track
const SIGNIFICANT_ANNOTATIONS: &[&str] = &[
    // Spring MVC / WebFlux
    "RestController", "Controller", "Service", "Repository", "Component",
    "Configuration", "Bean", "Qualifier",
    "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
    "RequestMapping", "RequestParam", "RequestBody", "PathVariable",
    "RequestHeader", "ResponseBody", "ResponseStatus", "ExceptionHandler",
    "Autowired", "Override", "Transactional",
    "SpringBootApplication", "EnableAutoConfiguration",
    // JPA / Hibernate
    "Entity", "Table", "Column", "Id", "GeneratedValue",
    "ManyToOne", "OneToMany", "ManyToMany", "OneToOne", "JoinColumn",
    "Embedded", "Embeddable",
    // Testing (JUnit 5 + Mockito)
    "Test", "BeforeEach", "AfterEach", "BeforeAll", "AfterAll",
    "ParameterizedTest", "MethodSource", "ValueSource", "CsvSource",
    "Mock", "InjectMocks", "Spy", "Captor",
    // DI (Dagger / JSR-330)
    "Inject", "Named", "Singleton", "Provides", "Binds", "Module",
    // Lombok
    "Data", "Value", "Builder",
    "AllArgsConstructor", "NoArgsConstructor", "RequiredArgsConstructor",
    "Getter", "Setter", "Slf4j", "Log4j2", "EqualsAndHashCode", "ToString",
    // Bean Validation
    "Valid", "Validated", "NotNull", "NotEmpty", "NotBlank",
    "Size", "Min", "Max", "Pattern", "Email",
];

impl LanguageParser for JavaParser {
    fn parse_symbols(&self, content: &str) -> Result<Vec<ParsedSymbol>> {
        let tree = parse_tree(content, &JAVA_LANGUAGE)?;
        let mut symbols = Vec::new();
        let query = &*JAVA_QUERY;
        let mut cursor = QueryCursor::new();

        let capture_names = query.capture_names();
        let idx = |name: &str| -> Option<u32> {
            capture_names.iter().position(|n| *n == name).map(|i| i as u32)
        };

        let idx_class_name = idx("class_name");
        let idx_class_node = idx("class_node");
        let idx_interface_name = idx("interface_name");
        let idx_interface_node = idx("interface_node");
        let idx_enum_name = idx("enum_name");
        let idx_enum_node = idx("enum_node");
        let idx_method_name = idx("method_name");
        let idx_method_node = idx("method_node");
        let idx_constructor_name = idx("constructor_name");
        let idx_constructor_node = idx("constructor_node");
        let idx_field_name = idx("field_name");
        let idx_field_node = idx("field_node");
        let idx_record_component_name = idx("record_component_name");
        let idx_record_component_node = idx("record_component_node");
        let idx_enum_constant_name = idx("enum_constant_name");
        let idx_enum_constant_node = idx("enum_constant_node");
        let idx_annotation_type_name = idx("annotation_type_name");
        let idx_annotation_type_node = idx("annotation_type_node");
        let idx_annotation_name = idx("annotation_name");
        let idx_annotation_call_name = idx("annotation_call_name");

        let mut emitted: std::collections::HashSet<(String, usize)> = std::collections::HashSet::new();
        let mut explicit_methods: std::collections::HashSet<(String, String)> = std::collections::HashSet::new();
        // (owner, base_name, symbol_name, line, accessor_signature)
        let mut pending_record_accessors: Vec<(String, String, String, usize, String)> = Vec::new();

        let mut matches = cursor.matches(query, tree.root_node(), content.as_bytes());

        while let Some(m) = matches.next() {
            // === Classes ===
            if let Some(name_cap) = find_capture(m, idx_class_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), line)) {
                    let parents = find_capture(m, idx_class_node)
                        .map(|n| extract_class_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Class,
                        line,
                        signature: line_text(content, line).trim().to_string(),
                        parents,
                    });
                }
                continue;
            }

            // === Interfaces ===
            if let Some(name_cap) = find_capture(m, idx_interface_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), line)) {
                    let parents = find_capture(m, idx_interface_node)
                        .map(|n| extract_interface_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Interface,
                        line,
                        signature: line_text(content, line).trim().to_string(),
                        parents,
                    });
                }
                continue;
            }

            // === Enums ===
            if let Some(name_cap) = find_capture(m, idx_enum_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), line)) {
                    let parents = find_capture(m, idx_enum_node)
                        .map(|n| extract_enum_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Enum,
                        line,
                        signature: line_text(content, line).trim().to_string(),
                        parents,
                    });
                }
                continue;
            }

            // === Methods (class / interface / enum / record / inner types) ===
            if let Some(name_cap) = find_capture(m, idx_method_name) {
                if let Some(node_cap) = find_capture(m, idx_method_node) {
                    if is_inside_type_body(&node_cap.node) {
                        let base_name = node_text(content, &name_cap.node);
                        let owner = enclosing_type_name(content, &node_cap.node);
                        // Track by (owner, unqualified_name) for record accessor dedup
                        if let Some(ref o) = owner {
                            explicit_methods.insert((o.clone(), base_name.to_string()));
                        }
                        let name = qualify(content, &node_cap.node, base_name);
                        let line = node_line(&name_cap.node);
                        let parents = member_of(owner);
                        if emitted.insert((name.clone(), line)) {
                            symbols.push(ParsedSymbol {
                                name,
                                kind: SymbolKind::Function,
                                line,
                                signature: line_text(content, line).trim().to_string(),
                                parents,
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
                        let base_name = node_text(content, &name_cap.node);
                        let name = qualify(content, &node_cap.node, base_name);
                        let line = node_line(&name_cap.node);
                        let parents = member_of(enclosing_type_name(content, &node_cap.node));
                        if emitted.insert((name.clone(), line)) {
                            symbols.push(ParsedSymbol {
                                name,
                                kind: SymbolKind::Function,
                                line,
                                signature: line_text(content, line).trim().to_string(),
                                parents,
                            });
                        }
                    }
                }
                continue;
            }

            // === Fields (class / interface / enum / record / inner types) ===
            if let Some(name_cap) = find_capture(m, idx_field_name) {
                if let Some(node_cap) = find_capture(m, idx_field_node) {
                    if is_inside_type_body(&node_cap.node) {
                        let base_name = node_text(content, &name_cap.node);
                        let name = qualify(content, &node_cap.node, base_name);
                        let line = node_line(&name_cap.node);
                        let parents = member_of(enclosing_type_name(content, &node_cap.node));
                        if emitted.insert((name.clone(), line)) {
                            // Interface fields are implicitly public static final even without modifiers
                            let kind = if is_static_final(&node_cap.node, content)
                                || is_interface_field(&node_cap.node)
                            {
                                SymbolKind::Constant
                            } else {
                                SymbolKind::Property
                            };
                            symbols.push(ParsedSymbol {
                                name,
                                kind,
                                line,
                                signature: line_text(content, line).trim().to_string(),
                                parents,
                            });
                        }
                    }
                }
                continue;
            }

            // === Record components ===
            if let Some(name_cap) = find_capture(m, idx_record_component_name) {
                if let Some(node_cap) = find_capture(m, idx_record_component_node) {
                    let base_name = node_text(content, &name_cap.node);
                    let line = node_line(&name_cap.node);
                    let component_signature = node_text(content, &node_cap.node).trim().to_string();
                    let owner = enclosing_type_name(content, &node_cap.node);
                    let symbol_name = qualify(content, &node_cap.node, base_name);
                    let accessor_sig = record_component_accessor_signature(content, &node_cap.node, base_name);
                    let parents = member_of(owner.clone());

                    if emitted.insert((symbol_name.clone(), line)) {
                        symbols.push(ParsedSymbol {
                            name: symbol_name.clone(),
                            kind: SymbolKind::Property,
                            line,
                            signature: component_signature,
                            parents,
                        });
                    }
                    pending_record_accessors.push((
                        owner.unwrap_or_default(),
                        base_name.to_string(),
                        symbol_name,
                        line,
                        accessor_sig,
                    ));
                }
                continue;
            }

            // === Enum constants ===
            if let Some(name_cap) = find_capture(m, idx_enum_constant_name) {
                if find_capture(m, idx_enum_constant_node).is_some() {
                    let base_name = node_text(content, &name_cap.node);
                    let line = node_line(&name_cap.node);
                    let name = qualify(content, &name_cap.node, base_name);
                    let parents = member_of(enclosing_type_name(content, &name_cap.node));
                    if emitted.insert((name.clone(), line)) {
                        symbols.push(ParsedSymbol {
                            name,
                            kind: SymbolKind::Constant,
                            line,
                            signature: line_text(content, line).trim().to_string(),
                            parents,
                        });
                    }
                }
                continue;
            }

            // === Annotation type declarations (@interface MyAnnotation) ===
            if let Some(name_cap) = find_capture(m, idx_annotation_type_name) {
                let name = node_text(content, &name_cap.node);
                let line = node_line(&name_cap.node);
                if emitted.insert((name.to_string(), line)) {
                    let parents = find_capture(m, idx_annotation_type_node)
                        .map(|n| extract_annotation_type_parents(content, &n.node))
                        .unwrap_or_default();
                    symbols.push(ParsedSymbol {
                        name: name.to_string(),
                        kind: SymbolKind::Interface,
                        line,
                        signature: line_text(content, line).trim().to_string(),
                        parents,
                    });
                }
                continue;
            }

            // === Marker annotations (no arguments) ===
            if let Some(name_cap) = find_capture(m, idx_annotation_name) {
                let name = node_text(content, &name_cap.node);
                if SIGNIFICANT_ANNOTATIONS.contains(&name) {
                    let line = node_line(&name_cap.node);
                    if emitted.insert((format!("@{}", name), line)) {
                        symbols.push(ParsedSymbol {
                            name: format!("@{}", name),
                            kind: SymbolKind::Annotation,
                            line,
                            signature: line_text(content, line).trim().to_string(),
                            parents: vec![],
                        });
                    }
                }
                continue;
            }

            // === Annotations with arguments ===
            if let Some(name_cap) = find_capture(m, idx_annotation_call_name) {
                let name = node_text(content, &name_cap.node);
                if SIGNIFICANT_ANNOTATIONS.contains(&name) {
                    let line = node_line(&name_cap.node);
                    if emitted.insert((format!("@{}", name), line)) {
                        symbols.push(ParsedSymbol {
                            name: format!("@{}", name),
                            kind: SymbolKind::Annotation,
                            line,
                            signature: line_text(content, line).trim().to_string(),
                            parents: vec![],
                        });
                    }
                }
                continue;
            }
        }

        // Java records synthesize public accessor methods for components unless explicitly overridden.
        for (owner, base_name, symbol_name, line, signature) in pending_record_accessors {
            if explicit_methods.contains(&(owner, base_name)) {
                continue;
            }
            if emitted.insert((format!("{}#record_accessor", symbol_name), line)) {
                symbols.push(ParsedSymbol {
                    name: symbol_name,
                    kind: SymbolKind::Function,
                    line,
                    signature,
                    parents: vec![],
                });
            }
        }

        Ok(symbols)
    }
}

/// Build a `member_of` parents vec from an optional enclosing type name.
fn member_of(owner: Option<String>) -> Vec<(String, String)> {
    owner.map(|n| vec![(n, "member_of".to_string())]).unwrap_or_default()
}

/// Returns true for single-uppercase-letter type parameters (T, E, K, V, R …)
/// and their numbered variants (T1, K2 …) to avoid treating generics as real parents.
fn is_type_parameter(name: &str) -> bool {
    let mut chars = name.chars();
    match (chars.next(), chars.next()) {
        (Some(first), None) => first.is_ascii_uppercase(),
        (Some(first), Some(second)) => {
            first.is_ascii_uppercase() && second.is_ascii_digit() && chars.next().is_none()
        }
        _ => false,
    }
}

/// Returns true when a field_declaration lives directly inside an interface body.
/// Interface fields are implicitly `public static final` even without those modifiers.
fn is_interface_field(field_node: &tree_sitter::Node) -> bool {
    field_node.parent().map(|p| p.kind() == "interface_body").unwrap_or(false)
}

/// Check if a node is inside a class/interface/enum/record body
fn is_inside_type_body(node: &tree_sitter::Node) -> bool {
    node.parent()
        .map(|p| matches!(
            p.kind(),
            "class_body" | "interface_body" | "enum_body" | "enum_body_declarations" | "record_body"
        ))
        .unwrap_or(false)
}

/// Walks up from `node` to find the nearest enclosing type declaration.
/// If that type declaration is itself nested inside another type body, returns its name.
/// Returns None when the nearest enclosing type is top-level.
///
/// Works uniformly for method/field/constructor nodes (parent is a type body),
/// record component nodes (nested inside formal_parameters → record_declaration),
/// and enum constant nodes (nested inside enum_body → enum_declaration).
fn nearest_nested_type_name(content: &str, node: &tree_sitter::Node) -> Option<String> {
    let mut cur = Some(*node);
    while let Some(n) = cur {
        if matches!(
            n.kind(),
            "class_declaration" | "interface_declaration" | "enum_declaration" |
            "record_declaration" | "annotation_type_declaration"
        ) {
            let parent = n.parent()?;
            return if matches!(
                parent.kind(),
                "class_body" | "interface_body" | "enum_body" | "record_body"
            ) {
                n.child_by_field_name("name")
                    .map(|name_node| node_text(content, &name_node).to_string())
            } else {
                None
            };
        }
        cur = n.parent();
    }
    None
}

/// Returns `"TypeName.base_name"` when the node is inside a nested type, otherwise `base_name`.
fn qualify(content: &str, node: &tree_sitter::Node, base_name: &str) -> String {
    match nearest_nested_type_name(content, node) {
        Some(owner) => format!("{}.{}", owner, base_name),
        None => base_name.to_string(),
    }
}

/// Build synthetic accessor signature for a record component (e.g. `String id()`).
fn record_component_accessor_signature(content: &str, component_node: &tree_sitter::Node, name: &str) -> String {
    if let Some(type_node) = component_node.child_by_field_name("type") {
        let mut type_text = node_text(content, &type_node).trim().to_string();
        if let Some(dim_node) = component_node.child_by_field_name("dimensions") {
            type_text.push_str(node_text(content, &dim_node).trim());
        }
        return format!("{} {}()", type_text, name);
    }
    format!("{}()", name)
}

/// Return the nearest enclosing type declaration name (class/interface/enum/record).
fn enclosing_type_name(content: &str, node: &tree_sitter::Node) -> Option<String> {
    let mut cur = Some(*node);
    while let Some(n) = cur {
        if matches!(n.kind(), "class_declaration" | "interface_declaration" | "enum_declaration" | "record_declaration") {
            if let Some(name_node) = n.child_by_field_name("name") {
                return Some(node_text(content, &name_node).to_string());
            }
        }
        cur = n.parent();
    }
    None
}

/// Extract parent types from a class_declaration (extends + implements + permits for sealed)
fn extract_class_parents(content: &str, class_node: &tree_sitter::Node) -> Vec<(String, String)> {
    let mut parents = Vec::new();
    let mut cursor = class_node.walk();

    for child in class_node.children(&mut cursor) {
        match child.kind() {
            "superclass" => {
                if let Some(name) = extract_type_from_parent_node(&child, content) {
                    parents.push((name, "extends".to_string()));
                }
            }
            "super_interfaces" => {
                extract_type_list(&child, content, "implements", &mut parents);
            }
            "permits" => {
                extract_type_list(&child, content, "permits", &mut parents);
            }
            _ => {}
        }
    }

    parents
}

/// Extract parent types from an interface_declaration (extends + permits for sealed)
fn extract_interface_parents(content: &str, iface_node: &tree_sitter::Node) -> Vec<(String, String)> {
    let mut parents = Vec::new();
    let mut cursor = iface_node.walk();

    for child in iface_node.children(&mut cursor) {
        match child.kind() {
            "extends_interfaces" => {
                extract_type_list(&child, content, "extends", &mut parents);
            }
            "permits" => {
                extract_type_list(&child, content, "permits", &mut parents);
            }
            _ => {}
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

/// Check if a field_declaration has both `static` and `final` modifiers
fn is_static_final(field_node: &tree_sitter::Node, content: &str) -> bool {
    let mut cursor = field_node.walk();
    for child in field_node.children(&mut cursor) {
        if child.kind() == "modifiers" {
            let mut has_static = false;
            let mut has_final = false;
            let mut mod_cursor = child.walk();
            for modifier in child.children(&mut mod_cursor) {
                match node_text(content, &modifier) {
                    "static" => has_static = true,
                    "final" => has_final = true,
                    _ => {}
                }
            }
            return has_static && has_final;
        }
    }
    false
}

/// Extract parent types from an annotation_type_declaration (meta-annotations)
fn extract_annotation_type_parents(_content: &str, _node: &tree_sitter::Node) -> Vec<(String, String)> {
    // Java @interface cannot extend/implement — no parents
    vec![]
}

/// Extract a single type name from a superclass node.
/// Filters out generic type parameters (T, E, K, V, T1, …).
fn extract_type_from_parent_node(node: &tree_sitter::Node, content: &str) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            "type_identifier" => {
                let name = node_text(content, &child).to_string();
                if !is_type_parameter(&name) {
                    return Some(name);
                }
            }
            "generic_type" => {
                // generic_type -> type_identifier type_arguments
                if let Some(first) = child.named_child(0) {
                    if first.kind() == "type_identifier" {
                        let name = node_text(content, &first).to_string();
                        if !is_type_parameter(&name) {
                            return Some(name);
                        }
                    }
                }
            }
            "scoped_type_identifier" => {
                let text = node_text(content, &child);
                if let Some(last) = text.rsplit('.').next() {
                    if !is_type_parameter(last) {
                        return Some(last.to_string());
                    }
                }
            }
            _ => {}
        }
    }
    None
}

/// Extract types from a type_list (super_interfaces, extends_interfaces, permits, …).
/// Filters out generic type parameters (T, E, K, V, T1, …).
fn extract_type_list(
    node: &tree_sitter::Node,
    content: &str,
    inherit_kind: &str,
    parents: &mut Vec<(String, String)>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            "type_list" => {
                extract_type_list(&child, content, inherit_kind, parents);
            }
            "type_identifier" => {
                let name = node_text(content, &child).to_string();
                if !is_type_parameter(&name) {
                    parents.push((name, inherit_kind.to_string()));
                }
            }
            "generic_type" => {
                if let Some(first) = child.named_child(0) {
                    if first.kind() == "type_identifier" {
                        let name = node_text(content, &first).to_string();
                        if !is_type_parameter(&name) {
                            parents.push((name, inherit_kind.to_string()));
                        }
                    }
                }
            }
            "scoped_type_identifier" => {
                let text = node_text(content, &child);
                if let Some(last) = text.rsplit('.').next() {
                    if !is_type_parameter(last) {
                        parents.push((last.to_string(), inherit_kind.to_string()));
                    }
                }
            }
            _ => {}
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

    /// One fixture covering all Java constructs: top-level and nested class, interface,
    /// enum, record, and @interface. All member names are unique across the fixture
    /// so assertions are unambiguous.
    const FIXTURE: &str = r#"
// ── Top-level class ───────────────────────────────────────────────────────────
@Service
public class TopClass extends TopBase implements TopIface {
    public static final int TC_CONST = 1;
    private String tcField;
    public TopClass(String tcField) { this.tcField = tcField; }
    public void tcMethod() {}
}

// ── Top-level interface ────────────────────────────────────────────────────────
public interface TopIface extends TopParent {
    void tifMethod();
}

// ── Top-level enum ─────────────────────────────────────────────────────────────
public enum TopEnum implements TopEnumIface {
    TE_A, TE_B, TE_C;
    public static final String TE_CONST = "x";
    private String teField;
    public void teMethod() {}
}

// ── Top-level record ───────────────────────────────────────────────────────────
public record TopRecord(String trId, String trName) implements TopRecordIface {
    public static final String TR_CONST = "r";
    public String trMethod() { return trId; }
    public String trId() { return trId.toUpperCase(); }  // explicit accessor override
}

// ── Top-level @interface ───────────────────────────────────────────────────────
@Retention(RetentionPolicy.RUNTIME)
public @interface TopAnnotation {
    String value() default "";
}

// ── Outer with all nested types ────────────────────────────────────────────────
public class Outer {
    public static final int OUTER_CONST = 42;
    private String outerField;
    public Outer() {}
    public void outerMethod() {}

    // inner class
    @Component
    public static class InnerClass extends InnerBase implements InnerIface {
        public static final int IC_CONST = 2;
        private String icField;
        public InnerClass() {}
        public void icMethod() {}
    }

    // inner interface
    public interface InnerIface extends InnerParent {
        void iifMethod();
    }

    // inner enum
    public enum InnerEnum implements InnerEnumIface {
        IE_ONE, IE_TWO;
        public static final String IE_CONST = "ie";
        private String ieField;
        public void ieMethod() {}
    }

    // inner record
    public record InnerRecord(String irId, String irName) implements InnerRecordIface {
        public static final String IR_CONST = "ir";
        public String irMethod() { return irId; }
        public String irId() { return irId.toLowerCase(); }  // explicit accessor override
    }

    // inner @interface
    public @interface InnerAnnotation {
        int timeout() default 0;
    }
}

// ── Non-significant annotations (must NOT be indexed) ─────────────────────────
@SuppressWarnings("unchecked")
public class Unchecked {
    @Deprecated
    public void legacyMethod() {}
}

// ── Sealed class with permits ──────────────────────────────────────────────────
public sealed class SealedShape permits ShapeCircle, ShapeRect {}
public final class ShapeCircle extends SealedShape {}
public non-sealed class ShapeRect extends SealedShape {}

// ── Sealed interface with permits ──────────────────────────────────────────────
public sealed interface SealedExpr permits LiteralExpr, BinaryExpr {}

// ── Interface with implicit constants (no static/final keywords) ───────────────
public interface NetworkConfig {
    int MAX_CONN = 100;
    String DEFAULT_HOST = "localhost";
}

// ── Interface with default method ─────────────────────────────────────────────
public interface Greetable {
    default String greet(String name) { return "Hello " + name; }
    void abstractGreet();
}

// ── Generic type parameter filtering ──────────────────────────────────────────
// T and E must NOT appear as parents; only real class names should be captured
public class GenericBox<T> extends BaseBox<T> implements Comparable<T> {}
public class MultiGeneric<K, V> extends AbstractMap<K, V> implements Map<K, V> {}

// ── Generic methods ────────────────────────────────────────────────────────────
public class GenericMethods {
    public <T> T identity(T input) { return input; }
    public <K, V> Map<K, V> toMap(K key, V value) { return null; }
    public <T extends Comparable<T>> T max(T a, T b) { return a; }
    public <E extends Exception> void sneakyThrow(E ex) throws E { throw ex; }
}

// ── Method annotations (RequestParam, etc.) ───────────────────────────────────
@RestController
public class DataController {
    @GetMapping("/items")
    public List<Item> listItems(@RequestParam String filter) { return null; }

    @PostMapping("/items")
    public Item createItem(@Valid @RequestBody Item item) { return null; }
}
"#;

    fn s() -> Vec<ParsedSymbol> {
        JAVA_PARSER.parse_symbols(FIXTURE).unwrap()
    }

    // ── Kinds ──────────────────────────────────────────────────────────────────

    #[test]
    fn top_class_kind() {
        assert!(s().iter().any(|s| s.name == "TopClass" && s.kind == SymbolKind::Class));
    }
    #[test]
    fn top_interface_kind() {
        assert!(s().iter().any(|s| s.name == "TopIface" && s.kind == SymbolKind::Interface));
    }
    #[test]
    fn top_enum_kind() {
        assert!(s().iter().any(|s| s.name == "TopEnum" && s.kind == SymbolKind::Enum));
    }
    #[test]
    fn top_record_kind() {
        assert!(s().iter().any(|s| s.name == "TopRecord" && s.kind == SymbolKind::Class));
    }
    #[test]
    fn top_annotation_type_kind() {
        assert!(s().iter().any(|s| s.name == "TopAnnotation" && s.kind == SymbolKind::Interface));
    }
    #[test]
    fn inner_class_kind() {
        assert!(s().iter().any(|s| s.name == "InnerClass" && s.kind == SymbolKind::Class));
    }
    #[test]
    fn inner_interface_kind() {
        assert!(s().iter().any(|s| s.name == "InnerIface" && s.kind == SymbolKind::Interface));
    }
    #[test]
    fn inner_enum_kind() {
        assert!(s().iter().any(|s| s.name == "InnerEnum" && s.kind == SymbolKind::Enum));
    }
    #[test]
    fn inner_record_kind() {
        assert!(s().iter().any(|s| s.name == "InnerRecord" && s.kind == SymbolKind::Class));
    }
    #[test]
    fn inner_annotation_type_kind() {
        assert!(s().iter().any(|s| s.name == "InnerAnnotation" && s.kind == SymbolKind::Interface));
    }

    // ── Inheritance / parents ──────────────────────────────────────────────────

    #[test]
    fn top_class_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "TopClass").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "TopBase" && k == "extends"));
        assert!(t.parents.iter().any(|(p, k)| p == "TopIface" && k == "implements"));
    }
    #[test]
    fn top_interface_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "TopIface").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "TopParent" && k == "extends"));
    }
    #[test]
    fn top_enum_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "TopEnum").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "TopEnumIface" && k == "implements"));
    }
    #[test]
    fn top_record_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "TopRecord").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "TopRecordIface" && k == "implements"));
    }
    #[test]
    fn inner_class_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "InnerClass").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "InnerBase" && k == "extends"));
        assert!(t.parents.iter().any(|(p, k)| p == "InnerIface" && k == "implements"));
    }
    #[test]
    fn inner_interface_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "InnerIface").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "InnerParent" && k == "extends"));
    }
    #[test]
    fn inner_enum_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "InnerEnum").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "InnerEnumIface" && k == "implements"));
    }
    #[test]
    fn inner_record_parents() {
        let syms = s();
        let t = syms.iter().find(|s| s.name == "InnerRecord").unwrap();
        assert!(t.parents.iter().any(|(p, k)| p == "InnerRecordIface" && k == "implements"));
    }

    // ── Top-level members ──────────────────────────────────────────────────────

    #[test]
    fn top_class_members() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "tcMethod" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "tcField" && s.kind == SymbolKind::Property));
        assert!(syms.iter().any(|s| s.name == "TC_CONST" && s.kind == SymbolKind::Constant));
        // constructor indexed as Function with class name
        assert!(syms.iter().any(|s| s.name == "TopClass" && s.kind == SymbolKind::Function));
    }
    #[test]
    fn top_enum_members() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "TE_A" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "TE_B" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "TE_C" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "TE_CONST" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "teField" && s.kind == SymbolKind::Property));
        assert!(syms.iter().any(|s| s.name == "teMethod" && s.kind == SymbolKind::Function));
    }
    #[test]
    fn top_record_members() {
        let syms = s();
        // components → Property
        assert!(syms.iter().any(|s| s.name == "trId" && s.kind == SymbolKind::Property && s.signature == "String trId"));
        assert!(syms.iter().any(|s| s.name == "trName" && s.kind == SymbolKind::Property && s.signature == "String trName"));
        // synthetic accessor for trName (no override)
        assert!(syms.iter().any(|s| s.name == "trName" && s.kind == SymbolKind::Function && s.signature == "String trName()"));
        // explicit method
        assert!(syms.iter().any(|s| s.name == "trMethod" && s.kind == SymbolKind::Function));
        // static final field
        assert!(syms.iter().any(|s| s.name == "TR_CONST" && s.kind == SymbolKind::Constant));
    }
    #[test]
    fn top_record_explicit_accessor_suppresses_synthetic() {
        let syms = s();
        // trId has explicit override → exactly one Function named trId (the explicit one)
        assert_eq!(syms.iter().filter(|s| s.name == "trId" && s.kind == SymbolKind::Function).count(), 1);
        assert!(syms.iter().any(|s| s.name == "trId"
            && s.kind == SymbolKind::Function
            && s.signature.contains("toUpperCase")));
    }
    #[test]
    fn top_interface_members() {
        assert!(s().iter().any(|s| s.name == "tifMethod" && s.kind == SymbolKind::Function));
    }

    // ── Outer members (top-level, unqualified) ─────────────────────────────────

    #[test]
    fn outer_members_unqualified() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "outerMethod" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "outerField" && s.kind == SymbolKind::Property));
        assert!(syms.iter().any(|s| s.name == "OUTER_CONST" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "Outer" && s.kind == SymbolKind::Function)); // constructor
    }

    // ── Inner class members (qualified) ───────────────────────────────────────

    #[test]
    fn inner_class_members_qualified() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "InnerClass.icMethod" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "InnerClass.icField" && s.kind == SymbolKind::Property));
        assert!(syms.iter().any(|s| s.name == "InnerClass.IC_CONST" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "InnerClass.InnerClass" && s.kind == SymbolKind::Function));
        // unqualified names must NOT appear
        assert!(!syms.iter().any(|s| s.name == "icMethod"));
        assert!(!syms.iter().any(|s| s.name == "icField"));
    }
    #[test]
    fn inner_class_annotation_indexed() {
        assert!(s().iter().any(|s| s.name == "@Component" && s.kind == SymbolKind::Annotation));
    }

    // ── Inner interface members (qualified) ───────────────────────────────────

    #[test]
    fn inner_interface_members_qualified() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "InnerIface.iifMethod" && s.kind == SymbolKind::Function));
        assert!(!syms.iter().any(|s| s.name == "iifMethod"));
    }

    // ── Inner enum members (qualified) ────────────────────────────────────────

    #[test]
    fn inner_enum_members_qualified() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "InnerEnum.IE_ONE" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "InnerEnum.IE_TWO" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "InnerEnum.IE_CONST" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "InnerEnum.ieField" && s.kind == SymbolKind::Property));
        assert!(syms.iter().any(|s| s.name == "InnerEnum.ieMethod" && s.kind == SymbolKind::Function));
        // unqualified names must NOT appear
        assert!(!syms.iter().any(|s| s.name == "IE_ONE"));
        assert!(!syms.iter().any(|s| s.name == "ieMethod"));
    }

    // ── Inner record members (qualified) ─────────────────────────────────────

    #[test]
    fn inner_record_members_qualified() {
        let syms = s();
        // components → qualified Property
        assert!(syms.iter().any(|s| s.name == "InnerRecord.irId" && s.kind == SymbolKind::Property && s.signature == "String irId"));
        assert!(syms.iter().any(|s| s.name == "InnerRecord.irName" && s.kind == SymbolKind::Property && s.signature == "String irName"));
        // synthetic accessor for irName (no override)
        assert!(syms.iter().any(|s| s.name == "InnerRecord.irName" && s.kind == SymbolKind::Function && s.signature == "String irName()"));
        // explicit method
        assert!(syms.iter().any(|s| s.name == "InnerRecord.irMethod" && s.kind == SymbolKind::Function));
        // static final field
        assert!(syms.iter().any(|s| s.name == "InnerRecord.IR_CONST" && s.kind == SymbolKind::Constant));
        // unqualified names must NOT appear
        assert!(!syms.iter().any(|s| s.name == "irId"));
        assert!(!syms.iter().any(|s| s.name == "irName"));
    }
    #[test]
    fn inner_record_explicit_accessor_suppresses_synthetic() {
        let syms = s();
        // irId has explicit override → exactly one Function named InnerRecord.irId
        assert_eq!(syms.iter().filter(|s| s.name == "InnerRecord.irId" && s.kind == SymbolKind::Function).count(), 1);
        assert!(syms.iter().any(|s| s.name == "InnerRecord.irId"
            && s.kind == SymbolKind::Function
            && s.signature.contains("toLowerCase")));
    }

    // ── Annotations ──────────────────────────────────────────────────────────

    #[test]
    fn significant_annotations_indexed() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "@Service" && s.kind == SymbolKind::Annotation));
        assert!(syms.iter().any(|s| s.name == "@Component" && s.kind == SymbolKind::Annotation));
    }
    #[test]
    fn nonsignificant_annotations_not_indexed() {
        let syms = s();
        assert!(!syms.iter().any(|s| s.name == "@SuppressWarnings"));
        assert!(!syms.iter().any(|s| s.name == "@Deprecated"));
        // class and method from that file still indexed
        assert!(syms.iter().any(|s| s.name == "Unchecked" && s.kind == SymbolKind::Class));
        assert!(syms.iter().any(|s| s.name == "legacyMethod" && s.kind == SymbolKind::Function));
    }

    // ── member_of parent ──────────────────────────────────────────────────────

    #[test]
    fn members_carry_member_of_parent() {
        let syms = s();
        // class method
        let m = syms.iter().find(|s| s.name == "tcMethod").unwrap();
        assert!(m.parents.iter().any(|(p, k)| p == "TopClass" && k == "member_of"));
        // class field
        let f = syms.iter().find(|s| s.name == "tcField").unwrap();
        assert!(f.parents.iter().any(|(p, k)| p == "TopClass" && k == "member_of"));
        // class constant
        let c = syms.iter().find(|s| s.name == "TC_CONST").unwrap();
        assert!(c.parents.iter().any(|(p, k)| p == "TopClass" && k == "member_of"));
        // constructor
        let ctor = syms.iter().find(|s| s.name == "TopClass" && s.kind == SymbolKind::Function).unwrap();
        assert!(ctor.parents.iter().any(|(p, k)| p == "TopClass" && k == "member_of"));
        // enum constant
        let ec = syms.iter().find(|s| s.name == "TE_A").unwrap();
        assert!(ec.parents.iter().any(|(p, k)| p == "TopEnum" && k == "member_of"));
        // enum method
        let em = syms.iter().find(|s| s.name == "teMethod").unwrap();
        assert!(em.parents.iter().any(|(p, k)| p == "TopEnum" && k == "member_of"));
        // record component
        let rc = syms.iter().find(|s| s.name == "trId" && s.kind == SymbolKind::Property).unwrap();
        assert!(rc.parents.iter().any(|(p, k)| p == "TopRecord" && k == "member_of"));
        // interface method
        let im = syms.iter().find(|s| s.name == "tifMethod").unwrap();
        assert!(im.parents.iter().any(|(p, k)| p == "TopIface" && k == "member_of"));
    }

    #[test]
    fn inner_members_carry_member_of_inner_type() {
        let syms = s();
        // inner class method
        let m = syms.iter().find(|s| s.name == "InnerClass.icMethod").unwrap();
        assert!(m.parents.iter().any(|(p, k)| p == "InnerClass" && k == "member_of"));
        // inner enum constant
        let ec = syms.iter().find(|s| s.name == "InnerEnum.IE_ONE").unwrap();
        assert!(ec.parents.iter().any(|(p, k)| p == "InnerEnum" && k == "member_of"));
        // inner record component
        let rc = syms.iter().find(|s| s.name == "InnerRecord.irId" && s.kind == SymbolKind::Property).unwrap();
        assert!(rc.parents.iter().any(|(p, k)| p == "InnerRecord" && k == "member_of"));
    }

    // ── Sealed types / permits ────────────────────────────────────────────────

    #[test]
    fn sealed_class_permits_extracted() {
        let syms = s();
        let cls = syms.iter().find(|s| s.name == "SealedShape").unwrap();
        assert!(cls.parents.iter().any(|(p, k)| p == "ShapeCircle" && k == "permits"));
        assert!(cls.parents.iter().any(|(p, k)| p == "ShapeRect" && k == "permits"));
    }

    #[test]
    fn sealed_interface_permits_extracted() {
        let syms = s();
        let iface = syms.iter().find(|s| s.name == "SealedExpr").unwrap();
        assert!(iface.parents.iter().any(|(p, k)| p == "LiteralExpr" && k == "permits"));
        assert!(iface.parents.iter().any(|(p, k)| p == "BinaryExpr" && k == "permits"));
    }

    // ── Interface constants ───────────────────────────────────────────────────

    #[test]
    fn interface_implicit_constants_are_constant_kind() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "MAX_CONN" && s.kind == SymbolKind::Constant));
        assert!(syms.iter().any(|s| s.name == "DEFAULT_HOST" && s.kind == SymbolKind::Constant));
    }

    #[test]
    fn interface_implicit_constants_carry_member_of() {
        let syms = s();
        let c = syms.iter().find(|s| s.name == "MAX_CONN").unwrap();
        assert!(c.parents.iter().any(|(p, k)| p == "NetworkConfig" && k == "member_of"));
    }

    // ── Default interface methods ─────────────────────────────────────────────

    #[test]
    fn interface_default_and_abstract_methods_indexed() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "greet" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "abstractGreet" && s.kind == SymbolKind::Function));
    }

    #[test]
    fn interface_methods_carry_member_of() {
        let syms = s();
        let m = syms.iter().find(|s| s.name == "greet").unwrap();
        assert!(m.parents.iter().any(|(p, k)| p == "Greetable" && k == "member_of"));
    }

    // ── Generic type parameter filtering ─────────────────────────────────────

    #[test]
    fn generic_type_params_not_in_parents() {
        let syms = s();
        // GenericBox<T> extends BaseBox<T> implements Comparable<T>
        // T must not appear as a parent
        let cls = syms.iter().find(|s| s.name == "GenericBox").unwrap();
        assert!(!cls.parents.iter().any(|(p, _)| p == "T"));
        assert!(cls.parents.iter().any(|(p, k)| p == "BaseBox" && k == "extends"));
        assert!(cls.parents.iter().any(|(p, k)| p == "Comparable" && k == "implements"));
        // MultiGeneric<K,V> — K and V must not appear
        let cls2 = syms.iter().find(|s| s.name == "MultiGeneric").unwrap();
        assert!(!cls2.parents.iter().any(|(p, _)| p == "K" || p == "V"));
        assert!(cls2.parents.iter().any(|(p, k)| p == "AbstractMap" && k == "extends"));
    }

    // ── Generic methods ───────────────────────────────────────────────────────

    #[test]
    fn generic_methods_indexed_by_name() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "identity" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "toMap" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "max" && s.kind == SymbolKind::Function));
        assert!(syms.iter().any(|s| s.name == "sneakyThrow" && s.kind == SymbolKind::Function));
    }

    #[test]
    fn generic_method_type_params_not_indexed_as_symbols() {
        let syms = s();
        for name in &["T", "K", "V", "E"] {
            assert!(
                !syms.iter().any(|s| s.name.as_str() == *name),
                "type parameter {name} must not be indexed as a symbol"
            );
        }
    }

    #[test]
    fn generic_method_type_params_not_in_parents() {
        let syms = s();
        let identity = syms.iter().find(|s| s.name == "identity").unwrap();
        assert!(identity.parents.iter().any(|(p, k)| p == "GenericMethods" && k == "member_of"));
        assert!(!identity.parents.iter().any(|(p, _)| p == "T"));

        let to_map = syms.iter().find(|s| s.name == "toMap").unwrap();
        assert!(!to_map.parents.iter().any(|(p, _)| p == "K" || p == "V"));

        // <T extends Comparable<T>> — T and Comparable must not leak into method parents
        let max = syms.iter().find(|s| s.name == "max").unwrap();
        assert!(!max.parents.iter().any(|(p, _)| p == "T" || p == "Comparable"));
    }

    // ── Method annotations ────────────────────────────────────────────────────

    #[test]
    fn method_param_annotations_indexed() {
        let syms = s();
        assert!(syms.iter().any(|s| s.name == "@RequestParam" && s.kind == SymbolKind::Annotation));
        assert!(syms.iter().any(|s| s.name == "@Valid" && s.kind == SymbolKind::Annotation));
        assert!(syms.iter().any(|s| s.name == "@RequestBody" && s.kind == SymbolKind::Annotation));
    }

    // ── Isolated parses ───────────────────────────────────────────────────────

    #[test]
    fn comments_do_not_produce_symbols() {
        let syms = JAVA_PARSER.parse_symbols(
            "// class FakeClass {}\npublic class RealClass {}\n/* void fakeMethod() {} */\n"
        ).unwrap();
        assert!(syms.iter().any(|s| s.name == "RealClass"));
        assert!(!syms.iter().any(|s| s.name == "FakeClass"));
        assert!(!syms.iter().any(|s| s.name == "fakeMethod"));
    }

    #[test]
    fn empty_record_has_no_components_or_accessors() {
        let syms = JAVA_PARSER.parse_symbols("public record Empty() {}\n").unwrap();
        assert!(syms.iter().any(|s| s.name == "Empty" && s.kind == SymbolKind::Class));
        assert_eq!(syms.iter().filter(|s| s.kind == SymbolKind::Property).count(), 0);
        assert_eq!(syms.iter().filter(|s| s.kind == SymbolKind::Function).count(), 0);
    }
}
