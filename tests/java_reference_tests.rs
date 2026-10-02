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
