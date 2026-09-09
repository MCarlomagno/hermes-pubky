//! A live Pubky testnet with a pre-authorized grant, for manual and scripted
//! validation of the Hermes-facing flows.
//!
//! Starts a static testnet, signs a fresh identity up to a homeserver,
//! approves a scoped grant programmatically (standing in for Pubky Ring),
//! optionally publishes a public base context, prints the details as JSON,
//! and then stays alive until interrupted.
//!
//! ```text
//! TEST_PUBKY_CONNECTION_STRING=postgres://... \
//!   cargo run --example testnet_fixture
//! ```
//!
//! Then, in another shell:
//!
//! ```text
//! export HERMES_PUBKY_TESTNET=1
//! export HERMES_PUBKY_GRANT_SECRET="<grant_secret from the JSON>"
//! hermes memory setup pubky
//! ```

use anyhow::{anyhow, Result};
use pubky::{Capabilities, Keypair};
use pubky_testnet::StaticTestnet;

const CLIENT_ID: &str = "hermes.pubky.app";
const CAPABILITY: &str = "/priv/hermes.pubky.app/v1/profiles/:rw";
const CONTEXT_PATH: &str = "/pub/hermes.pubky.app/v1/contexts/researcher.json";
const CONTEXT_BODY: &[u8] = br#"{"schemaVersion":1,"id":"researcher","name":"Researcher","description":"Research-oriented agent instructions","instructions":"Be rigorous. Cite your sources. Prefer primary literature."}"#;

#[tokio::main]
async fn main() -> Result<()> {
    let mut testnet = StaticTestnet::start().await?;
    let homeserver = testnet.create_random_homeserver().await?;
    let homeserver_pk = homeserver.public_key();

    let keypair = Keypair::random();
    let user = keypair.public_key().z32();
    let sdk = testnet.sdk()?;
    let signer = sdk.signer(keypair);
    signer.signup(&homeserver_pk, None).await?;

    // Publish a public base context so `hermes pubky base set` has a target.
    let session = signer.signin(CLIENT_ID.try_into()?).await?;
    session
        .storage()
        .put(CONTEXT_PATH, CONTEXT_BODY.to_vec())
        .await?;

    // Run the grant flow, approving it ourselves in place of Pubky Ring.
    let caps: Capabilities = CAPABILITY
        .parse()
        .map_err(|e| anyhow!("invalid capability: {e}"))?;
    let flow =
        sdk.start_grant_auth_flow(&caps, pubky::AuthFlowKind::signin(), CLIENT_ID.try_into()?)?;
    let auth_url = flow.authorization_url().to_string();
    signer.approve_auth(&auth_url).await?;

    let credential = flow.await_credential().await?;
    let secret = credential
        .export_local_secret()
        .await
        .ok_or_else(|| anyhow!("grant key is not exportable"))?;

    println!(
        "{}",
        serde_json::json!({
            "user": user,
            "homeserver": homeserver_pk.z32(),
            "grant_secret": secret,
            "capability": CAPABILITY,
            "context_url": format!("pubky://{user}{CONTEXT_PATH}"),
        })
    );
    println!("READY");

    // Hold the testnet open for the client under test.
    tokio::signal::ctrl_c().await?;
    Ok(())
}
