//! End-to-end tests against a live Pubky v0.11 testnet.
//!
//! These drive the plugin's own code paths — not the SDK directly — through a
//! real homeserver: a scoped grant is approved by a programmatic signer, a
//! private profile is created, read and updated, a public context is
//! published and loaded, and a second clean machine restores from the same
//! grant and sees the same documents.
//!
//! Run with:
//!
//! ```text
//! cargo test --test e2e -- --ignored --test-threads=1
//! ```
//!
//! They are `#[ignore]`d because they bind the well-known testnet ports, and
//! single-threaded for the same reason. `StaticTestnet` is used rather than
//! `EphemeralTestnet` because the plugin resolves its testnet endpoints from
//! those fixed ports via `HERMES_PUBKY_TESTNET`.

use std::sync::Once;
use std::time::Duration;

use anyhow::{anyhow, Result};
use pubky::{Keypair, PubkySession, PubkySigner};
use pubky_testnet::StaticTestnet;

use _native::auth::ops as auth_ops;
use _native::errors::NativeError;
use _native::http::MAX_DOCUMENT_BYTES;
use _native::runtime::TESTNET_ENV;
use _native::session::ops as session_ops;
use _native::urls::REQUIRED_CAPABILITY;

const CLIENT_ID: &str = "hermes.pubky.app";
const APPROVAL_TIMEOUT: Duration = Duration::from_secs(30);

static INIT: Once = Once::new();

/// Point the plugin's process-wide SDK at the local testnet.
///
/// Must run before the first `runtime::sdk()` call, which caches the client
/// for the life of the process.
fn use_testnet() {
    INIT.call_once(|| unsafe {
        std::env::set_var(TESTNET_ENV, "1");
    });
}

struct Harness {
    _testnet: StaticTestnet,
    signer: PubkySigner,
    user: String,
}

impl Harness {
    /// Boot a testnet, create a fresh identity, and sign it up to a homeserver.
    async fn start() -> Result<Self> {
        use_testnet();
        let mut testnet = StaticTestnet::start().await?;
        let homeserver = testnet.create_random_homeserver().await?;
        let homeserver_pk = homeserver.public_key();

        let keypair = Keypair::random();
        let user = keypair.public_key().z32();
        let signer = testnet.sdk()?.signer(keypair);
        signer.signup(&homeserver_pk, None).await?;

        Ok(Self {
            _testnet: testnet,
            signer,
            user,
        })
    }

    /// Run the full grant flow: start it, approve it as the signer, collect
    /// the portable secret. This is exactly what setup does, minus Ring.
    async fn authorize(&self, capabilities: &str) -> Result<String> {
        let (caps, client_id) =
            auth_ops::parse_request(capabilities, CLIENT_ID).map_err(|e| anyhow!("{e:?}"))?;
        let flow = auth_ops::start(&caps, client_id).map_err(|e| anyhow!("{e:?}"))?;
        let url = flow.authorization_url().to_string();

        self.signer.approve_auth(&url).await?;

        let secret = tokio::time::timeout(APPROVAL_TIMEOUT, auth_ops::await_approval(&flow))
            .await
            .map_err(|_| anyhow!("grant was never approved"))?
            .map_err(|e| anyhow!("{e:?}"))?;
        Ok(secret)
    }

    /// Publish a document into the identity's public tree.
    async fn publish_public(&self, path: &str, body: &[u8]) -> Result<String> {
        let session = self.signer.signin(CLIENT_ID.try_into()?).await?;
        session.storage().put(path, body.to_vec()).await?;
        Ok(format!("pubky://{}{}", self.user, path))
    }
}

fn profile_json(revision: u32, memory: &[&str]) -> Vec<u8> {
    let entries: Vec<String> = memory.iter().map(|m| format!("{m:?}")).collect();
    format!(
        r#"{{"schemaVersion":1,"profileId":"default","baseContext":null,"user":[],"memory":[{}],"revision":{revision},"updatedAt":"2026-09-09T10:00:00Z"}}"#,
        entries.join(",")
    )
    .into_bytes()
}

// ---------------------------------------------------------------------------
// Grant auth
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn scoped_grant_is_approved_and_restores_a_working_session() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;

    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    assert_eq!(session.info().public_key().z32(), harness.user);
    let caps: Vec<String> = session
        .info()
        .capabilities()
        .iter()
        .map(ToString::to_string)
        .collect();
    assert!(
        caps.iter().any(|c| c == REQUIRED_CAPABILITY),
        "session should carry only the requested capability, got {caps:?}"
    );
    assert!(
        !caps.iter().any(|c| c == "/:rw"),
        "session must not hold the root capability, got {caps:?}"
    );
    assert!(session_ops::is_valid(&session)
        .await
        .map_err(|e| anyhow!("{e:?}"))?);
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_grant_secret_survives_a_simulated_restart() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;

    // First "run": write something.
    let first = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    session_ops::put_profile(&first, "default", profile_json(1, &["from run one"]))
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    drop(first);

    // Second "run": restore from the same stored secret and read it back.
    let second = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let raw = session_ops::get_profile(&second, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .ok_or_else(|| anyhow!("profile should exist"))?;
    assert!(String::from_utf8_lossy(&raw).contains("from run one"));
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn revoking_a_grant_stops_it_working() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;

    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    session_ops::revoke(session)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    // Restoring a revoked grant must fail, or yield a session the homeserver
    // no longer accepts. Either is a correct outcome; silently working is not.
    match session_ops::restore(&secret).await {
        Err(NativeError::Auth(_)) => {}
        Err(other) => return Err(anyhow!("expected an auth error, got {other:?}")),
        Ok(revived) => {
            let valid = session_ops::is_valid(&revived).await.unwrap_or(false);
            assert!(!valid, "a revoked grant must not produce a valid session");
        }
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_malformed_secret_is_rejected_without_a_network_round_trip() -> Result<()> {
    use_testnet();
    match session_ops::restore("not-a-real-grant-secret").await {
        Err(NativeError::Auth(_) | NativeError::Validation(_)) => Ok(()),
        Err(other) => Err(anyhow!("expected auth/validation, got {other:?}")),
        Ok(_) => Err(anyhow!("a malformed secret must not restore")),
    }
}

// ---------------------------------------------------------------------------
// Private profile storage
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn private_profile_create_read_update_delete() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    // Absent before creation.
    assert!(session_ops::get_profile(&session, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .is_none());

    // Create.
    session_ops::put_profile(&session, "default", profile_json(1, &["first"]))
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let raw = session_ops::get_profile(&session, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .ok_or_else(|| anyhow!("should exist"))?;
    assert!(String::from_utf8_lossy(&raw).contains("first"));

    // Update.
    session_ops::put_profile(&session, "default", profile_json(2, &["first", "second"]))
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let raw = session_ops::get_profile(&session, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .ok_or_else(|| anyhow!("should exist"))?;
    let text = String::from_utf8_lossy(&raw);
    assert!(text.contains("second") && text.contains(r#""revision":2"#));

    // Delete, then confirm absence and that deleting again is still fine.
    session_ops::delete_profile(&session, "default")
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    assert!(session_ops::get_profile(&session, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .is_none());
    session_ops::delete_profile(&session, "default")
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn profiles_are_independent_of_each_other() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    session_ops::put_profile(&session, "work", profile_json(1, &["work fact"]))
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    session_ops::put_profile(&session, "personal", profile_json(1, &["personal fact"]))
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    let work = session_ops::get_profile(&session, "work", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .unwrap();
    assert!(String::from_utf8_lossy(&work).contains("work fact"));
    assert!(!String::from_utf8_lossy(&work).contains("personal fact"));
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn an_oversized_profile_is_refused_before_upload() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    let huge = vec![b'x'; MAX_DOCUMENT_BYTES + 1];
    match session_ops::put_profile(&session, "default", huge).await {
        Err(NativeError::TooLarge(_)) => Ok(()),
        other => Err(anyhow!("expected TooLarge, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn the_path_guard_holds_against_a_live_homeserver() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    for hostile in ["../../pub/escape", "a/b", "", "with space"] {
        match session_ops::get_profile(&session, hostile, 0).await {
            Err(NativeError::Validation(_)) => {}
            other => return Err(anyhow!("profile id {hostile:?} was not refused: {other:?}")),
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Public context
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_published_public_context_is_readable_without_credentials() -> Result<()> {
    let harness = Harness::start().await?;
    let body = br#"{"schemaVersion":1,"id":"researcher","name":"Researcher","description":"d","instructions":"Be rigorous."}"#;
    let url = harness
        .publish_public("/pub/hermes.pubky.app/v1/contexts/researcher.json", body)
        .await?;

    // No session involved — this is the unauthenticated read path.
    let raw = session_ops::public_get(&url, 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    assert_eq!(raw, body.to_vec());
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_missing_public_context_reports_not_found() -> Result<()> {
    let harness = Harness::start().await?;
    let url = format!(
        "pubky://{}/pub/hermes.pubky.app/v1/contexts/absent.json",
        harness.user
    );
    match session_ops::public_get(&url, 0).await {
        Err(NativeError::NotFound(_)) => Ok(()),
        other => Err(anyhow!("expected NotFound, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn an_oversized_public_context_is_cut_off_mid_download() -> Result<()> {
    let harness = Harness::start().await?;
    let body = vec![b'x'; MAX_DOCUMENT_BYTES * 2];
    let url = harness
        .publish_public("/pub/hermes.pubky.app/v1/contexts/huge.json", &body)
        .await?;

    match session_ops::public_get(&url, 0).await {
        Err(NativeError::TooLarge(_)) => Ok(()),
        other => Err(anyhow!("expected TooLarge, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn the_public_reader_refuses_a_private_address() -> Result<()> {
    let harness = Harness::start().await?;
    // Even against a live server the policy is enforced client-side, so no
    // request is ever made for a /priv path through the public reader.
    let url = format!(
        "pubky://{}/priv/hermes.pubky.app/v1/profiles/default.json",
        harness.user
    );
    match session_ops::public_get(&url, 0).await {
        Err(NativeError::Validation(_)) => Ok(()),
        other => Err(anyhow!("expected Validation, got {other:?}")),
    }
}

// ---------------------------------------------------------------------------
// The portability claim
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_second_clean_machine_sees_the_same_context() -> Result<()> {
    let harness = Harness::start().await?;
    let secret = harness.authorize(REQUIRED_CAPABILITY).await?;

    // Publish a public base context, and pin it from the private profile.
    let context_body = br#"{"schemaVersion":1,"id":"researcher","name":"Researcher","description":"d","instructions":"Be rigorous."}"#;
    let context_url = harness
        .publish_public(
            "/pub/hermes.pubky.app/v1/contexts/researcher.json",
            context_body,
        )
        .await?;

    let machine_one = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let profile = format!(
        r#"{{"schemaVersion":1,"profileId":"default","baseContext":{{"url":"{context_url}","sha256":"{}"}},"user":["prefers concise answers"],"memory":["deploy via scripts/deploy.sh"],"revision":1,"updatedAt":"2026-09-09T10:00:00Z"}}"#,
        sha256_hex(context_body)
    );
    session_ops::put_profile(&machine_one, "default", profile.into_bytes())
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    // A second machine holds nothing but the same grant secret.
    let machine_two: PubkySession = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let raw = session_ops::get_profile(&machine_two, "default", 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?
        .ok_or_else(|| anyhow!("the second machine should see the profile"))?;
    let text = String::from_utf8_lossy(&raw);

    assert!(text.contains("prefers concise answers"));
    assert!(text.contains("deploy via scripts/deploy.sh"));
    assert!(text.contains(&context_url));

    // And it can load the pinned public context, with the hash still matching.
    let fetched = session_ops::public_get(&context_url, 0)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    assert_eq!(sha256_hex(&fetched), sha256_hex(context_body));
    Ok(())
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}
