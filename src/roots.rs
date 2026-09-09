//! Typed storage roots and the exact paths they permit.
//!
//! Python never hands the native layer a free-form URL. It names a root and a
//! document kind, and this module renders the only paths that root allows. A
//! bug on the Python side therefore cannot become a write outside the agent's
//! own directory.
//!
//! Reference: implementation plan sections 5, 5.1 and 14.

use std::fmt;
use std::str::FromStr;

use pubky::{Capabilities, PublicKey};

use crate::errors::{NativeError, NativeResult};

/// Namespace this integration owns on a homeserver.
pub const APP_NAMESPACE: &str = "hermes.pubky.app";
/// Protocol generation. There is no v1 reader.
pub const PROTOCOL_VERSION: &str = "v2";

const PRIVATE_PREFIX: &str = "/priv/hermes.pubky.app/v2/agents/";
const PUBLIC_PREFIX: &str = "/pub/hermes.pubky.app/v2/templates/";

/// Caps enforced natively, before a document reaches Python.
pub const MAX_HEAD_BYTES: usize = 4 * 1024;
pub const MAX_SNAPSHOT_BYTES: usize = 1024 * 1024;
pub const MAX_OBJECT_BYTES: usize = 1024 * 1024;
/// Upper bound on one page of a snapshot listing.
pub const MAX_LIST_LIMIT: u16 = 500;

/// Which document within a root is being addressed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Document {
    Head,
    Snapshot(String),
    Object(String),
}

impl Document {
    /// The byte cap that applies to this document kind.
    pub fn max_bytes(&self) -> usize {
        match self {
            Document::Head => MAX_HEAD_BYTES,
            Document::Snapshot(_) => MAX_SNAPSHOT_BYTES,
            Document::Object(_) => MAX_OBJECT_BYTES,
        }
    }

    fn relative(&self) -> String {
        match self {
            Document::Head => "head.json".to_string(),
            Document::Snapshot(id) => format!("snapshots/{id}.json"),
            Document::Object(reference) => reference.clone(),
        }
    }
}

/// A private agent root, or a public template root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Root {
    Agent { owner: String, id: String },
    Template { owner: String, id: String },
}

impl Root {
    pub fn agent(owner: &str, id: &str) -> NativeResult<Self> {
        Ok(Root::Agent {
            owner: validate_owner(owner)?,
            id: validate_id(id)?,
        })
    }

    pub fn template(owner: &str, id: &str) -> NativeResult<Self> {
        Ok(Root::Template {
            owner: validate_owner(owner)?,
            id: validate_id(id)?,
        })
    }

    pub fn is_template(&self) -> bool {
        matches!(self, Root::Template { .. })
    }

    pub fn owner(&self) -> &str {
        match self {
            Root::Agent { owner, .. } | Root::Template { owner, .. } => owner,
        }
    }

    pub fn id(&self) -> &str {
        match self {
            Root::Agent { id, .. } | Root::Template { id, .. } => id,
        }
    }

    /// Absolute storage path of this root, with a trailing slash.
    pub fn base_path(&self) -> String {
        match self {
            Root::Agent { id, .. } => format!("{PRIVATE_PREFIX}{id}/"),
            Root::Template { id, .. } => format!("{PUBLIC_PREFIX}{id}/"),
        }
    }

    /// Absolute storage path of one document inside this root.
    pub fn path_for(&self, document: &Document) -> NativeResult<String> {
        match document {
            Document::Head => {}
            Document::Snapshot(id) => {
                validate_hex32(id, "snapshot id")?;
            }
            Document::Object(reference) => {
                validate_object_ref(reference)?;
            }
        }
        Ok(format!("{}{}", self.base_path(), document.relative()))
    }

    /// The single capability a session needs to write this root.
    pub fn capability(&self) -> String {
        format!("{}:rw", self.base_path())
    }

    /// Parse the capability string, so the caller cannot invent a wider scope.
    pub fn capabilities(&self) -> NativeResult<Capabilities> {
        self.capability()
            .parse()
            .map_err(|e| NativeError::validation(format!("could not build capabilities: {e}")))
    }

    /// The canonical `pubky://` URI naming this root's head document.
    pub fn uri(&self) -> NativeResult<String> {
        Ok(format!(
            "pubky://{}{}",
            self.owner(),
            self.path_for(&Document::Head)?
        ))
    }

    /// Parse a canonical head URI back into a root.
    ///
    /// Rejects anything that is not exactly a `/v2/` head address, so a stale
    /// 0.1 address or a foreign path fails before any request is made.
    pub fn from_uri(uri: &str, expect_template: bool) -> NativeResult<Self> {
        if uri.len() > 2048 {
            return Err(NativeError::validation("agent URI is too long"));
        }
        let rest = uri
            .strip_prefix("pubky://")
            .ok_or_else(|| NativeError::validation("agent URI must start with pubky://"))?;
        let (owner, path) = rest
            .split_once('/')
            .ok_or_else(|| NativeError::validation("agent URI is missing its path"))?;
        let owner = validate_owner(owner)?;
        let path = format!("/{path}");

        let prefix = if expect_template {
            PUBLIC_PREFIX
        } else {
            PRIVATE_PREFIX
        };
        let tail = path.strip_prefix(prefix).ok_or_else(|| {
            NativeError::validation(format!(
                "unsupported address {path:?}; this release only reads {prefix}<id>/head.json"
            ))
        })?;
        let id = tail
            .strip_suffix("/head.json")
            .ok_or_else(|| NativeError::validation("agent URI must address head.json"))?;
        let id = validate_id(id)?;
        if expect_template {
            Root::template(&owner, &id)
        } else {
            Root::agent(&owner, &id)
        }
    }
}

impl fmt::Display for Root {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}{}", self.owner(), self.base_path())
    }
}

// -- validators --------------------------------------------------------------

/// Accept only a z-base32 public key the SDK itself can parse.
pub fn validate_owner(owner: &str) -> NativeResult<String> {
    let key = PublicKey::from_str(owner)
        .map_err(|_| NativeError::validation(format!("invalid owner key: {owner:?}")))?;
    Ok(key.z32())
}

/// Agent and template ids: `^[a-z0-9][a-z0-9_-]{0,63}$` (plan 5.1).
pub fn validate_id(id: &str) -> NativeResult<String> {
    let ok = !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_')
        && id
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    if !ok {
        return Err(NativeError::validation(format!(
            "id must match ^[a-z0-9][a-z0-9_-]{{0,63}}$ (got {id:?})"
        )));
    }
    Ok(id.to_string())
}

pub fn validate_hex32(value: &str, what: &str) -> NativeResult<()> {
    if value.len() != 32
        || !value
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase())
    {
        return Err(NativeError::validation(format!(
            "{what} must be 32 lowercase hex characters (got {value:?})"
        )));
    }
    Ok(())
}

/// Object references are generated: `objects/<64 hex>.<md|json|bin|chunk>`.
pub fn validate_object_ref(reference: &str) -> NativeResult<()> {
    let tail = reference.strip_prefix("objects/").ok_or_else(|| {
        NativeError::validation(format!(
            "object reference must start with objects/ (got {reference:?})"
        ))
    })?;
    let (digest, extension) = tail
        .rsplit_once('.')
        .ok_or_else(|| NativeError::validation("object reference must have an extension"))?;
    if digest.len() != 64
        || !digest
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase())
    {
        return Err(NativeError::validation(format!(
            "object digest must be 64 lowercase hex characters (got {digest:?})"
        )));
    }
    if !matches!(extension, "md" | "json" | "bin" | "chunk") {
        return Err(NativeError::validation(format!(
            "unsupported object extension {extension:?}"
        )));
    }
    Ok(())
}

/// The digest an object reference claims to hold.
pub fn digest_of(reference: &str) -> NativeResult<String> {
    validate_object_ref(reference)?;
    let tail = &reference["objects/".len()..];
    let (digest, _ext) = tail.rsplit_once('.').expect("validated above");
    Ok(digest.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const PK: &str = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo";
    const SNAP: &str = "0123456789abcdef0123456789abcdef";
    const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[test]
    fn agent_paths_are_confined_to_the_agent_directory() {
        let root = Root::agent(PK, "default").unwrap();
        assert_eq!(
            root.path_for(&Document::Head).unwrap(),
            "/priv/hermes.pubky.app/v2/agents/default/head.json"
        );
        assert_eq!(
            root.path_for(&Document::Snapshot(SNAP.into())).unwrap(),
            format!("/priv/hermes.pubky.app/v2/agents/default/snapshots/{SNAP}.json")
        );
        assert_eq!(
            root.path_for(&Document::Object(format!("objects/{DIGEST}.md")))
                .unwrap(),
            format!("/priv/hermes.pubky.app/v2/agents/default/objects/{DIGEST}.md")
        );
    }

    #[test]
    fn template_paths_are_public() {
        let root = Root::template(PK, "researcher").unwrap();
        assert!(root.base_path().starts_with("/pub/"));
        assert_eq!(
            root.capability(),
            "/pub/hermes.pubky.app/v2/templates/researcher/:rw"
        );
    }

    #[test]
    fn the_capability_is_scoped_to_one_agent() {
        let root = Root::agent(PK, "default").unwrap();
        assert_eq!(
            root.capability(),
            "/priv/hermes.pubky.app/v2/agents/default/:rw"
        );
        let caps = root.capabilities().unwrap();
        assert_eq!(caps.len(), 1);
        assert!(!caps.iter().any(|c| c.is_root()));
    }

    #[test]
    fn uri_round_trips() {
        let root = Root::agent(PK, "work-laptop").unwrap();
        let uri = root.uri().unwrap();
        assert_eq!(Root::from_uri(&uri, false).unwrap(), root);
    }

    #[test]
    fn a_v1_address_is_refused_with_an_explanation() {
        let uri = format!("pubky://{PK}/priv/hermes.pubky.app/v1/profiles/default.json");
        let err = Root::from_uri(&uri, false).unwrap_err();
        assert!(matches!(err, NativeError::Validation(_)));
    }

    #[test]
    fn a_public_address_is_not_accepted_as_a_private_agent() {
        let template = Root::template(PK, "researcher").unwrap();
        assert!(Root::from_uri(&template.uri().unwrap(), false).is_err());
    }

    #[test]
    fn a_private_address_is_not_accepted_as_a_template() {
        let agent = Root::agent(PK, "default").unwrap();
        assert!(Root::from_uri(&agent.uri().unwrap(), true).is_err());
    }

    #[test]
    fn rejects_hostile_ids() {
        for bad in [
            "",
            "Default",
            "-lead",
            "_lead",
            "a/b",
            "..",
            "with space",
            &"x".repeat(65),
        ] {
            assert!(validate_id(bad).is_err(), "{bad:?} should be refused");
        }
    }

    #[test]
    fn rejects_object_references_that_are_not_generated_digests() {
        for bad in [
            "objects/../escape.md",
            "objects/short.md",
            "notobjects/x.md",
            &format!("objects/{DIGEST}.exe"),
            &format!("objects/{}.md", DIGEST.to_uppercase()),
            "objects/nodot",
        ] {
            assert!(
                validate_object_ref(bad).is_err(),
                "{bad:?} should be refused"
            );
        }
    }

    #[test]
    fn traversal_cannot_reach_outside_a_root() {
        let root = Root::agent(PK, "default").unwrap();
        assert!(root
            .path_for(&Document::Object("objects/../../head.json".into()))
            .is_err());
        assert!(root
            .path_for(&Document::Snapshot("../evil".into()))
            .is_err());
    }

    #[test]
    fn document_caps_differ_by_kind() {
        assert_eq!(Document::Head.max_bytes(), MAX_HEAD_BYTES);
        assert_eq!(
            Document::Snapshot(SNAP.into()).max_bytes(),
            MAX_SNAPSHOT_BYTES
        );
        assert_eq!(
            Document::Object(format!("objects/{DIGEST}.bin")).max_bytes(),
            MAX_OBJECT_BYTES
        );
    }

    #[test]
    fn digest_is_read_back_from_the_reference() {
        assert_eq!(
            digest_of(&format!("objects/{DIGEST}.chunk")).unwrap(),
            DIGEST
        );
    }

    #[test]
    fn owner_must_be_a_real_public_key() {
        assert!(Root::agent("not-a-key", "default").is_err());
        assert!(validate_owner(PK).is_ok());
    }
}
