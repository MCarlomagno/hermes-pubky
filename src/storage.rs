//! Scoped transport for the /v2 protocol.
//!
//! Callers name a [`Root`] and a [`Document`]; this module renders the path and
//! performs the transfer. It never accepts a free-form URL, and it refuses an
//! actor that does not match the root: private agent data always requires an
//! authenticated session, and a public template read never sends a grant.
//!
//! Reference: implementation plan sections 5, 6.3 and 14.

use pubky::{PubkySession, StatusCode};

use crate::errors::{map_sdk_error, NativeError, NativeResult};
use crate::http::read_capped;
use crate::roots::{Document, Root, MAX_LIST_LIMIT};
use crate::runtime::sdk;

/// Who is performing an operation.
pub enum Actor {
    /// Unauthenticated. Valid only for reading a public template.
    Public,
    /// A grant-backed session scoped to the root being addressed.
    Session(PubkySession),
}

impl Actor {
    pub(crate) fn session(&self, what: &str) -> NativeResult<&PubkySession> {
        match self {
            Actor::Session(session) => Ok(session),
            Actor::Public => Err(NativeError::Auth(format!(
                "{what} requires an authenticated session"
            ))),
        }
    }
}

/// Refuse actor/root combinations that would leak or under-authorize.
pub(crate) fn check_actor(root: &Root, actor: &Actor, write: bool) -> NativeResult<()> {
    match (root, actor, write) {
        // Private agent data is never readable or writable without a session.
        (Root::Agent { .. }, Actor::Public, _) => Err(NativeError::Auth(
            "private agent storage requires an authenticated session".to_string(),
        )),
        // Publishing a template needs a template-scoped session.
        (Root::Template { .. }, Actor::Public, true) => Err(NativeError::Auth(
            "publishing a template requires an authenticated session".to_string(),
        )),
        _ => Ok(()),
    }
}

/// Confirm a session's owner is the root's owner before trusting it.
pub(crate) fn check_owner(root: &Root, session: &PubkySession) -> NativeResult<()> {
    let signed_in = session.info().public_key().z32();
    if signed_in != root.owner() {
        return Err(NativeError::Auth(format!(
            "session belongs to {signed_in} but the address names {}",
            root.owner()
        )));
    }
    Ok(())
}

/// Confirm a session actually carries the capability this root needs.
///
/// The homeserver enforces this too; checking locally turns a
/// mis-scoped grant into a clear message instead of a 403 mid-transfer.
pub fn check_capability(root: &Root, session: &PubkySession) -> NativeResult<()> {
    let required = root.capabilities()?;
    let info = session.info();
    let held = info.capabilities();
    for capability in required.iter() {
        let covered = held.iter().any(|h| {
            h.scope_covers_path(capability.scope())
                && capability.actions().iter().all(|a| h.actions().contains(a))
        });
        if !covered {
            return Err(NativeError::Auth(format!(
                "session lacks the capability {capability}; re-authorize this agent"
            )));
        }
    }
    Ok(())
}

// -- metadata (head and snapshot documents) ----------------------------------

/// Read a head or snapshot document. `None` means it does not exist.
pub async fn get_metadata(
    root: &Root,
    document: &Document,
    actor: &Actor,
) -> NativeResult<Option<Vec<u8>>> {
    check_actor(root, actor, false)?;
    let path = root.path_for(document)?;
    let cap = document.max_bytes();

    let response = match actor {
        Actor::Session(session) => {
            check_owner(root, session)?;
            match session.storage().get(path.as_str()).await {
                Ok(response) => response,
                Err(e) => return absent_or_error(e),
            }
        }
        Actor::Public => {
            let address = format!("pubky://{}{}", root.owner(), path);
            match sdk()?.public_storage().get(address.as_str()).await {
                Ok(response) => response,
                Err(e) => return absent_or_error(e),
            }
        }
    };

    if response.status() == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    read_capped(response, cap).await.map(Some)
}

/// Write a head or snapshot document.
pub async fn put_metadata(
    root: &Root,
    document: &Document,
    body: Vec<u8>,
    actor: &Actor,
) -> NativeResult<()> {
    check_actor(root, actor, true)?;
    let session = actor.session("writing metadata")?;
    check_owner(root, session)?;
    check_capability(root, session)?;

    let path = root.path_for(document)?;
    let cap = document.max_bytes();
    if body.len() > cap {
        return Err(NativeError::too_large(format!(
            "{path} is {} bytes, over its {cap} byte cap",
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

// -- listing -----------------------------------------------------------------

/// One page of snapshot ids, plus the cursor to continue from.
#[derive(Debug)]
pub struct SnapshotPage {
    pub snapshot_ids: Vec<String>,
    pub next_cursor: Option<String>,
}

/// List snapshot documents, newest-agnostic and explicitly paginated.
///
/// Routine operation reads known paths; this exists for `agent history` and
/// `agent restore`, not for polling.
pub async fn list_snapshots(
    root: &Root,
    cursor: Option<&str>,
    limit: u16,
    actor: &Actor,
) -> NativeResult<SnapshotPage> {
    check_actor(root, actor, false)?;
    if limit == 0 || limit > MAX_LIST_LIMIT {
        return Err(NativeError::validation(format!(
            "limit must be between 1 and {MAX_LIST_LIMIT}"
        )));
    }
    let session = actor.session("listing snapshots")?;
    check_owner(root, session)?;

    let directory = format!("{}snapshots/", root.base_path());
    let storage = session.storage();
    let mut builder = storage
        .list(directory.as_str())
        .map_err(|e| map_sdk_error(&e))?
        .limit(limit);
    if let Some(cursor) = cursor {
        builder = builder.cursor(cursor);
    }
    let entries = builder.send().await.map_err(|e| map_sdk_error(&e))?;

    let mut snapshot_ids = Vec::with_capacity(entries.len());
    let mut last = None;
    for entry in &entries {
        // An entry outside the root we asked about means the server answered a
        // different question; refuse rather than reinterpret it.
        if entry.owner.z32() != root.owner() {
            return Err(NativeError::validation(format!(
                "listing returned an entry owned by {}",
                entry.owner.z32()
            )));
        }
        let path = entry.path.as_str();
        let name = path.strip_prefix(directory.as_str()).ok_or_else(|| {
            NativeError::validation(format!("listing returned {path} outside {directory}"))
        })?;
        let id = name.strip_suffix(".json").ok_or_else(|| {
            NativeError::validation(format!("listing returned a non-snapshot entry {name}"))
        })?;
        crate::roots::validate_hex32(id, "snapshot id")?;
        snapshot_ids.push(id.to_string());
        // The homeserver's cursor is the entry's canonical pubky URL, not its
        // bare path (see ListBuilder::cursor).
        last = Some(entry.to_pubky_url());
    }

    // A full page implies more may follow; a short page ends the listing.
    let next_cursor = if entries.len() as u16 == limit {
        last
    } else {
        None
    };
    Ok(SnapshotPage {
        snapshot_ids,
        next_cursor,
    })
}

// -- helpers -----------------------------------------------------------------

fn absent_or_error(err: pubky::Error) -> NativeResult<Option<Vec<u8>>> {
    match map_sdk_error(&err) {
        NativeError::NotFound(_) => Ok(None),
        mapped => Err(mapped),
    }
}
