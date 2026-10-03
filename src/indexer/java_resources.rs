//! Literal Android namespace ownership for Java resource expressions.
use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use anyhow::Result;
use regex::Regex;
use rusqlite::Connection;

/// Read literal namespace declarations inside an android block, without executing Gradle.
fn literal_namespace(content: &str) -> Option<String> {
    static TOKENS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r#"(?s)//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*|[^\s]"#,
        )
        .unwrap()
    });
    static NAME: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$").unwrap()
    });
    let tokens: Vec<&str> = TOKENS
        .find_iter(content)
        .map(|m| m.as_str())
        .filter(|s| !s.starts_with("//") && !s.starts_with("/*"))
        .collect();
    let mut depth = 0usize;
    let mut android_depth = None;
    let mut namespaces = Vec::new();
    for (index, token) in tokens.iter().enumerate() {
        match *token {
            "{" => {
                depth += 1;
                if index > 0 && tokens[index - 1] == "android" {
                    android_depth = Some(depth);
                }
            }
            "}" => {
                if android_depth == Some(depth) {
                    android_depth = None;
                }
                depth = depth.saturating_sub(1);
            }
            "namespace" if android_depth == Some(depth) => {
                let mut value = index + 1;
                if tokens.get(value).is_some_and(|v| matches!(*v, "=" | "(")) {
                    value += 1;
                }
                let token = tokens.get(value)?;
                if !token.starts_with(['\'', '"']) {
                    return None;
                }
                let name = &token[1..token.len() - 1];
                if !NAME.is_match(name) {
                    return None;
                }
                if tokens
                    .get(value + 1)
                    .is_some_and(|v| matches!(*v, "+" | "." | "?" | "["))
                {
                    return None;
                }
                namespaces.push(name.to_owned());
            }
            _ => {}
        }
    }
    namespaces
        .first()
        .filter(|first| namespaces.iter().all(|v| v == *first))
        .cloned()
}

/// Read only exact module build paths with the source parser's file-size budget.
pub(super) fn namespace_owners(
    conn: &Connection,
    root: &Path,
) -> Result<HashMap<String, Vec<i64>>> {
    let mut namespaces: HashMap<String, Vec<i64>> = HashMap::new();
    let mut statement = conn.prepare("SELECT id,path FROM modules")?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (id, path) = row?;
        let mut values = Vec::new();
        for name in ["build.gradle", "build.gradle.kts"] {
            let file = root.join(&path).join(name);
            if !fs::metadata(&file).is_ok_and(|m| m.len() <= super::max_file_size_bytes()) {
                continue;
            }
            if let Ok(file) = fs::File::open(file) {
                let limit = super::max_file_size_bytes();
                let mut content = String::new();
                if file
                    .take(limit.saturating_add(1))
                    .read_to_string(&mut content)
                    .is_ok()
                    && content.len() as u64 <= limit
                {
                    if let Some(namespace) = literal_namespace(&content) {
                        values.push(namespace);
                    }
                }
            }
        }
        if let Some(namespace) = values
            .first()
            .filter(|first| values.iter().all(|v| v == *first))
        {
            namespaces.entry(namespace.clone()).or_default().push(id);
        }
    }
    Ok(namespaces)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literal_namespaces_ignore_comments_strings_and_computed_values() {
        for source in [
            "android { namespace 'fixture.app' }",
            "android { namespace = \"fixture.app\" }",
            "android { namespace(\"fixture.app\") }",
        ] {
            assert_eq!(literal_namespace(source).as_deref(), Some("fixture.app"));
        }
        assert_eq!(
            literal_namespace(
                r#"
            // android { namespace 'wrong' }
            def text = "android { namespace 'also.wrong' }"
            android { /* namespace 'wrong' */ namespace 'fixture.app' }
        "#
            )
            .as_deref(),
            Some("fixture.app")
        );
        assert_eq!(literal_namespace("android { namespace packageName }"), None);
        assert_eq!(
            literal_namespace("android { namespace 'fixture.app' + suffix }"),
            None
        );
        assert_eq!(
            literal_namespace("android { namespace 'one'; namespace 'two' }"),
            None
        );
        assert_eq!(
            literal_namespace("android { defaultConfig { namespace 'wrong' } }"),
            None
        );
    }
}
