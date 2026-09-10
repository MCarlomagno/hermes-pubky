//! Error taxonomy exposed to Python, plus mapping from `pubky::Error`.
//!
//! Every native call funnels through [`NativeError`]. The mapping is
//! deliberately coarse: Python only needs to distinguish "retry later"
//! (network/timeout) from "stop and tell the user" (auth/validation) and
//! "absent" (not found).

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;

create_exception!(
    _native,
    PubkyError,
    PyException,
    "Base class for Pubky errors."
);
create_exception!(
    _native,
    PubkyAuthError,
    PubkyError,
    "Authentication or authorization failed."
);
create_exception!(
    _native,
    PubkyNetworkError,
    PubkyError,
    "Transport, DNS, or homeserver failure."
);
create_exception!(
    _native,
    PubkyTimeoutError,
    PubkyNetworkError,
    "Operation exceeded its deadline."
);
create_exception!(
    _native,
    PubkyNotFoundError,
    PubkyError,
    "Resource does not exist."
);
create_exception!(
    _native,
    PubkyTooLargeError,
    PubkyError,
    "Document exceeded the size cap."
);
create_exception!(
    _native,
    PubkyValidationError,
    PubkyError,
    "Caller supplied an invalid argument."
);

/// Internal error type; converted into the Python exceptions above.
#[derive(Debug)]
pub enum NativeError {
    Auth(String),
    Network(String),
    Timeout(String),
    NotFound(String),
    TooLarge(String),
    Validation(String),
}

pub type NativeResult<T> = Result<T, NativeError>;

impl NativeError {
    pub fn validation(msg: impl Into<String>) -> Self {
        Self::Validation(msg.into())
    }
    pub fn network(msg: impl Into<String>) -> Self {
        Self::Network(msg.into())
    }
    pub fn timeout(msg: impl Into<String>) -> Self {
        Self::Timeout(msg.into())
    }
    pub fn too_large(msg: impl Into<String>) -> Self {
        Self::TooLarge(msg.into())
    }
}

impl From<NativeError> for PyErr {
    fn from(err: NativeError) -> Self {
        match err {
            NativeError::Auth(m) => PubkyAuthError::new_err(m),
            NativeError::Network(m) => PubkyNetworkError::new_err(m),
            NativeError::Timeout(m) => PubkyTimeoutError::new_err(m),
            NativeError::NotFound(m) => PubkyNotFoundError::new_err(m),
            NativeError::TooLarge(m) => PubkyTooLargeError::new_err(m),
            NativeError::Validation(m) => PubkyValidationError::new_err(m),
        }
    }
}

/// Map an SDK error into the native taxonomy.
///
/// HTTP status drives the mapping where the SDK surfaces one: 401/403 are
/// authentication problems the user must fix, 404 is a plain absence, 413
/// is an over-cap document, and 4xx otherwise is a bad request from us.
/// Everything else — including 5xx — is transient and safe to retry.
pub fn map_sdk_error(err: &pubky::Error) -> NativeError {
    use pubky::errors::{Error, RequestError};

    match err {
        Error::Request(RequestError::Server { status, message }) => {
            let code = status.as_u16();
            let msg = format!("homeserver returned {code}: {message}");
            match code {
                401 | 403 => NativeError::Auth(msg),
                404 | 410 => NativeError::NotFound(msg),
                413 => NativeError::TooLarge(msg),
                // Throttling is transient: the caller backs off and retries.
                429 => NativeError::Network(msg),
                400 | 405..=412 | 414..=499 => NativeError::Validation(msg),
                _ => NativeError::Network(msg),
            }
        }
        Error::Request(RequestError::Validation { message }) => {
            NativeError::Validation(message.clone())
        }
        Error::Request(RequestError::DecodeJson { message }) => {
            NativeError::Validation(format!("malformed response: {message}"))
        }
        Error::Request(RequestError::Transport(e)) => {
            if e.is_timeout() {
                NativeError::Timeout(format!("request timed out: {e}"))
            } else {
                NativeError::Network(format!("transport error: {e}"))
            }
        }
        Error::Authentication(e) => NativeError::Auth(format!("{e}")),
        Error::Pkarr(e) => NativeError::Network(format!("pkarr resolution failed: {e}")),
        Error::Parse(e) => NativeError::Validation(format!("invalid URL: {e}")),
        Error::Build(e) => NativeError::Network(format!("client build failed: {e}")),
    }
}

impl From<pubky::Error> for NativeError {
    fn from(err: pubky::Error) -> Self {
        map_sdk_error(&err)
    }
}

/// Register the exception types on the module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("PubkyError", m.py().get_type::<PubkyError>())?;
    m.add("PubkyAuthError", m.py().get_type::<PubkyAuthError>())?;
    m.add("PubkyNetworkError", m.py().get_type::<PubkyNetworkError>())?;
    m.add("PubkyTimeoutError", m.py().get_type::<PubkyTimeoutError>())?;
    m.add(
        "PubkyNotFoundError",
        m.py().get_type::<PubkyNotFoundError>(),
    )?;
    m.add(
        "PubkyTooLargeError",
        m.py().get_type::<PubkyTooLargeError>(),
    )?;
    m.add(
        "PubkyValidationError",
        m.py().get_type::<PubkyValidationError>(),
    )?;
    Ok(())
}
