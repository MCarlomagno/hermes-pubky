//! Shared tokio runtime and HTTP client construction.
//!
//! One multi-threaded runtime is created lazily and reused for the life of
//! the process. Python callers always block on it with the GIL released, so
//! a single runtime is enough and avoids paying client/DNS setup per call.

use std::sync::OnceLock;
use std::time::Duration;

use pubky::Pubky;
use tokio::runtime::Runtime;

use crate::errors::{NativeError, NativeResult};

static RUNTIME: OnceLock<Runtime> = OnceLock::new();
static SDK: OnceLock<Pubky> = OnceLock::new();

/// Environment flag that switches DNS/relay resolution to a local testnet.
/// Used by the e2e suite; never set in normal operation.
pub const TESTNET_ENV: &str = "HERMES_PUBKY_TESTNET";

pub fn runtime() -> NativeResult<&'static Runtime> {
    if let Some(rt) = RUNTIME.get() {
        return Ok(rt);
    }
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .thread_name("hermes-pubky")
        .build()
        .map_err(|e| NativeError::network(format!("failed to start async runtime: {e}")))?;
    Ok(RUNTIME.get_or_init(|| rt))
}

fn testnet_enabled() -> bool {
    std::env::var(TESTNET_ENV)
        .map(|v| matches!(v.as_str(), "1" | "true" | "TRUE" | "yes"))
        .unwrap_or(false)
}

/// Build (once) the process-wide Pubky SDK facade.
///
/// Going through the facade rather than a bare HTTP client keeps URL
/// resolution, auth flows and public reads on the SDK's own code paths.
pub fn sdk() -> NativeResult<&'static Pubky> {
    if let Some(s) = SDK.get() {
        return Ok(s);
    }
    let built = if testnet_enabled() {
        Pubky::testnet()
    } else {
        Pubky::new()
    }
    .map_err(|e| NativeError::network(format!("failed to build Pubky SDK: {e}")))?;
    Ok(SDK.get_or_init(|| built))
}

/// Run `fut` on the shared runtime with a deadline.
///
/// `timeout_secs` <= 0 means "no deadline"; callers that expose a timeout to
/// Python always pass a positive value.
pub fn block_on<F, T>(timeout_secs: f64, fut: F) -> NativeResult<T>
where
    F: std::future::Future<Output = NativeResult<T>>,
{
    let rt = runtime()?;
    rt.block_on(async move {
        if timeout_secs <= 0.0 {
            return fut.await;
        }
        match tokio::time::timeout(Duration::from_secs_f64(timeout_secs), fut).await {
            Ok(result) => result,
            Err(_) => Err(NativeError::timeout(format!(
                "operation did not complete within {timeout_secs:.1}s"
            ))),
        }
    })
}
