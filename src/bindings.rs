//! The Python-facing surface for the /v2 protocol.
//!
//! Python names a root and a document; it never supplies a URL or a path. Each
//! handle binds one root to one actor at construction, so a private agent
//! handle cannot be used to publish publicly and a public template handle
//! carries no grant.

use std::path::PathBuf;

use pyo3::prelude::*;

use crate::errors::NativeResult;
use crate::objects::{get_object_to_file, put_object_from_file};
use crate::roots::{Document, Root};
use crate::runtime::block_on;
use crate::session::ops as session_ops;
use crate::storage::{check_capability, get_metadata, list_snapshots, put_metadata, Actor};

/// Authenticated access to one private agent root.
#[pyclass(module = "hermes_pubky._native")]
pub struct AgentTransport {
    root: Root,
    actor: Actor,
}

impl AgentTransport {
    fn build(root: Root, secret: &str, timeout_secs: f64) -> NativeResult<Self> {
        let session = block_on(timeout_secs, session_ops::restore(secret))?;
        crate::storage::check_owner(&root, &session)?;
        check_capability(&root, &session)?;
        Ok(Self {
            root,
            actor: Actor::Session(session),
        })
    }
}

#[pymethods]
impl AgentTransport {
    /// Restore a session and bind it to `pubky://<owner>/…/agents/<id>/`.
    ///
    /// Fails before any transfer if the grant belongs to another identity or
    /// lacks this agent's capability.
    #[staticmethod]
    #[pyo3(signature = (secret, owner, agent_id, timeout_secs = 10.0))]
    fn open(
        py: Python<'_>,
        secret: &str,
        owner: &str,
        agent_id: &str,
        timeout_secs: f64,
    ) -> PyResult<Self> {
        let root = Root::agent(owner, agent_id)?;
        let secret = secret.to_string();
        Ok(py.detach(move || AgentTransport::build(root, &secret, timeout_secs))?)
    }

    /// Restore a session for an agent named by its canonical head URI.
    #[staticmethod]
    #[pyo3(signature = (secret, uri, timeout_secs = 10.0))]
    fn from_uri(py: Python<'_>, secret: &str, uri: &str, timeout_secs: f64) -> PyResult<Self> {
        let root = Root::from_uri(uri, false)?;
        let secret = secret.to_string();
        Ok(py.detach(move || AgentTransport::build(root, &secret, timeout_secs))?)
    }

    #[getter]
    fn owner(&self) -> &str {
        self.root.owner()
    }

    #[getter]
    fn agent_id(&self) -> &str {
        self.root.id()
    }

    #[getter]
    fn uri(&self) -> PyResult<String> {
        Ok(self.root.uri()?)
    }

    #[getter]
    fn capability(&self) -> String {
        self.root.capability()
    }

    #[getter]
    fn capabilities(&self) -> Vec<String> {
        match &self.actor {
            Actor::Session(session) => session
                .info()
                .capabilities()
                .iter()
                .map(std::string::ToString::to_string)
                .collect(),
            Actor::Public => Vec::new(),
        }
    }

    /// Read `head.json`, or `None` when the agent does not exist remotely.
    #[pyo3(signature = (timeout_secs = 10.0))]
    fn head_get(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Option<Vec<u8>>> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_metadata(&self.root, &Document::Head, &self.actor),
            )
        })?)
    }

    #[pyo3(signature = (body, timeout_secs = 15.0))]
    fn head_put(&self, py: Python<'_>, body: Vec<u8>, timeout_secs: f64) -> PyResult<()> {
        py.detach(|| {
            block_on(
                timeout_secs,
                put_metadata(&self.root, &Document::Head, body, &self.actor),
            )
        })?;
        Ok(())
    }

    #[pyo3(signature = (snapshot_id, timeout_secs = 15.0))]
    fn snapshot_get(
        &self,
        py: Python<'_>,
        snapshot_id: &str,
        timeout_secs: f64,
    ) -> PyResult<Option<Vec<u8>>> {
        let document = Document::Snapshot(snapshot_id.to_string());
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_metadata(&self.root, &document, &self.actor),
            )
        })?)
    }

    #[pyo3(signature = (snapshot_id, body, timeout_secs = 20.0))]
    fn snapshot_put(
        &self,
        py: Python<'_>,
        snapshot_id: &str,
        body: Vec<u8>,
        timeout_secs: f64,
    ) -> PyResult<()> {
        let document = Document::Snapshot(snapshot_id.to_string());
        py.detach(|| {
            block_on(
                timeout_secs,
                put_metadata(&self.root, &document, body, &self.actor),
            )
        })?;
        Ok(())
    }

    /// Download one object into `destination`, returning its verified size.
    #[pyo3(signature = (reference, destination, timeout_secs = 30.0))]
    fn object_get(
        &self,
        py: Python<'_>,
        reference: &str,
        destination: PathBuf,
        timeout_secs: f64,
    ) -> PyResult<u64> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_object_to_file(&self.root, reference, &destination, &self.actor),
            )
        })?)
    }

    /// Upload one object read from `source`, returning its size.
    #[pyo3(signature = (reference, source, timeout_secs = 30.0))]
    fn object_put(
        &self,
        py: Python<'_>,
        reference: &str,
        source: PathBuf,
        timeout_secs: f64,
    ) -> PyResult<u64> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                put_object_from_file(&self.root, reference, &source, &self.actor),
            )
        })?)
    }

    /// One explicit page of snapshot ids: `(ids, next_cursor)`.
    #[pyo3(signature = (cursor = None, limit = 100, timeout_secs = 20.0))]
    fn list_snapshots(
        &self,
        py: Python<'_>,
        cursor: Option<String>,
        limit: u16,
        timeout_secs: f64,
    ) -> PyResult<(Vec<String>, Option<String>)> {
        let page = py.detach(|| {
            block_on(
                timeout_secs,
                list_snapshots(&self.root, cursor.as_deref(), limit, &self.actor),
            )
        })?;
        Ok((page.snapshot_ids, page.next_cursor))
    }

    fn __repr__(&self) -> String {
        format!("<AgentTransport {}>", self.root)
    }
}

/// Unauthenticated reads of a public template. Sends no grant, ever.
#[pyclass(module = "hermes_pubky._native")]
pub struct PublicTemplate {
    root: Root,
}

#[pymethods]
impl PublicTemplate {
    #[staticmethod]
    fn open(owner: &str, template_id: &str) -> PyResult<Self> {
        Ok(Self {
            root: Root::template(owner, template_id)?,
        })
    }

    #[staticmethod]
    fn from_uri(uri: &str) -> PyResult<Self> {
        Ok(Self {
            root: Root::from_uri(uri, true)?,
        })
    }

    #[getter]
    fn owner(&self) -> &str {
        self.root.owner()
    }

    #[getter]
    fn template_id(&self) -> &str {
        self.root.id()
    }

    #[getter]
    fn uri(&self) -> PyResult<String> {
        Ok(self.root.uri()?)
    }

    #[pyo3(signature = (timeout_secs = 10.0))]
    fn head_get(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Option<Vec<u8>>> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_metadata(&self.root, &Document::Head, &Actor::Public),
            )
        })?)
    }

    #[pyo3(signature = (snapshot_id, timeout_secs = 15.0))]
    fn snapshot_get(
        &self,
        py: Python<'_>,
        snapshot_id: &str,
        timeout_secs: f64,
    ) -> PyResult<Option<Vec<u8>>> {
        let document = Document::Snapshot(snapshot_id.to_string());
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_metadata(&self.root, &document, &Actor::Public),
            )
        })?)
    }

    #[pyo3(signature = (reference, destination, timeout_secs = 30.0))]
    fn object_get(
        &self,
        py: Python<'_>,
        reference: &str,
        destination: PathBuf,
        timeout_secs: f64,
    ) -> PyResult<u64> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                get_object_to_file(&self.root, reference, &destination, &Actor::Public),
            )
        })?)
    }

    fn __repr__(&self) -> String {
        format!("<PublicTemplate {}>", self.root)
    }
}

/// Authenticated writes to one public template root.
///
/// Separate from [`AgentTransport`] so an agent's grant can never publish, and
/// publishing needs its own explicitly requested capability.
#[pyclass(module = "hermes_pubky._native")]
pub struct TemplatePublisher {
    root: Root,
    actor: Actor,
}

#[pymethods]
impl TemplatePublisher {
    #[staticmethod]
    #[pyo3(signature = (secret, owner, template_id, timeout_secs = 10.0))]
    fn open(
        py: Python<'_>,
        secret: &str,
        owner: &str,
        template_id: &str,
        timeout_secs: f64,
    ) -> PyResult<Self> {
        let root = Root::template(owner, template_id)?;
        let secret = secret.to_string();
        Ok(py.detach(move || -> NativeResult<Self> {
            let session = block_on(timeout_secs, session_ops::restore(&secret))?;
            crate::storage::check_owner(&root, &session)?;
            check_capability(&root, &session)?;
            Ok(Self {
                root,
                actor: Actor::Session(session),
            })
        })?)
    }

    #[getter]
    fn uri(&self) -> PyResult<String> {
        Ok(self.root.uri()?)
    }

    #[getter]
    fn capability(&self) -> String {
        self.root.capability()
    }

    #[pyo3(signature = (body, timeout_secs = 15.0))]
    fn head_put(&self, py: Python<'_>, body: Vec<u8>, timeout_secs: f64) -> PyResult<()> {
        py.detach(|| {
            block_on(
                timeout_secs,
                put_metadata(&self.root, &Document::Head, body, &self.actor),
            )
        })?;
        Ok(())
    }

    #[pyo3(signature = (snapshot_id, body, timeout_secs = 20.0))]
    fn snapshot_put(
        &self,
        py: Python<'_>,
        snapshot_id: &str,
        body: Vec<u8>,
        timeout_secs: f64,
    ) -> PyResult<()> {
        let document = Document::Snapshot(snapshot_id.to_string());
        py.detach(|| {
            block_on(
                timeout_secs,
                put_metadata(&self.root, &document, body, &self.actor),
            )
        })?;
        Ok(())
    }

    #[pyo3(signature = (reference, source, timeout_secs = 30.0))]
    fn object_put(
        &self,
        py: Python<'_>,
        reference: &str,
        source: PathBuf,
        timeout_secs: f64,
    ) -> PyResult<u64> {
        Ok(py.detach(|| {
            block_on(
                timeout_secs,
                put_object_from_file(&self.root, reference, &source, &self.actor),
            )
        })?)
    }

    fn __repr__(&self) -> String {
        format!("<TemplatePublisher {}>", self.root)
    }
}

// -- address helpers ---------------------------------------------------------

/// The canonical head URI for a private agent.
#[pyfunction]
pub fn agent_uri(owner: &str, agent_id: &str) -> PyResult<String> {
    Ok(Root::agent(owner, agent_id)?.uri()?)
}

/// The single capability a private agent needs.
#[pyfunction]
pub fn agent_capability(owner: &str, agent_id: &str) -> PyResult<String> {
    Ok(Root::agent(owner, agent_id)?.capability())
}

/// Split a canonical agent URI into `(owner, agent_id)`.
#[pyfunction]
pub fn parse_agent_uri(uri: &str) -> PyResult<(String, String)> {
    let root = Root::from_uri(uri, false)?;
    Ok((root.owner().to_string(), root.id().to_string()))
}

/// The canonical head URI for a public template.
#[pyfunction]
pub fn template_uri(owner: &str, template_id: &str) -> PyResult<String> {
    Ok(Root::template(owner, template_id)?.uri()?)
}

/// The capability needed to publish a template.
#[pyfunction]
pub fn template_capability(owner: &str, template_id: &str) -> PyResult<String> {
    Ok(Root::template(owner, template_id)?.capability())
}

/// Split a canonical template URI into `(owner, template_id)`.
#[pyfunction]
pub fn parse_template_uri(uri: &str) -> PyResult<(String, String)> {
    let root = Root::from_uri(uri, true)?;
    Ok((root.owner().to_string(), root.id().to_string()))
}
