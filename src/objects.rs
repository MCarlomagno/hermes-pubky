//! Bounded, verified object transfers.
//!
//! Objects are content-addressed and capped at 1 MiB, so a download that does
//! not hash to the digest in its name is discarded, and an upload whose bytes
//! disagree with its name is refused before any request is made.
//!
//! Reference: implementation plan sections 5.4 and 14.

use std::path::{Path, PathBuf};

use futures_util::StreamExt;
use pubky::StatusCode;
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

use crate::errors::{map_sdk_error, NativeError, NativeResult};
use crate::roots::{digest_of, Document, Root};
use crate::runtime::sdk;
use crate::storage::{check_actor, check_capability, check_owner, Actor};

/// Download one object to `destination`, verifying its digest first.
///
/// The bytes land in a sibling temporary file and are renamed only after the
/// hash matches, so an interrupted transfer can never be mistaken for verified
/// cached content.
pub async fn get_object_to_file(
    root: &Root,
    reference: &str,
    destination: &Path,
    actor: &Actor,
) -> NativeResult<u64> {
    check_actor(root, actor, false)?;
    let document = Document::Object(reference.to_string());
    let path = root.path_for(&document)?;
    let expected = digest_of(reference)?;
    let cap = document.max_bytes();

    let response = match actor {
        Actor::Session(session) => {
            check_owner(root, session)?;
            session
                .storage()
                .get(path.as_str())
                .await
                .map_err(|e| map_sdk_error(&e))?
        }
        Actor::Public => {
            let address = format!("pubky://{}{}", root.owner(), path);
            sdk()?
                .public_storage()
                .get(address.as_str())
                .await
                .map_err(|e| map_sdk_error(&e))?
        }
    };
    if response.status() == StatusCode::NOT_FOUND {
        return Err(NativeError::NotFound(format!("no object at {path}")));
    }

    let temp = temp_path(destination);
    if let Some(parent) = destination.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| NativeError::network(format!("could not create {parent:?}: {e}")))?;
    }

    let outcome = stream_to_file(response, &temp, cap, &expected).await;
    match outcome {
        Ok(size) => {
            tokio::fs::rename(&temp, destination).await.map_err(|e| {
                NativeError::network(format!("could not install {destination:?}: {e}"))
            })?;
            Ok(size)
        }
        Err(e) => {
            let _ = tokio::fs::remove_file(&temp).await;
            Err(e)
        }
    }
}

/// Upload one object read from a local file.
pub async fn put_object_from_file(
    root: &Root,
    reference: &str,
    source: &Path,
    actor: &Actor,
) -> NativeResult<u64> {
    check_actor(root, actor, true)?;
    let session = actor.session("uploading an object")?;
    check_owner(root, session)?;
    check_capability(root, session)?;

    let document = Document::Object(reference.to_string());
    let path = root.path_for(&document)?;
    let expected = digest_of(reference)?;
    let cap = document.max_bytes();

    let metadata = tokio::fs::metadata(source)
        .await
        .map_err(|e| NativeError::validation(format!("cannot read {source:?}: {e}")))?;
    if metadata.len() > cap as u64 {
        return Err(NativeError::too_large(format!(
            "{source:?} is {} bytes, over the {cap} byte object cap",
            metadata.len()
        )));
    }

    // ponytail: an object is capped at 1 MiB, so buffering one is fine; the
    // whole-archive streaming requirement applies to files, which are chunked
    // into objects before they reach here.
    let body = tokio::fs::read(source)
        .await
        .map_err(|e| NativeError::validation(format!("cannot read {source:?}: {e}")))?;

    // Never upload bytes under a name that does not describe them.
    let actual = hex::encode(Sha256::digest(&body));
    if actual != expected {
        return Err(NativeError::validation(format!(
            "{source:?} hashes to {actual} but {reference} claims {expected}"
        )));
    }

    let size = body.len() as u64;
    session
        .storage()
        .put(path.as_str(), body)
        .await
        .map_err(|e| map_sdk_error(&e))?;
    Ok(size)
}

fn temp_path(destination: &Path) -> PathBuf {
    let mut name = destination
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "object".to_string());
    name.push_str(".partial");
    destination.with_file_name(name)
}

/// Stream a response into a file, enforcing the cap and verifying the digest.
async fn stream_to_file(
    response: reqwest::Response,
    temp: &Path,
    cap: usize,
    expected_digest: &str,
) -> NativeResult<u64> {
    if let Some(len) = response.content_length() {
        if len > cap as u64 {
            return Err(NativeError::too_large(format!(
                "object is {len} bytes, over the {cap} byte cap"
            )));
        }
    }

    let mut file = tokio::fs::File::create(temp)
        .await
        .map_err(|e| NativeError::network(format!("could not create {temp:?}: {e}")))?;
    let mut hasher = Sha256::new();
    let mut written: u64 = 0;
    let mut stream = response.bytes_stream();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| NativeError::network(format!("read failed: {e}")))?;
        written += chunk.len() as u64;
        if written > cap as u64 {
            return Err(NativeError::too_large(format!(
                "object exceeded the {cap} byte cap while downloading"
            )));
        }
        hasher.update(&chunk);
        file.write_all(&chunk)
            .await
            .map_err(|e| NativeError::network(format!("write failed: {e}")))?;
    }
    file.flush()
        .await
        .map_err(|e| NativeError::network(format!("flush failed: {e}")))?;
    file.sync_all()
        .await
        .map_err(|e| NativeError::network(format!("fsync failed: {e}")))?;

    let actual = hex::encode(hasher.finalize());
    if actual != expected_digest {
        return Err(NativeError::Validation(format!(
            "downloaded object hashes to {actual}, expected {expected_digest}"
        )));
    }
    Ok(written)
}
