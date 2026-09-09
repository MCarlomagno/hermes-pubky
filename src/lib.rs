//! Native Pubky bindings for the managed Hermes agent launcher.
//!
//! This crate is deliberately thin. It exposes the Pubky SDK's auth flow,
//! session restore and scoped storage to Python, and enforces the properties
//! that are easiest to get wrong in a dynamic language: the path policy
//! (`roots`), the download caps (`http`, `objects`), and the actor/root pairing
//! (`storage`). Everything about *what* to store lives in the Python package.

pub mod auth;
pub mod bindings;
pub mod errors;
pub mod http;
pub mod objects;
pub mod roots;
pub mod runtime;
pub mod session;
pub mod storage;

use pyo3::prelude::*;

/// Hash raw bytes exactly as they were received.
#[pyfunction]
fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(data))
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("PROTOCOL_VERSION", roots::PROTOCOL_VERSION)?;
    m.add("APP_NAMESPACE", roots::APP_NAMESPACE)?;
    m.add("MAX_HEAD_BYTES", roots::MAX_HEAD_BYTES)?;
    m.add("MAX_SNAPSHOT_BYTES", roots::MAX_SNAPSHOT_BYTES)?;
    m.add("MAX_OBJECT_BYTES", roots::MAX_OBJECT_BYTES)?;
    m.add("MAX_LIST_LIMIT", roots::MAX_LIST_LIMIT)?;

    errors::register(m)?;

    m.add_class::<auth::AuthFlow>()?;
    m.add_class::<bindings::AgentTransport>()?;
    m.add_class::<bindings::PublicTemplate>()?;
    m.add_class::<bindings::TemplatePublisher>()?;

    m.add_function(wrap_pyfunction!(sha256_hex, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::agent_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::agent_capability, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::agent_scope, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::session_owner, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::template_scope, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::parse_agent_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::template_uri, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::template_capability, m)?)?;
    m.add_function(wrap_pyfunction!(bindings::parse_template_uri, m)?)?;
    Ok(())
}
