use ast_index::parsers::{parse_file_symbols, FileType};

#[test]
fn java_method_references_are_usages_and_prose_is_not() {
    let source = r#"class Example {
    void work() {}
    Runnable bound = this::work;
    Runnable generic = this::<String>work;
    java.util.function.Supplier<Example> constructor = Example::new;
    String prose = "this::hidden";
    // this::commented
    Runnable duplicate = this::work;
}"#;
    let (_, refs) = parse_file_symbols(source, FileType::Java).unwrap();
    let lines: Vec<_> = refs
        .iter()
        .filter(|r| r.name == "work")
        .map(|r| r.line)
        .collect();
    assert_eq!(lines, [3, 4, 8]);
    assert!(!refs
        .iter()
        .any(|r| matches!(r.name.as_str(), "new" | "hidden" | "commented" | "this")));
    assert!(refs.iter().any(|r| r.name == "Example" && r.line == 5));
}

#[test]
fn java_references_include_fields_short_calls_and_same_line_self_use() {
    let source = r#"package example;
import example.Base;
import static example.Base.LIMIT;
class Example {
    int value;
    void go() {}
    int read() { go(); return this.value; }
    Example copy() { return new Example(); }
    String prose = "value go() Example";
    // value go() Example
}
"#;
    let (symbols, refs) = parse_file_symbols(source, FileType::Java).unwrap();
    assert!(symbols
        .iter()
        .any(|s| s.name == "Base" && s.kind == ast_index::db::SymbolKind::Import));
    assert!(symbols
        .iter()
        .any(|s| s.name == "LIMIT" && s.kind == ast_index::db::SymbolKind::Import));
    for (name, line) in [("value", 7), ("go", 7), ("Example", 8)] {
        assert!(
            refs.iter().any(|r| r.name == name && r.line == line),
            "missing reference {name}:{line}"
        );
    }
    assert!(!refs.iter().any(|r| matches!(r.line, 2 | 3 | 10)));
    assert!(!refs.iter().any(|r| r.line == 9 && matches!(r.name.as_str(), "value" | "go" | "Example")));
}
