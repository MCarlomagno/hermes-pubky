//! Authenticated session: restore from a stored grant, read/write the private
//! profile, and revoke.
//!
//! The async functions in [`ops`] hold the actual logic and are what the e2e
//! suite drives against a live testnet; the `#[pyclass]` wrappers below only
//! add argument marshalling and GIL handling.
//!
//! Everything routes through the profile-directory guard in [`crate::urls`],
//! so the plugin can only ever touch the documents its grant was scoped to.

use pyo3::prelude::*;

use pubky::{PubkySession, StatusCode};

use crate::errors::{map_sdk_error, NativeError, NativeResult};
use crate::http::{effective_cap, read_capped};
use crate::runtime::{block_on, sdk};
use crate::urls::{assert_within_profile_dir, private_profile_path};

/// Transport-free operations, shared by the Python bindings and the e2e tests.
pub mod ops {
    use super::{
        assert_within_profile_dir, effective_cap, map_sdk_error, private_profile_path, read_capped,
        sdk, NativeError, NativeResult, PubkySession, StatusCode,
    };

    /// Resolve a profile id to a storage path, refusing anything out of scope.
    pub fn profile_storage_path(profile_id: &str) -> NativeResult<String> {
        let path = private_profile_path(profile_id)?;
        assert_within_profile_dir(&path)?;
        Ok(path)
    }

    pub async fn restore(secret: &str) -> NativeResult<PubkySession> {
        sdk()?
            .restore_session(secret)
            .await
            .map_err(|e| map_sdk_error(&e))
    }

    /// Fetch the private profile, mapping "absent" to `None` rather than an error.
    pub async fn get_profile(
        session: &PubkySession,
        profile_id: &str,
        max_bytes: usize,
    ) -> NativeResult<Option<Vec<u8>>> {
        let path = profile_storage_path(profile_id)?;
        let cap = effective_cap(max_bytes);
        match session.storage().get(path.as_str()).await {
            Ok(response) => {
                if response.status() == StatusCode::NOT_FOUND {
                    return Ok(None);
                }
                read_capped(response, cap).await.map(Some)
            }
            Err(e) => match map_sdk_error(&e) {
                NativeError::NotFound(_) => Ok(None),
                mapped => Err(mapped),
            },
        }
    }

    pub async fn put_profile(
        session: &PubkySession,
        profile_id: &str,
        body: Vec<u8>,
    ) -> NativeResult<()> {
        let path = profile_storage_path(profile_id)?;
        let cap = effective_cap(0);
        if body.len() > cap {
            return Err(NativeError::too_large(format!(
                "profile document is {} bytes, over the {cap} byte cap",
                body.len()
            )));
        }
        session
            .storage()
            .put(path.as_str(), body)
            .await
            .map_err(|e| map_sdk_error(&e))?;
        Ok(())
    }

    /// Delete the profile. Absence is not an error — the caller's intent is met.
    pub async fn delete_profile(session: &PubkySession, profile_id: &str) -> NativeResult<()> {
        let path = profile_storage_path(profile_id)?;
        match session.storage().delete(path.as_str()).await {
            Ok(_) => Ok(()),
            Err(e) => match map_sdk_error(&e) {
                NativeError::NotFound(_) => Ok(()),
                mapped => Err(mapped),
            },
        }
    }

    pub async fn is_valid(session: &PubkySession) -> NativeResult<bool> {
        match session.revalidate().await {
            Ok(info) => Ok(info.is_some()),
            Err(e) => match map_sdk_error(&e) {
                NativeError::Auth(_) => Ok(false),
                mapped => Err(mapped),
            },
        }
    }

    /// Revoke this grant at the homeserver.
    ///
    /// Uses the session's own `DELETE /auth/grant/session`, which a scoped
    /// grant may call — unlike `GrantManager::revoke`, which the homeserver
    /// restricts to root-capability sessions.
    pub async fn revoke(session: PubkySession) -> NativeResult<()> {
        match session.signout().await {
            Ok(()) => Ok(()),
            Err((e, _session)) => match map_sdk_error(&e) {
                // Already gone server-side: the caller's intent is met.
                NativeError::Auth(_) | NativeError::NotFound(_) => Ok(()),
                mapped => Err(mapped),
            },
        }
    }

    /// Read a public document with no credentials attached.
    pub async fn public_get(url: &str, max_bytes: usize) -> NativeResult<Vec<u8>> {
        let parsed = crate::urls::parse_public_context_url(url)?;
        let cap = effective_cap(max_bytes);
        let storage = sdk()?.public_storage();
        let response = storage
            .get(parsed.normalized.as_str())
            .await
            .map_err(|e| map_sdk_error(&e))?;
        if response.status() == StatusCode::NOT_FOUND {
            return Err(NativeError::NotFound(format!(
                "no document at {}",
                parsed.normalized
            )));
        }
        read_capped(response, cap).await
    }
}

/// A restored, grant-backed Pubky session.
#[pyclass(module = "hermes_pubky._native")]
pub struct Session {
    inner: PubkySession,
}

#[pymethods]
impl Session {
    /// Restore a session from a previously exported grant secret.
    ///
    /// The secret is bearer-equivalent material; it is never logged here and
    /// never leaves the caller's machine.
    #[staticmethod]
    #[pyo3(signature = (secret, timeout_secs = 5.0))]
    fn restore(py: Python<'_>, secret: &str, timeout_secs: f64) -> PyResult<Self> {
        let secret = secret.to_string();
        let inner = py.detach(move || block_on(timeout_secs, ops::restore(&secret)))?;
        Ok(Self { inner })
    }

    /// z-base32 public key of the signed-in identity.
    #[getter]
    fn public_key(&self) -> String {
        self.inner.info().public_key().z32()
    }

    /// Capability strings this session actually carries.
    #[getter]
    fn capabilities(&self) -> Vec<String> {
        self.inner
            .info()
            .capabilities()
            .iter()
            .map(std::string::ToString::to_string)
            .collect()
    }

    /// Fetch the private profile document, or `None` when it does not exist.
    #[pyo3(signature = (profile_id, timeout_secs = 5.0, max_bytes = 0))]
    fn get_profile(
        &self,
        py: Python<'_>,
        profile_id: &str,
        timeout_secs: f64,
        max_bytes: usize,
    ) -> PyResult<Option<Vec<u8>>> {
        let session = self.inner.clone();
        let profile_id = profile_id.to_string();
        Ok(py.detach(move || {
            block_on(
                timeout_secs,
                ops::get_profile(&session, &profile_id, max_bytes),
            )
        })?)
    }

    /// Write the private profile document.
    #[pyo3(signature = (profile_id, body, timeout_secs = 10.0))]
    fn put_profile(
        &self,
        py: Python<'_>,
        profile_id: &str,
        body: Vec<u8>,
        timeout_secs: f64,
    ) -> PyResult<()> {
        let session = self.inner.clone();
        let profile_id = profile_id.to_string();
        py.detach(move || block_on(timeout_secs, ops::put_profile(&session, &profile_id, body)))?;
        Ok(())
    }

    /// Delete the private profile document. Absence is not an error.
    #[pyo3(signature = (profile_id, timeout_secs = 10.0))]
    fn delete_profile(&self, py: Python<'_>, profile_id: &str, timeout_secs: f64) -> PyResult<()> {
        let session = self.inner.clone();
        let profile_id = profile_id.to_string();
        py.detach(move || block_on(timeout_secs, ops::delete_profile(&session, &profile_id)))?;
        Ok(())
    }

    /// True when the session is still accepted by the homeserver.
    #[pyo3(signature = (timeout_secs = 5.0))]
    fn is_valid(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<bool> {
        let session = self.inner.clone();
        Ok(py.detach(move || block_on(timeout_secs, ops::is_valid(&session)))?)
    }

    /// Revoke this grant at the homeserver.
    #[pyo3(signature = (timeout_secs = 10.0))]
    fn revoke(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<()> {
        let session = self.inner.clone();
        py.detach(move || block_on(timeout_secs, ops::revoke(session)))?;
        Ok(())
    }

    fn __repr__(&self) -> String {
        format!("<Session public_key={}>", self.public_key())
    }
}

/// Read a public document by `pubky://` address, with no credentials attached.
#[pyfunction]
#[pyo3(signature = (url, timeout_secs = 5.0, max_bytes = 0))]
pub fn public_get(
    py: Python<'_>,
    url: &str,
    timeout_secs: f64,
    max_bytes: usize,
) -> PyResult<Vec<u8>> {
    let url = url.to_string();
    Ok(py.detach(move || block_on(timeout_secs, ops::public_get(&url, max_bytes)))?)
}
