//! Small lexical XML reader for Android resource/class locations.
//! No entity expansion or external document loading is performed.
use regex::Regex;
use std::sync::LazyLock;

pub struct Attribute<'a> {
    pub name: &'a str,
    pub value: &'a str,
    pub line: usize,
}

pub struct Tag<'a> {
    pub name: &'a str,
    pub line: usize,
    pub attributes: Vec<Attribute<'a>>,
}

impl<'a> Tag<'a> {
    pub fn attribute(&self, name: &str) -> Option<&Attribute<'a>> {
        self.attributes.iter().find(|a| a.name == name)
    }
}

fn tag_end(bytes: &[u8], start: usize) -> usize {
    let mut quote = None;
    let mut brackets = 0usize;
    for (offset, &byte) in bytes.iter().enumerate().skip(start) {
        if let Some(q) = quote {
            if byte == q {
                quote = None;
            }
        } else {
            match byte {
                b'\'' | b'"' => quote = Some(byte),
                b'[' => brackets += 1,
                b']' => brackets = brackets.saturating_sub(1),
                b'>' if brackets == 0 => return offset + 1,
                _ => {}
            }
        }
    }
    bytes.len()
}

/// Mask comments, CDATA and declarations, preserving byte offsets and lines.
pub fn visible(content: &str) -> String {
    let mut bytes = content.as_bytes().to_vec();
    let mut cursor = 0;
    while let Some(offset) = content[cursor..].find('<') {
        let start = cursor + offset;
        let tail = &content[start..];
        let delimiter = if tail.starts_with("<!--") {
            Some("-->")
        } else if tail.starts_with("<![CDATA[") {
            Some("]]>")
        } else if tail.starts_with("<?") {
            Some("?>")
        } else {
            None
        };
        let end = if let Some(delimiter) = delimiter {
            tail.find(delimiter)
                .map(|n| start + n + delimiter.len())
                .unwrap_or(content.len())
        } else {
            tag_end(content.as_bytes(), start + 1)
        };
        if delimiter.is_some() || tail.starts_with("<!") {
            for byte in &mut bytes[start..end] {
                if *byte != b'\n' && *byte != b'\r' {
                    *byte = b' ';
                }
            }
        }
        cursor = end;
    }
    String::from_utf8(bytes).expect("masked XML retains UTF-8")
}

/// Read complete opening tags, with exact attribute names and local IDs.
pub fn tags(content: &str) -> Vec<Tag<'_>> {
    static ATTR: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"([^\s=<>/'"]+)\s*=\s*(?:"([^"]*)"|'([^']*)')"#).unwrap());
    let mut tags = Vec::new();
    let mut cursor = 0;
    let mut line = 1;
    while let Some(offset) = content[cursor..].find('<') {
        let start = cursor + offset;
        line += content[cursor..start]
            .bytes()
            .filter(|&b| b == b'\n')
            .count();
        let end = tag_end(content.as_bytes(), start + 1);
        let body = &content[start + 1..end];
        let name_len = body
            .find(|c: char| c.is_whitespace() || matches!(c, '/' | '>'))
            .unwrap_or(body.len());
        let name = &body[..name_len];
        if !name.is_empty() && !name.starts_with(['!', '?']) && content[..end].ends_with('>') {
            let attributes = ATTR
                .captures_iter(&body[name_len..])
                .map(|c| {
                    let key = c.get(1).unwrap();
                    let value = c.get(2).or_else(|| c.get(3)).unwrap();
                    Attribute {
                        name: key.as_str(),
                        value: value.as_str(),
                        line: line
                            + body[..name_len + key.start()]
                                .bytes()
                                .filter(|&b| b == b'\n')
                                .count(),
                    }
                })
                .collect();
            tags.push(Tag {
                name,
                line,
                attributes,
            });
        }
        line += content[start..end].bytes().filter(|&b| b == b'\n').count();
        cursor = end;
    }
    tags
}

pub fn is_java_class(name: &str) -> bool {
    static CLASS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"^[\p{XID_Start}_$][\p{XID_Continue}$]*(?:\.[\p{XID_Start}_$][\p{XID_Continue}$]*)+$",
        )
        .unwrap()
    });
    CLASS.is_match(name)
}
