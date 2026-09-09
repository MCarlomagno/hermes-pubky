//! Pubky address parsing and the plugin's path policy.
//!
//! Parsing is delegated to the SDK's [`PubkyResource`], which normalizes and
//! rejects traversal (`.`, `..`, `//`) and percent-encoded trickery. On top of
//! that the plugin enforces its own policy: public context documents must live
//! under `/pub/`, private profiles under the plugin's own `/priv/` namespace,
//! and both must be plain `.json` files.

use std::str::FromStr;

use pubky::PubkyResource;

use crate::errors::{NativeError, NativeResult};

/// Namespace this plugin owns on the homeserver.
pub const APP_NAMESPACE: &str = "hermes.pubky.app";
/// Directory holding private profile documents.
pub const PRIVATE_PROFILE_DIR: &str = "/priv/hermes.pubky.app/v1/profiles/";
/// Capability requested during setup — read+write, private profiles only.
pub const REQUIRED_CAPABILITY: &str = "/priv/hermes.pubky.app/v1/profiles/:rw";

/// A parsed and policy-checked Pubky address.
#[derive(Debug, Clone)]
pub struct ParsedAddress {
    pub author: String,
    pub path: String,
    pub normalized: String,
}

/// Parse any `pubky://` address, applying SDK normalization only.
pub fn parse_address(input: &str) -> NativeResult<ParsedAddress> {
    if input.len() > 2048 {
        return Err(NativeError::validation("pubky URL is too long"));
    }
    let resource = PubkyResource::from_str(input)
        .map_err(|e| NativeError::validation(format!("invalid pubky URL: {e}")))?;
    Ok(ParsedAddress {
        author: resource.owner.z32(),
        path: resource.path.as_str().to_string(),
        normalized: resource.to_pubky_url(),
    })
}

/// Parse a **public base-context** address and enforce the public-read policy.
///
/// Only `/pub/**.json` is accepted. Anything under `/priv/` is refused
/// outright: a base context is meant to be shareable, and pinning a private
/// path would silently produce a document nobody else could load.
pub fn parse_public_context_url(input: &str) -> NativeResult<ParsedAddress> {
    let parsed = parse_address(input)?;
    let path = &parsed.path;

    if !path.starts_with("/pub/") {
        return Err(NativeError::validation(
            "public context must live under /pub/ (got a non-public path)",
        ));
    }
    if path.ends_with('/') {
        return Err(NativeError::validation(
            "public context must be a file, not a directory",
        ));
    }
    if !path.ends_with(".json") {
        return Err(NativeError::validation(
            "public context must be a .json document",
        ));
    }
    Ok(parsed)
}

/// Validate a profile id and render its private storage path.
///
/// Profile ids become a path segment, so they are restricted to a
/// conservative character set rather than relying on encoding to save us.
pub fn private_profile_path(profile_id: &str) -> NativeResult<String> {
    validate_profile_id(profile_id)?;
    Ok(format!("{PRIVATE_PROFILE_DIR}{profile_id}.json"))
}

pub fn validate_profile_id(profile_id: &str) -> NativeResult<()> {
    if profile_id.is_empty() {
        return Err(NativeError::validation("profile id cannot be empty"));
    }
    if profile_id.len() > 64 {
        return Err(NativeError::validation(
            "profile id must be at most 64 characters",
        ));
    }
    let ok = profile_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !ok {
        return Err(NativeError::validation(
            "profile id may only contain letters, digits, '-' and '_'",
        ));
    }
    if profile_id.starts_with('-') || profile_id.starts_with('_') {
        return Err(NativeError::validation(
            "profile id must start with a letter or digit",
        ));
    }
    Ok(())
}

/// Guard a storage path before it is handed to an authenticated session.
///
/// The grant is scoped to the private profile directory; refusing anything
/// else locally means a bug in the plugin cannot turn into a write the
/// homeserver would have to reject.
pub fn assert_within_profile_dir(path: &str) -> NativeResult<()> {
    if !path.starts_with(PRIVATE_PROFILE_DIR) {
        return Err(NativeError::validation(format!(
            "refusing to touch {path}: outside {PRIVATE_PROFILE_DIR}"
        )));
    }
    let tail = &path[PRIVATE_PROFILE_DIR.len()..];
    if tail.is_empty() || tail.contains('/') {
        return Err(NativeError::validation(
            "profile path must be a single file inside the profiles directory",
        ));
    }
    if !tail.ends_with(".json") {
        return Err(NativeError::validation("profile path must end in .json"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // A syntactically valid z32 public key, used as the address owner.
    const PK: &str = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo";

    #[test]
    fn parses_a_public_context_url() {
        let url = format!("pubky://{PK}/pub/hermes.pubky.app/v1/contexts/researcher.json");
        let parsed = parse_public_context_url(&url).expect("should parse");
        assert_eq!(parsed.author, PK);
        assert_eq!(
            parsed.path,
            "/pub/hermes.pubky.app/v1/contexts/researcher.json"
        );
        assert_eq!(parsed.normalized, url);
    }

    #[test]
    fn rejects_traversal_in_public_context_url() {
        let url = format!("pubky://{PK}/pub/../priv/secrets.json");
        assert!(matches!(
            parse_public_context_url(&url),
            Err(NativeError::Validation(_))
        ));
    }

    #[test]
    fn rejects_empty_segments() {
        let url = format!("pubky://{PK}/pub//ctx.json");
        assert!(parse_public_context_url(&url).is_err());
    }

    #[test]
    fn rejects_private_path_as_public_context() {
        let url = format!("pubky://{PK}/priv/hermes.pubky.app/v1/profiles/default.json");
        assert!(parse_public_context_url(&url).is_err());
    }

    #[test]
    fn rejects_non_json_public_context() {
        let url = format!("pubky://{PK}/pub/hermes.pubky.app/v1/contexts/researcher.md");
        assert!(parse_public_context_url(&url).is_err());
    }

    #[test]
    fn rejects_directory_public_context() {
        let url = format!("pubky://{PK}/pub/hermes.pubky.app/v1/contexts/");
        assert!(parse_public_context_url(&url).is_err());
    }

    #[test]
    fn rejects_bad_public_key() {
        assert!(parse_public_context_url("pubky://not-a-key/pub/x.json").is_err());
    }

    #[test]
    fn percent_encoded_traversal_is_defused_not_resolved() {
        // `%2e%2e` is re-encoded to a literal `%252e%252e` segment rather than
        // being resolved as `..`, so the address cannot climb out of /pub/.
        // Any `/priv/` that survives is an ordinary subdirectory name *inside*
        // the author's public tree, which is harmless.
        let url = format!("pubky://{PK}/pub/%2e%2e/%2e%2e/priv/x.json");
        // Rejecting outright is equally acceptable, hence `if let`.
        if let Ok(parsed) = parse_public_context_url(&url) {
            assert!(
                parsed.path.starts_with("/pub/"),
                "escaped the public prefix: {}",
                parsed.path
            );
            assert!(
                !parsed.path.contains("/../"),
                "left an unresolved traversal segment: {}",
                parsed.path
            );
        }
    }

    #[test]
    fn a_priv_path_can_never_be_reached_via_the_public_parser() {
        // The property that matters: whatever the input encoding, the parsed
        // path never *starts* with /priv/.
        for candidate in [
            "/priv/hermes.pubky.app/v1/profiles/default.json",
            "/%70riv/x.json",
            "/pub/../priv/x.json",
            "/./priv/x.json",
            "//priv/x.json",
        ] {
            let url = format!("pubky://{PK}{candidate}");
            if let Ok(parsed) = parse_public_context_url(&url) {
                assert!(
                    !parsed.path.starts_with("/priv/"),
                    "input {candidate:?} reached a private path: {}",
                    parsed.path
                );
            }
        }
    }

    #[test]
    fn builds_private_profile_paths() {
        assert_eq!(
            private_profile_path("default").unwrap(),
            "/priv/hermes.pubky.app/v1/profiles/default.json"
        );
    }

    #[test]
    fn rejects_hostile_profile_ids() {
        for bad in [
            "",
            "../escape",
            "a/b",
            "with space",
            "-leading",
            "_leading",
            "sü",
            &"x".repeat(65),
        ] {
            assert!(
                validate_profile_id(bad).is_err(),
                "profile id {bad:?} should be rejected"
            );
        }
    }

    #[test]
    fn accepts_reasonable_profile_ids() {
        for good in ["default", "work-laptop", "a", "p1_2", "0"] {
            assert!(validate_profile_id(good).is_ok(), "{good:?} should be ok");
        }
    }

    #[test]
    fn confines_writes_to_the_profile_directory() {
        assert!(
            assert_within_profile_dir("/priv/hermes.pubky.app/v1/profiles/default.json").is_ok()
        );
        for bad in [
            "/pub/hermes.pubky.app/v1/profiles/default.json",
            "/priv/other.app/v1/profiles/default.json",
            "/priv/hermes.pubky.app/v1/profiles/",
            "/priv/hermes.pubky.app/v1/profiles/nested/default.json",
            "/priv/hermes.pubky.app/v1/profiles/default.txt",
        ] {
            assert!(
                assert_within_profile_dir(bad).is_err(),
                "path {bad:?} should be refused"
            );
        }
    }
}
