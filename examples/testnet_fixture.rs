//! A live Pubky testnet with pre-authorized grants, for acceptance tests.
//!
//! Starts a static testnet, signs a fresh identity up to a homeserver, approves
//! agent and template grants programmatically (standing in for Pubky Ring),
//! prints them as JSON, and stays alive until interrupted.
//!
//! ```text
//! TEST_PUBKY_CONNECTION_STRING=postgres://... \
//!   cargo run --example testnet_fixture -- --agent default --template researcher
//! ```
//!
//! Then, in another shell:
//!
//! ```text
//! export HERMES_PUBKY_TESTNET=1
//! python scripts/check_managed_handoff.py --fixture /path/to/fixture.json
//! ```

use anyhow::{anyhow, Result};
use pubky::{Capabilities, Keypair};
use pubky_testnet::StaticTestnet;

const CLIENT_ID: &str = "hermes.pubky.app";

#[tokio::main]
async fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let mut agent_id = "default".to_string();
    let mut template_id = "researcher".to_string();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--agent" => agent_id = args.next().unwrap_or(agent_id),
            "--template" => template_id = args.next().unwrap_or(template_id),
            other => return Err(anyhow!("unknown argument {other}")),
        }
    }

    let mut testnet = StaticTestnet::start().await?;
    let homeserver = testnet.create_random_homeserver().await?;
    let homeserver_pk = homeserver.public_key();

    let keypair = Keypair::random();
    let owner = keypair.public_key().z32();
    let sdk = testnet.sdk()?;
    let signer = sdk.signer(keypair);
    signer.signup(&homeserver_pk, None).await?;

    let agent_scope = _native::roots::agent_scope(&agent_id).map_err(|e| anyhow!("{e:?}"))?;
    let template_scope =
        _native::roots::template_scope(&template_id).map_err(|e| anyhow!("{e:?}"))?;

    let agent_grant = approve(&sdk, &signer, &agent_scope).await?;
    let template_grant = approve(&sdk, &signer, &template_scope).await?;

    println!(
        "{}",
        serde_json::json!({
            "owner": owner,
            "homeserver": homeserver_pk.z32(),
            "agentId": agent_id,
            "agentScope": agent_scope,
            "agentGrant": agent_grant,
            "templateId": template_id,
            "templateScope": template_scope,
            "templateGrant": template_grant,
        })
    );
    println!("READY");

    tokio::signal::ctrl_c().await?;
    Ok(())
}

/// Start a grant flow and approve it as the signer would in Pubky Ring.
async fn approve(sdk: &pubky::Pubky, signer: &pubky::PubkySigner, scope: &str) -> Result<String> {
    let caps: Capabilities = scope.parse().map_err(|e| anyhow!("{e}"))?;
    let flow =
        sdk.start_grant_auth_flow(&caps, pubky::AuthFlowKind::signin(), CLIENT_ID.try_into()?)?;
    signer
        .approve_auth(&flow.authorization_url().to_string())
        .await?;
    let credential = flow.await_credential().await?;
    credential
        .export_local_secret()
        .await
        .ok_or_else(|| anyhow!("grant key is not exportable"))
}
