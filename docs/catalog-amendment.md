# Catalog amendment for hermes-pubky 0.2.3

Addresses [the review of PR #114412](https://github.com/NousResearch/hermes-agent/pull/114412#issuecomment-5733437191).
Version 0.2.2 addressed the original review and is published. Version 0.2.3
addresses [the follow-up review](https://github.com/NousResearch/hermes-agent/pull/114412#issuecomment-5762489178):
the setup helper declares `requires_hermes: ">=0.21.3"` so a newer Hermes
version does not hide `/pubky`. The launcher's strict runtime check is unchanged.

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

## Validation from v0.2.2

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
  wrapper safe. A subsequent fresh GitHub installation at the v0.2.2 commit
  resolved its pinned dependency from PyPI and passed the admission validator.
- Rust formatting, 16 unit tests, and Clippy.

CI checks out the exact Hermes commit and repeats the managed-runtime,
fresh-install, and testnet acceptance checks. The v0.2.2 CI and release
workflows passed, including the Python matrix and published wheel checks.

## Validation for v0.2.3

- 422 Python tests passed against the built v0.2.3 wheel and verified Hermes.
- The real plugin loader was exercised with reported Hermes versions 0.21.3,
  0.21.4, and 0.22.0. The latter two reproduced the skipped-helper bug before
  the manifest fix and passed afterward. These simulate the version gate;
  they do not certify future Hermes runtimes.
- The launcher still refuses unverified versions and database schemas.
- Fresh wrapper installation from the candidate wheel passed the admission
  validator, safe scanner, helper command, and provider/native import checks.

## Release and PR steps

1. Review and commit the local changes, then push and let CI pass.
2. Publish tag `v0.2.3` using the release workflow; wait for the wheels and PyPI
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
requires_hermes: ">=0.21.3"
docs_url: https://github.com/MCarlomagno/hermes-pubky#readme
version: "0.2.3"
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
   another review. Distinguish the helper's minimum Hermes version from the
   launcher's exact runtime requirement; the broader helper range does not
   imply managed-agent support for unverified runtimes.
