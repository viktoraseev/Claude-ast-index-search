//! Proven Android namespace metadata and dependency owners for Java resources.
use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use anyhow::Result;
use regex::Regex;
use rusqlite::Connection;

/// Read literal namespace values and definite top-level aliases without executing Gradle.
fn namespace_metadata(content: &str) -> Option<Option<String>> {
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
    let mut variables: HashMap<&str, Option<&str>> = HashMap::new();
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
                let Some(mut token) = tokens.get(value).copied() else {
                    return Some(None);
                };
                if !token.starts_with(['\'', '"']) {
                    let Some(Some(literal)) = variables.get(token) else {
                        return Some(None);
                    };
                    token = literal;
                }
                if !token.starts_with(['\'', '"']) {
                    return Some(None);
                }
                let name = &token[1..token.len() - 1];
                if !NAME.is_match(name) {
                    return Some(None);
                }
                if tokens
                    .get(value + 1)
                    .is_some_and(|v| matches!(*v, "+" | "." | "?" | "["))
                {
                    return Some(None);
                }
                namespaces.push(name.to_owned());
            }
            name if depth == 0 && tokens.get(index + 1) == Some(&"=") => {
                let literal = tokens.get(index + 2).copied().filter(|value| {
                    value.starts_with(['\'', '"'])
                        && value.len() > 2
                        && NAME.is_match(&value[1..value.len() - 1])
                        && !tokens
                            .get(index + 3)
                            .is_some_and(|v| matches!(*v, "+" | "." | "?" | "["))
                });
                // Reassignment or unknown expressions cannot establish a
                // namespace. Do not run Gradle to guess their values.
                variables
                    .entry(name)
                    .and_modify(|value| *value = None)
                    .or_insert(literal);
            }
            _ => {}
        }
    }
    if namespaces.is_empty() {
        None
    } else {
        Some(
            namespaces
                .first()
                .filter(|first| namespaces.iter().all(|v| v == *first))
                .cloned(),
        )
    }
}

#[cfg(test)]
fn literal_namespace(content: &str) -> Option<String> {
    namespace_metadata(content).flatten()
}

/// Read only exact module build paths with the source parser's file-size budget.
pub(super) fn namespace_owners(
    conn: &Connection,
    root: &Path,
) -> Result<HashMap<String, Vec<i64>>> {
    let mut namespaces: HashMap<String, Vec<i64>> = HashMap::new();
    let mut statement = conn.prepare("SELECT id,path,root_path FROM modules")?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    for row in rows {
        let (id, path, owner) = row?;
        let directory = if owner.is_empty() {
            root.join(&path)
        } else {
            Path::new(&owner).join(&path)
        };
        let mut values = Vec::new();
        let mut declared = false;
        for name in ["build.gradle", "build.gradle.kts"] {
            let file = directory.join(name);
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
                    if let Some(namespace) = namespace_metadata(&content) {
                        declared = true;
                        values.push(namespace);
                    }
                }
            }
        }
        if !declared {
            let manifest = directory.join("src/main/AndroidManifest.xml");
            if fs::metadata(&manifest).is_ok_and(|m| m.len() <= super::max_file_size_bytes()) {
                if let Ok(content) = crate::commands::grep::read_java_syntax_source(
                    &manifest,
                    super::max_file_size_bytes(),
                ) {
                    let visible = super::android_xml::visible(&content);
                    for tag in super::android_xml::tags(&visible) {
                        if tag.name == "manifest" {
                            if let Some(package) = tag.attribute("package") {
                                values.push(Some(package.value.to_string()));
                            }
                        }
                    }
                }
            }
        }
        if let Some(Some(namespace)) = values
            .first()
            .filter(|first| values.iter().all(|v| v == *first))
        {
            namespaces.entry(namespace.clone()).or_default().push(id);
        }
    }
    Ok(namespaces)
}

/// Follow declared dependencies for transitive R and legacy Maven resources.
pub(super) fn merged_owners(conn: &Connection, root: &Path) -> Result<HashMap<i64, Vec<i64>>> {
    let mut merged = HashMap::new();
    let mut modules = conn.prepare("SELECT id,path,root_path FROM modules")?;
    let rows = modules.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    for row in rows {
        let (id, path, owner) = row?;
        let base = if owner.is_empty() {
            root
        } else {
            Path::new(&owner)
        };
        let directory = base.join(&path);
        let mut setting = None;
        for file in [
            base.join("gradle.properties"),
            directory.join("gradle.properties"),
        ] {
            if fs::metadata(&file).is_ok_and(|m| m.len() <= super::max_file_size_bytes()) {
                if let Ok(content) = crate::commands::grep::read_java_syntax_source(
                    &file,
                    super::max_file_size_bytes(),
                ) {
                    for line in content.lines().map(str::trim) {
                        if let Some((key, value)) = line.split_once('=') {
                            if key.trim() == "android.nonTransitiveRClass" {
                                setting = Some(value.trim() == "false");
                            }
                        }
                    }
                }
            }
        }
        // Preserve Maven's legacy merged resource lookup within its declared
        // dependency graph. Gradle projects still require an explicit R mode;
        // an explicit non-transitive setting overrides this compatibility path.
        let legacy_maven = setting.is_none()
            && directory.join("pom.xml").is_file()
            && !directory.join("build.gradle").is_file()
            && !directory.join("build.gradle.kts").is_file();
        if setting == Some(true) || legacy_maven {
            let mut statement = conn.prepare("WITH RECURSIVE owners(id) AS (
                SELECT dep_module_id FROM module_deps WHERE module_id=?1 AND dep_module_id IS NOT NULL
                UNION SELECT d.dep_module_id FROM module_deps d JOIN owners o ON o.id=d.module_id WHERE d.dep_module_id IS NOT NULL
            ) SELECT id FROM owners WHERE id!=?1 ORDER BY id")?;
            merged.insert(
                id,
                statement
                    .query_map([id], |row| row.get(0))?
                    .collect::<rusqlite::Result<Vec<i64>>>()?,
            );
        }
    }
    Ok(merged)
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
        assert_eq!(
            literal_namespace("def owner='fixture.app'; android { namespace owner }").as_deref(),
            Some("fixture.app")
        );
        assert_eq!(
            literal_namespace("val owner=\"fixture.app\"; android { namespace=owner }").as_deref(),
            Some("fixture.app")
        );
        assert_eq!(
            literal_namespace("def owner='fixture.app'+suffix; android { namespace owner }"),
            None
        );
        assert_eq!(
            literal_namespace(
                "def owner='fixture.app'; owner='wrong'; android { namespace owner }"
            ),
            None
        );
        assert_eq!(
            namespace_metadata("android { namespace project.packageName }"),
            Some(None)
        );
        assert_eq!(namespace_metadata("android { defaultConfig {} }"), None);
    }
}
