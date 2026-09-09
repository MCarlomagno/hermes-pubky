//! Session restore and revocation.
//!
//! Only what the managed launcher needs: turn a stored grant into a session,
//! check it is still accepted, and give it back. Storage lives in `storage.rs`
//! and `objects.rs`, addressed through typed roots rather than through methods
//! on a session.

use crate::errors::{map_sdk_error, NativeError, NativeResult};
use crate::runtime::sdk;
use pubky::PubkySession;

/// Transport-free operations, shared by the bindings and the e2e suite.
pub mod ops {
    use super::{map_sdk_error, sdk, NativeError, NativeResult, PubkySession};

    /// Restore a session from a previously exported grant secret.
    ///
    /// The secret is bearer-equivalent material; it is never logged.
    pub async fn restore(secret: &str) -> NativeResult<PubkySession> {
        if secret.trim().is_empty() {
            return Err(NativeError::validation("no grant secret was supplied"));
        }
        sdk()?
            .restore_session(secret)
            .await
            .map_err(|e| map_sdk_error(&e))
    }

    /// True when the homeserver still accepts this session.
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
    /// grant may call; `GrantManager::revoke` is restricted to root sessions.
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
}
