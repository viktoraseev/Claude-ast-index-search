//! Maven reactor coordinates and direct project dependencies.

use regex::Regex;
use std::collections::HashMap;
use std::sync::LazyLock;

#[derive(Default)]
struct Element {
    name: String,
    text: String,
    children: Vec<Element>,
}

impl Element {
    fn child(&self, name: &str) -> Option<&Element> {
        self.children.iter().find(|child| child.name == name)
    }

    fn value(&self, name: &str) -> String {
        self.child(name)
            .map(|child| decode_xml(child.text.trim()))
            .unwrap_or_default()
    }
}

fn decode_xml(value: &str) -> String {
    value
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn document(content: &str) -> Option<Element> {
    static TOKENS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?s)<!--.*?-->|<!\[CDATA\[.*?\]\]>|<[^>]*>|[^<]+").expect("XML tokens")
    });
    let mut stack = vec![Element::default()];
    for token in TOKENS.find_iter(content).map(|m| m.as_str()) {
        if token.starts_with("<!--") || token.starts_with("<?") || token.starts_with("<!DOCTYPE") {
            continue;
        }
        if let Some(text) = token
            .strip_prefix("<![CDATA[")
            .and_then(|s| s.strip_suffix("]]>"))
        {
            stack.last_mut()?.text.push_str(text);
        } else if let Some(tag) = token.strip_prefix("</") {
            let name = tag.trim_end_matches('>').trim().rsplit(':').next()?;
            let node = stack.pop()?;
            if node.name != name || stack.is_empty() {
                return None;
            }
            stack.last_mut()?.children.push(node);
        } else if let Some(tag) = token.strip_prefix('<') {
            let name = tag
                .trim_end_matches('>')
                .trim_end_matches('/')
                .split_whitespace()
                .next()?
                .rsplit(':')
                .next()?;
            let node = Element {
                name: name.to_string(),
                ..Element::default()
            };
            if token.ends_with("/>") {
                stack.last_mut()?.children.push(node);
            } else {
                stack.push(node);
            }
        } else {
            stack.last_mut()?.text.push_str(token);
        }
    }
    if stack.len() != 1 {
        return None;
    }
    stack
        .pop()?
        .children
        .into_iter()
        .find(|n| n.name == "project")
}

pub(super) struct Manifest {
    pub group: String,
    pub artifact: String,
    pub dependencies: Vec<(String, String, String)>,
}

pub(super) fn parse(content: &str) -> Option<Manifest> {
    let project = document(content)?;
    let mut group = project.value("groupId");
    if group.is_empty() {
        group = project
            .child("parent")
            .map(|p| p.value("groupId"))
            .unwrap_or_default();
    }
    let artifact = project.value("artifactId");
    if artifact.is_empty() {
        return None;
    }
    let mut properties: HashMap<String, String> = project
        .child("properties")
        .map(|p| {
            p.children
                .iter()
                .map(|v| (v.name.clone(), decode_xml(v.text.trim())))
                .collect()
        })
        .unwrap_or_default();
    properties.insert("project.groupId".into(), group.clone());
    properties.insert("pom.groupId".into(), group.clone());
    properties.insert("project.artifactId".into(), artifact.clone());
    properties.insert("pom.artifactId".into(), artifact.clone());
    let resolve = |value: String| {
        static PROPERTY: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"\$\{([^}]+)\}").expect("Maven property"));
        let mut value = value;
        for _ in 0..16 {
            let new = PROPERTY
                .replace_all(&value, |caps: &regex::Captures<'_>| {
                    properties
                        .get(&caps[1])
                        .cloned()
                        .unwrap_or_else(|| caps[0].to_string())
                })
                .into_owned();
            if new == value {
                break;
            }
            value = new;
        }
        value
    };
    let dependencies = project
        .child("dependencies")
        .map(|deps| {
            deps.children
                .iter()
                .filter(|d| d.name == "dependency")
                .map(|d| {
                    let scope = d.value("scope");
                    (
                        resolve(d.value("groupId")),
                        resolve(d.value("artifactId")),
                        resolve(if scope.is_empty() {
                            "compile".into()
                        } else {
                            scope
                        }),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    Some(Manifest {
        group: resolve(group),
        artifact: resolve(artifact),
        dependencies,
    })
}
