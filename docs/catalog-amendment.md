# Catalog amendment for hermes-pubky 0.2.2

Addresses [the review of PR #114412](https://github.com/NousResearch/hermes-agent/pull/114412#issuecomment-5733437191).
This is an uncommitted release candidate; 0.2.2 has not been published.

## Changes

- Supports Hermes 0.21.3 / conversation schema 30, tested against upstream
  commit `a51143fbbe6ddbc0c7f403d0579c4d75504c6793`. Hermes now requires a
  source/editable installation; the old PyPI installation instructions do not
  apply to this runtime.
- Upgrades schema-22 snapshots with Hermes' own migrations on staged copies.
  Preserves conversation history, system prompts, search metadata, and old
  remote checkpoints. All devices must upgrade after a schema-30 checkpoint
  is published. Unverified schemas remain blocked.
- Describes the catalog entry as a setup helper. Its pinned Python dependency
  supplies the launcher, provider, and native SDK; `/pubky` explains how to
  initialize or attach an agent and launch it through `hermes-pubky run`.
- Discloses remote synchronization of conversation database snapshots,
  portable configuration, and other managed state, including homeserver
  operator access. Enabling the helper does not start syncing a normal profile.

## Local validation

Validated on macOS ARM64 with Python 3.11 using the built 0.2.2 wheel and the
upstream commit above:

- 415 Python tests, including schema-22 upgrade and failure recovery tests.
- 26 real Hermes checks: launch, provider discovery, snapshot restore, conversation resume,
  and legacy conversation upgrade/resume against a local deterministic model.
- 22 real Pubky testnet checks: create on A, recover on B, run repeatedly, fetch and edit
  workspace files, recover on A again, and revoke the test grant.
- Fresh environment with no installed hermes-pubky: directory wrapper copied
  into an isolated profile, `hermes plugins validate --install-deps`, enablement,
  help command, launcher, provider import, and native extension import.
  Dependency resolution used the local candidate wheel; the scanner rated the
  wrapper safe. Remote GitHub/PyPI installation still needs verification after
  publication.
- Rust formatting, 16 unit tests, and Clippy.

CI now checks out the exact Hermes commit and repeats the managed-runtime,
fresh-install, and testnet acceptance checks. The other Python versions and
Linux/wheel architecture checks remain release/CI work.

## Release and PR steps

1. Review and commit the local changes, then push and let CI pass.
2. Publish tag `v0.2.2` using the release workflow; wait for the wheels and PyPI
   package to become available.
3. Repeat the install from GitHub in a fresh Hermes environment with no local
   wheel override. Check the pinned commit with the admission validator.
4. Update `plugin-catalog/hermes-pubky.yaml` in PR #114412 with the following
   content, replacing `RELEASE_COMMIT_SHA` with the actual tested 40-character
   commit. Do not submit the placeholder.

```yaml
name: hermes-pubky
repo: https://github.com/MCarlomagno/hermes-pubky
sha: RELEASE_COMMIT_SHA
subdir: plugin
description: Setup helper for managed Pubky agents. The launcher syncs agent state, including conversation database snapshots and portable configuration, to your remote Pubky homeserver.
maintainer: MCarlomagno
tier: community
category: memory
requires_hermes: "==0.21.3"
docs_url: https://github.com/MCarlomagno/hermes-pubky#readme
version: "0.2.2"
platforms:
  - linux
  - macos
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

5. Rewrite the PR description for the final support requirements and helper
   behavior. Include the published-install and recovery evidence, then request
   another review. Do not claim a broader version range until it is tested.
