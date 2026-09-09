//! Bounded response reading.
//!
//! Documents fetched from a homeserver are attacker-influenced: the base
//! context belongs to a third party, and even the private profile is served
//! by an operator we do not control. Every body is therefore read through a
//! streaming cap rather than `Response::bytes()`, so a server that lies about
//! (or omits) `Content-Length` still cannot exhaust memory.

use futures_util::StreamExt;
use reqwest::Response;

use crate::errors::{NativeError, NativeResult};

/// Maximum size of any single document this plugin will ingest.
pub const MAX_DOCUMENT_BYTES: usize = 64 * 1024;

pub async fn read_capped(response: Response, cap: usize) -> NativeResult<Vec<u8>> {
    if let Some(len) = response.content_length() {
        if len > cap as u64 {
            return Err(NativeError::too_large(format!(
                "document is {len} bytes, over the {cap} byte cap"
            )));
        }
    }

    let mut buf: Vec<u8> = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| NativeError::network(format!("read failed: {e}")))?;
        if buf.len() + chunk.len() > cap {
            return Err(NativeError::too_large(format!(
                "document exceeded the {cap} byte cap while downloading"
            )));
        }
        buf.extend_from_slice(&chunk);
    }
    Ok(buf)
}

/// Clamp a caller-supplied cap to the hard maximum.
pub fn effective_cap(requested: usize) -> usize {
    if requested == 0 {
        MAX_DOCUMENT_BYTES
    } else {
        requested.min(MAX_DOCUMENT_BYTES)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cap_defaults_and_clamps() {
        assert_eq!(effective_cap(0), MAX_DOCUMENT_BYTES);
        assert_eq!(effective_cap(10), 10);
        assert_eq!(effective_cap(usize::MAX), MAX_DOCUMENT_BYTES);
        assert_eq!(effective_cap(MAX_DOCUMENT_BYTES + 1), MAX_DOCUMENT_BYTES);
    }
}
