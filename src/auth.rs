//! Pubky Auth grant flow, driven from Python.
//!
//! Setup shows the user an authorization URL (opened in Pubky Ring), then
//! polls until the grant is approved. On approval the flow yields a portable
//! grant secret, which the caller stores locally — it is the only credential
//! the plugin ever holds.

use std::time::Duration;

use pyo3::prelude::*;

use pubky::{AuthFlowKind, Capabilities, ClientId, PubkyGrantAuthFlow};

use crate::errors::{map_sdk_error, NativeError, NativeResult};
use crate::runtime::{block_on, runtime, sdk};
use crate::urls::{APP_NAMESPACE, REQUIRED_CAPABILITY};

/// How long to wait between relay polls while the user approves in Ring.
const POLL_INTERVAL: Duration = Duration::from_millis(750);

/// Transport-free operations, shared by the Python bindings and the e2e tests.
pub mod ops {
    use super::{
        map_sdk_error, runtime, sdk, AuthFlowKind, Capabilities, ClientId, NativeError,
        NativeResult, PubkyGrantAuthFlow, POLL_INTERVAL,
    };

    /// Build the capability set and client id, rejecting malformed input.
    pub fn parse_request(
        capabilities: &str,
        client_id: &str,
    ) -> NativeResult<(Capabilities, ClientId)> {
        let caps: Capabilities = capabilities.parse().map_err(|e| {
            NativeError::validation(format!("invalid capabilities {capabilities:?}: {e}"))
        })?;
        if caps.is_empty() {
            return Err(NativeError::validation("capabilities cannot be empty"));
        }
        let client_id = ClientId::new(client_id)
            .map_err(|e| NativeError::validation(format!("invalid client id: {e}")))?;
        Ok((caps, client_id))
    }

    /// Start a grant flow. Must be called with the shared runtime entered,
    /// because the flow spawns its own relay listener task.
    pub fn start(caps: &Capabilities, client_id: ClientId) -> NativeResult<PubkyGrantAuthFlow> {
        let sdk = sdk()?;
        let _guard = runtime()?.enter();
        sdk.start_grant_auth_flow(caps, AuthFlowKind::signin(), client_id)
            .map_err(|e| map_sdk_error(&e))
    }

    /// Check once for an approval, returning the portable grant secret.
    pub async fn poll_once(flow: &PubkyGrantAuthFlow) -> NativeResult<Option<String>> {
        match flow.try_poll_credential_once().await {
            Ok(Some(credential)) => Ok(Some(export(credential).await?)),
            Ok(None) => Ok(None),
            Err(e) => Err(map_sdk_error(&e)),
        }
    }

    /// Poll until the signer approves. The caller supplies the deadline.
    pub async fn await_approval(flow: &PubkyGrantAuthFlow) -> NativeResult<String> {
        loop {
            if let Some(secret) = poll_once(flow).await? {
                return Ok(secret);
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    }

    async fn export(credential: pubky::GrantCredential) -> NativeResult<String> {
        credential.export_local_secret().await.ok_or_else(|| {
            NativeError::Auth("grant was approved but its key is not exportable".to_string())
        })
    }
}

/// A pending Pubky Auth grant request.
#[pyclass(module = "hermes_pubky._native")]
pub struct AuthFlow {
    inner: PubkyGrantAuthFlow,
    authorization_url: String,
    capabilities: String,
}

#[pymethods]
impl AuthFlow {
    /// Start a grant request for the given capability string.
    ///
    /// Defaults to exactly the capability this plugin needs: read+write on
    /// its own private profile directory, and nothing else.
    #[new]
    #[pyo3(signature = (capabilities = REQUIRED_CAPABILITY, client_id = APP_NAMESPACE))]
    fn new(py: Python<'_>, capabilities: &str, client_id: &str) -> PyResult<Self> {
        let (caps, client_id) = ops::parse_request(capabilities, client_id)?;
        let caps_string = caps.to_string();
        let inner = py.detach(move || ops::start(&caps, client_id))?;

        let authorization_url = inner.authorization_url().to_string();
        Ok(Self {
            inner,
            authorization_url,
            capabilities: caps_string,
        })
    }

    /// The `pubkyauth://` URL to show the user (QR code or deep link).
    #[getter]
    fn authorization_url(&self) -> &str {
        &self.authorization_url
    }

    /// The capability string being requested, normalized by the SDK.
    #[getter]
    fn capabilities(&self) -> &str {
        &self.capabilities
    }

    /// Check once for an approval. Returns the grant secret, or `None`.
    ///
    /// Non-blocking by design so a caller can interleave polling with its own
    /// UI (a spinner, a cancel key) instead of disappearing into a wait.
    #[pyo3(signature = (timeout_secs = 10.0))]
    fn poll_once(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Option<String>> {
        let flow = &self.inner;
        Ok(py.detach(move || block_on(timeout_secs, ops::poll_once(flow)))?)
    }

    /// Poll until approved or `timeout_secs` elapses.
    ///
    /// Raises `PubkyTimeoutError` if the user never approves.
    #[pyo3(signature = (timeout_secs = 300.0))]
    fn await_approval(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<String> {
        let flow = &self.inner;
        Ok(py.detach(move || block_on(timeout_secs, ops::await_approval(flow)))?)
    }

    fn __repr__(&self) -> String {
        format!("<AuthFlow capabilities={:?}>", self.capabilities)
    }
}

/// The capability string this plugin requests. Exposed so Python (and its
/// tests) never has to restate it.
#[pyfunction]
pub fn required_capability() -> &'static str {
    REQUIRED_CAPABILITY
}

/// The client id this plugin identifies itself with during auth.
#[pyfunction]
pub fn client_id() -> &'static str {
    APP_NAMESPACE
}
