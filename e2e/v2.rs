//! Slice 1 acceptance: the /v2 protocol against a live Pubky v0.11 testnet.
//!
//! Proves the real backend stores and returns exact bytes for each object kind,
//! paginates a listing, and refuses the authorization mistakes that matter:
//! a grant from the wrong identity, a grant scoped to another agent, and an
//! unauthenticated read of private data.
//!
//! ```text
//! TEST_PUBKY_CONNECTION_STRING=postgres://… \
//!   cargo test --test v2 -- --ignored --test-threads=1
//! ```

use std::sync::Once;
use std::time::Duration;

use anyhow::{anyhow, Result};
use pubky::{Capabilities, Keypair, PubkySession, PubkySigner};
use pubky_testnet::StaticTestnet;

use _native::auth::ops as auth_ops;
use _native::errors::NativeError;
use _native::objects::{get_object_to_file, put_object_from_file};
use _native::roots::{Document, Root, MAX_OBJECT_BYTES};
use _native::runtime::TESTNET_ENV;
use _native::session::ops as session_ops;
use _native::storage::{get_metadata, list_snapshots, put_metadata, Actor};

const CLIENT_ID: &str = "hermes.pubky.app";
const APPROVAL_TIMEOUT: Duration = Duration::from_secs(30);

static INIT: Once = Once::new();

fn use_testnet() {
    INIT.call_once(|| unsafe {
        std::env::set_var(TESTNET_ENV, "1");
    });
}

struct Harness {
    _testnet: StaticTestnet,
    signer: PubkySigner,
    owner: String,
}

impl Harness {
    async fn start() -> Result<Self> {
        use_testnet();
        let mut testnet = StaticTestnet::start().await?;
        let homeserver = testnet.create_random_homeserver().await?;
        let homeserver_pk = homeserver.public_key();

        let keypair = Keypair::random();
        let owner = keypair.public_key().z32();
        let signer = testnet.sdk()?.signer(keypair);
        signer.signup(&homeserver_pk, None).await?;
        Ok(Self {
            _testnet: testnet,
            signer,
            owner,
        })
    }

    /// Approve a grant for exactly `capability`, standing in for Pubky Ring.
    async fn grant(&self, capability: &str) -> Result<String> {
        let (caps, client_id) =
            auth_ops::parse_request(capability, CLIENT_ID).map_err(|e| anyhow!("{e:?}"))?;
        let flow = auth_ops::start(&caps, client_id).map_err(|e| anyhow!("{e:?}"))?;
        self.signer
            .approve_auth(&flow.authorization_url().to_string())
            .await?;
        tokio::time::timeout(APPROVAL_TIMEOUT, auth_ops::await_approval(&flow))
            .await
            .map_err(|_| anyhow!("grant was never approved"))?
            .map_err(|e| anyhow!("{e:?}"))
    }

    async fn agent_session(&self, agent_id: &str) -> Result<(Root, PubkySession)> {
        let root = Root::agent(&self.owner, agent_id).map_err(|e| anyhow!("{e:?}"))?;
        let secret = self.grant(&root.capability()).await?;
        let session = session_ops::restore(&secret)
            .await
            .map_err(|e| anyhow!("{e:?}"))?;
        Ok((root, session))
    }
}

fn digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(bytes))
}

fn temp_dir() -> Result<tempfile::TempDir> {
    Ok(tempfile::Builder::new()
        .prefix("hermes-pubky-v2-")
        .tempdir()?)
}

// ---------------------------------------------------------------------------
// Exact bytes, every object kind
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn objects_round_trip_exact_bytes_for_each_kind() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);
    let dir = temp_dir()?;

    // Markdown, JSON and opaque bytes each keep their own object extension so
    // the documents stay readable on the homeserver.
    let cases: Vec<(&str, Vec<u8>)> = vec![
        (
            "md",
            "# Soul\n\nBe rigorous. Cite sources — español 🎉\n"
                .as_bytes()
                .to_vec(),
        ),
        ("json", br#"{"schemaVersion":2,"model":""}"#.to_vec()),
        ("bin", (0u8..=255).cycle().take(4096).collect()),
    ];

    for (extension, body) in cases {
        let reference = format!("objects/{}.{extension}", digest(&body));
        let source = dir.path().join(format!("src.{extension}"));
        tokio::fs::write(&source, &body).await?;

        let uploaded = put_object_from_file(&root, &reference, &source, &actor)
            .await
            .map_err(|e| anyhow!("upload {extension}: {e:?}"))?;
        assert_eq!(uploaded, body.len() as u64);

        let destination = dir.path().join(format!("out.{extension}"));
        let downloaded = get_object_to_file(&root, &reference, &destination, &actor)
            .await
            .map_err(|e| anyhow!("download {extension}: {e:?}"))?;
        assert_eq!(downloaded, body.len() as u64);
        assert_eq!(
            tokio::fs::read(&destination).await?,
            body,
            "{extension} bytes differ"
        );
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_multi_chunk_file_round_trips_through_several_objects() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);
    let dir = temp_dir()?;

    // Three chunks: two full objects and a short tail, as a real database would.
    let chunks: Vec<Vec<u8>> = vec![
        vec![b'a'; MAX_OBJECT_BYTES],
        vec![b'b'; MAX_OBJECT_BYTES],
        vec![b'c'; 12_345],
    ];
    let mut references = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        let reference = format!("objects/{}.chunk", digest(chunk));
        let source = dir.path().join(format!("chunk-{index}"));
        tokio::fs::write(&source, chunk).await?;
        put_object_from_file(&root, &reference, &source, &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?;
        references.push(reference);
    }

    // Reassemble and confirm the whole file is byte-identical.
    let mut assembled = Vec::new();
    for (index, reference) in references.iter().enumerate() {
        let destination = dir.path().join(format!("out-{index}"));
        get_object_to_file(&root, reference, &destination, &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?;
        assembled.extend_from_slice(&tokio::fs::read(&destination).await?);
    }
    let expected: Vec<u8> = chunks.concat();
    assert_eq!(assembled.len(), expected.len());
    assert_eq!(digest(&assembled), digest(&expected));
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn head_and_snapshot_documents_round_trip() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);

    assert!(
        get_metadata(&root, &Document::Head, &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?
            .is_none(),
        "a fresh agent has no head"
    );

    let snapshot_id = "0123456789abcdef0123456789abcdef";
    let snapshot = br#"{"kind":"agent-snapshot","schemaVersion":2}"#.to_vec();
    put_metadata(
        &root,
        &Document::Snapshot(snapshot_id.into()),
        snapshot.clone(),
        &actor,
    )
    .await
    .map_err(|e| anyhow!("{e:?}"))?;

    let head = format!(
        r#"{{"agentId":"default","kind":"agent-head","schemaVersion":2,"sha256":"{}","snapshotId":"{snapshot_id}"}}"#,
        digest(&snapshot)
    )
    .into_bytes();
    put_metadata(&root, &Document::Head, head.clone(), &actor)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    assert_eq!(
        get_metadata(&root, &Document::Head, &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?,
        Some(head)
    );
    assert_eq!(
        get_metadata(&root, &Document::Snapshot(snapshot_id.into()), &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?,
        Some(snapshot)
    );
    Ok(())
}

// ---------------------------------------------------------------------------
// Authorization
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn the_grant_carries_only_the_requested_agent_capability() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let held: Vec<String> = session
        .info()
        .capabilities()
        .iter()
        .map(ToString::to_string)
        .collect();
    assert!(held.iter().any(|c| c == &root.capability()), "got {held:?}");
    assert!(
        !held.iter().any(|c| c == "/:rw"),
        "must not hold root: {held:?}"
    );
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_grant_for_another_agent_cannot_write_this_one() -> Result<()> {
    let harness = Harness::start().await?;
    // Authorized for "other", used against "default".
    let (_other, session) = harness.agent_session("other").await?;
    let target = Root::agent(&harness.owner, "default").map_err(|e| anyhow!("{e:?}"))?;
    let actor = Actor::Session(session);

    match put_metadata(&target, &Document::Head, b"{}".to_vec(), &actor).await {
        Err(NativeError::Auth(_)) => Ok(()),
        other => Err(anyhow!("expected an auth error, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_session_from_another_identity_is_refused() -> Result<()> {
    let harness = Harness::start().await?;
    let (_root, session) = harness.agent_session("default").await?;
    // Same agent id, a different owner: the address is not this session's.
    let stranger = Keypair::random().public_key().z32();
    let foreign = Root::agent(&stranger, "default").map_err(|e| anyhow!("{e:?}"))?;
    let actor = Actor::Session(session);

    match get_metadata(&foreign, &Document::Head, &actor).await {
        Err(NativeError::Auth(_)) => Ok(()),
        other => Err(anyhow!("expected an auth error, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn private_agent_storage_is_never_read_without_a_session() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    put_metadata(
        &root,
        &Document::Head,
        b"{}".to_vec(),
        &Actor::Session(session),
    )
    .await
    .map_err(|e| anyhow!("{e:?}"))?;

    // Refused locally, before a request is made: no grant, no private read.
    match get_metadata(&root, &Document::Head, &Actor::Public).await {
        Err(NativeError::Auth(_)) => Ok(()),
        other => Err(anyhow!("expected an auth error, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_public_template_is_readable_without_any_credentials() -> Result<()> {
    let harness = Harness::start().await?;
    let root = Root::template(&harness.owner, "researcher").map_err(|e| anyhow!("{e:?}"))?;

    // Publishing needs its own template-scoped grant.
    let secret = harness.grant(&root.capability()).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    let head = br#"{"kind":"template-head","schemaVersion":2}"#.to_vec();
    put_metadata(
        &root,
        &Document::Head,
        head.clone(),
        &Actor::Session(session),
    )
    .await
    .map_err(|e| anyhow!("{e:?}"))?;

    // Read with no session at all.
    let fetched = get_metadata(&root, &Document::Head, &Actor::Public)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
    assert_eq!(fetched, Some(head));
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn an_agent_grant_cannot_publish_a_template() -> Result<()> {
    let harness = Harness::start().await?;
    let (_agent, session) = harness.agent_session("default").await?;
    let template = Root::template(&harness.owner, "researcher").map_err(|e| anyhow!("{e:?}"))?;

    match put_metadata(
        &template,
        &Document::Head,
        b"{}".to_vec(),
        &Actor::Session(session),
    )
    .await
    {
        Err(NativeError::Auth(_)) => Ok(()),
        other => Err(anyhow!("expected an auth error, got {other:?}")),
    }
}

// ---------------------------------------------------------------------------
// Limits and pagination
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn oversized_metadata_is_refused_before_upload() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);

    let too_big = vec![b'x'; _native::roots::MAX_HEAD_BYTES + 1];
    match put_metadata(&root, &Document::Head, too_big, &actor).await {
        Err(NativeError::TooLarge(_)) => {}
        other => return Err(anyhow!("expected TooLarge for head, got {other:?}")),
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn an_object_whose_bytes_disagree_with_its_name_is_refused() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);
    let dir = temp_dir()?;

    let source = dir.path().join("lying");
    tokio::fs::write(&source, b"actual bytes").await?;
    let wrong = format!("objects/{}.bin", "0".repeat(64));

    match put_object_from_file(&root, &wrong, &source, &actor).await {
        Err(NativeError::Validation(_)) => Ok(()),
        other => Err(anyhow!("expected Validation, got {other:?}")),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn snapshot_listing_paginates_and_terminates() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);

    let mut written = Vec::new();
    for index in 0..7u32 {
        let id = format!("{index:032x}");
        put_metadata(
            &root,
            &Document::Snapshot(id.clone()),
            br#"{"kind":"agent-snapshot"}"#.to_vec(),
            &actor,
        )
        .await
        .map_err(|e| anyhow!("{e:?}"))?;
        written.push(id);
    }

    // Walk in pages of three; the listing must end rather than loop.
    let mut seen = Vec::new();
    let mut cursor: Option<String> = None;
    for _ in 0..10 {
        let page = list_snapshots(&root, cursor.as_deref(), 3, &actor)
            .await
            .map_err(|e| anyhow!("{e:?}"))?;
        seen.extend(page.snapshot_ids.clone());
        match page.next_cursor {
            Some(next) => cursor = Some(next),
            None => break,
        }
    }
    seen.sort();
    written.sort();
    assert_eq!(seen, written, "pagination lost or duplicated entries");
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn an_unbounded_listing_limit_is_refused() -> Result<()> {
    let harness = Harness::start().await?;
    let (root, session) = harness.agent_session("default").await?;
    let actor = Actor::Session(session);

    for limit in [0u16, 501] {
        match list_snapshots(&root, None, limit, &actor).await {
            Err(NativeError::Validation(_)) => {}
            other => return Err(anyhow!("limit {limit} should be refused, got {other:?}")),
        }
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requires the well-known testnet ports"]
async fn a_capability_narrower_than_the_root_is_detected_locally() -> Result<()> {
    let harness = Harness::start().await?;
    // A read-only grant cannot satisfy a root that needs read+write.
    let root = Root::agent(&harness.owner, "default").map_err(|e| anyhow!("{e:?}"))?;
    let read_only = format!("{}:r", root.base_path());
    let _caps: Capabilities = read_only.parse().map_err(|e| anyhow!("{e}"))?;
    let secret = harness.grant(&read_only).await?;
    let session = session_ops::restore(&secret)
        .await
        .map_err(|e| anyhow!("{e:?}"))?;

    match put_metadata(
        &root,
        &Document::Head,
        b"{}".to_vec(),
        &Actor::Session(session),
    )
    .await
    {
        Err(NativeError::Auth(_)) => Ok(()),
        other => Err(anyhow!("expected an auth error, got {other:?}")),
    }
}
