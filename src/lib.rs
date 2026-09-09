//! Native Pubky bindings for the Hermes `pubky` memory provider.
//!
//! This crate is deliberately thin: it exposes the Pubky SDK's auth flow,
//! session restore, and storage verbs to Python, and enforces the two safety
//! properties that are easiest to get wrong in a dynamic language — the path
//! policy (`urls`) and the download size cap (`http`). All policy about *what*
//! to store lives in the Python package.

// Modules are public so the e2e suite can drive the real code paths against
// a live testnet; only the `#[pymodule]` surface below is exposed to Python.
pub mod auth;
pub mod bindings;
pub mod errors;
pub mod http;
pub mod objects;
pub mod roots;
pub mod runtime;
pub mod session;
pub mod storage;
pub mod urls;

use pyo3::prelude::*;

/// Hash raw document bytes exactly as they were received.
///
/// Base contexts are pinned by the hash of their raw bytes, not of a parsed
/// and re-serialized form, so a change in formatting still counts as a change.
#[pyfunction]
fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

/// Parse and normalize a `pubky://` base-context address.
///
/// Returns `(author, path, normalized_url)`. Raises `PubkyValidationError`
/// for anything outside `/pub/**.json`.
#[pyfunction]
fn parse_context_url(url: &str) -> PyResult<(String, String, String)> {
    let parsed = urls::parse_public_context_url(url)?;
    Ok((parsed.author, parsed.path, parsed.normalized))
}

/// Validate a profile id, raising `PubkyValidationError` when unusable.
#[pyfunction]
fn validate_profile_id(profile_id: &str) -> PyResult<()> {
    urls::validate_profile_id(profile_id)?;
    Ok(())
}

/// The storage path a profile id maps to.
#[pyfunction]
fn profile_path(profile_id: &str) -> PyResult<String> {
    Ok(urls::private_profile_path(profile_id)?)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("MAX_DOCUMENT_BYTES", http::MAX_DOCUMENT_BYTES)?;
    m.add("APP_NAMESPACE", urls::APP_NAMESPACE)?;
    m.add("PRIVATE_PROFILE_DIR", urls::PRIVATE_PROFILE_DIR)?;
    m.add("REQUIRED_CAPABILITY", urls::REQUIRED_CAPABILITY)?;

    errors::register(m)?;

    m.add_class::<auth::AuthFlow>()?;
    m.add_class::<session::Session>()?;
    m.add_class::<bindings::AgentTransport>()?;
    m.add_class::<bindings::PublicTemplate>()?;
    m.add_class::<bindings::TemplatePublisher>()?;

    m.add_function(wrap_pyfunction!(session::public_get, m)?)?;
    m.add_function(wrap_pyfunction!(auth::required_capability, m)?)?;
    m.add_function(wrap_pyfunction!(auth::client_id, m)?)?;
    m.add_function(wrap_pyfunction!(sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(parse_context_url, m)?)?;
    m.add_function(wrap_pyfunction!(validate_profile_id, m)?)?;
    m.add_function(wrap_pyfunction!(profile_path, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::agent_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::agent_capability, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::parse_agent_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::template_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::template_capability, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::parse_template_uri, m)?)?;
    m.add("PROTOCOL_VERSION", roots::PROTOCOL_VERSION)?;
    m.add("MAX_OBJECT_BYTES", roots::MAX_OBJECT_BYTES)?;
    Ok(())
}
