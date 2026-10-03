//! Small lexical XML reader for Android resource/class locations.
//! Built-in character references are decoded once; no DTD expansion or external loading.
use regex::Regex;
use std::borrow::Cow;
use std::sync::LazyLock;

pub struct Attribute<'a> {
    pub name: &'a str,
    pub value: Cow<'a, str>,
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

/// Decode only XML's predefined entities and valid numeric character references.
/// Unknown/invalid references remain literal, and decoded text is never re-parsed.
pub fn character_references(value: &str) -> Cow<'_, str> {
    static REFERENCES: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"&(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);").unwrap());
    if !value.contains('&') {
        return Cow::Borrowed(value);
    }
    let mut output = String::new();
    let mut consumed = 0;
    for reference in REFERENCES.find_iter(value) {
        let entity = &reference.as_str()[1..reference.len() - 1];
        let decoded = match entity {
            "amp" => Some('&'),
            "lt" => Some('<'),
            "gt" => Some('>'),
            "quot" => Some('"'),
            "apos" => Some('\''),
            numeric => {
                let number = if let Some(hex) = numeric.strip_prefix("#x") {
                    u32::from_str_radix(hex, 16).ok()
                } else {
                    numeric
                        .strip_prefix('#')
                        .and_then(|decimal| decimal.parse::<u32>().ok())
                };
                number.filter(|n| matches!(n, 9 | 10 | 13 | 0x20..=0xD7FF | 0xE000..=0xFFFD | 0x10000..=0x10FFFF))
                    .and_then(char::from_u32)
            }
        };
        if let Some(character) = decoded {
            if consumed == 0 {
                output.reserve(value.len());
            }
            output.push_str(&value[consumed..reference.start()]);
            output.push(character);
            consumed = reference.end();
        }
    }
    if consumed == 0 {
        Cow::Borrowed(value)
    } else {
        output.push_str(&value[consumed..]);
        Cow::Owned(output)
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
                        value: character_references(value.as_str()),
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

#[cfg(test)]
mod character_reference_tests {
    use super::*;

    #[test]
    fn valid_numeric_and_predefined_references_decode_once() {
        assert_eq!(
            character_references("&#36;&#x24; &#xE9;&amp;&lt;&gt;&quot;&apos;"),
            "$$ é&<>\"'"
        );
        assert_eq!(character_references("&amp;#36; &amp;amp;"), "&#36; &amp;");
    }

    #[test]
    fn unknown_and_invalid_references_never_expand() {
        let value = "&external; &#0; &#xD800; &#x110000; &#99999999999999999;";
        assert!(matches!(character_references(value), Cow::Borrowed(_)));
        assert_eq!(character_references(value), value);
        assert!(matches!(
            character_references("fixture.Class"),
            Cow::Borrowed(_)
        ));
        assert!(matches!(
            character_references(&"&".repeat(10000)),
            Cow::Borrowed(_)
        ));
    }

    #[test]
    fn decoded_attribute_markup_is_not_a_new_tag() {
        let xml = "<view note='&lt;fixture.Ghost/&gt;' class='fixture.Outer&#36;Inner'/>";
        let tags = tags(xml);
        assert_eq!(tags.len(), 1);
        assert_eq!(tags[0].name, "view");
        assert_eq!(
            tags[0].attribute("class").unwrap().value,
            "fixture.Outer$Inner"
        );
        assert_eq!(tags[0].attribute("note").unwrap().value, "<fixture.Ghost/>");
    }
}
