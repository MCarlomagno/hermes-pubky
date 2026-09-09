# hermes-pubky

Portable [Hermes](https://github.com/NousResearch/hermes) agent context over
[Pubky](https://github.com/pubky/pubky-core).

Your agent's context stops living on one machine. `hermes-pubky` adds a Hermes
memory provider named `pubky` that loads:

- **a public base context** you explicitly approved — shareable agent
  instructions addressed by a `pubky://` URL, and
- **a private portable overlay** — your own user facts and agent memory, stored
  in a private document on your homeserver and updated as Hermes writes memory.

Your local `SOUL.md`, `USER.md` and `MEMORY.md` are untouched and still take
precedence. The plugin only ever *reads* them.

> **Status: v0.1, alpha.** Homeserver `/priv` storage is itself alpha. See
> [What the homeserver operator can see](#what-the-homeserver-operator-can-see)
> before storing anything sensitive.

## Install

```bash
cd ~/.hermes/hermes-agent
uv pip install hermes-pubky==0.1.0
hermes-pubky install
hermes memory setup pubky
```

`hermes-pubky install` writes a three-file shim into
`$HERMES_HOME/plugins/pubky/`. Hermes discovers memory providers by scanning
that directory rather than through Python entry points, so a pip install alone
does not make the provider visible. The shim just imports the pip-installed
package, so upgrades are a plain `uv pip install -U hermes-pubky`.

Requires Python 3.11–3.13, Hermes 0.19+, and a Pubky homeserver running v0.11
or later.

### Setup walks you through

1. A profile id (default: `default`).
2. Pubky Auth — an authorization URL you approve in [Pubky
   Ring](https://pubky.org). The grant secret is stored in your
   profile-scoped `.env` at mode `0600` and never leaves your machine.
3. Loading your existing remote profile, or creating one.
4. For a new profile: optionally pinning a public base context, and optionally
   importing your existing `USER.md` / `MEMORY.md`. Filenames, entry counts and
   sizes are shown before anything is copied, and the local files are never
   modified.
5. Activating `memory.provider: pubky`.

## Commands

```text
hermes pubky status                  # grant, cache, revision, pending writes
hermes pubky status --offline        # skip the homeserver check
hermes pubky login                   # authorize this machine
hermes pubky logout                  # revoke the grant, delete the local secret
hermes pubky sync                    # reconcile pending writes
hermes pubky sync --prefer remote    # resolve a conflict, keeping the remote
hermes pubky sync --prefer local     # resolve a conflict, keeping local writes
hermes pubky base set <pubky-url>    # pin a public base context
hermes pubky base refresh            # re-approve it after its content changed
hermes pubky base clear              # unpin it
```

## Requested capabilities

Setup requests exactly one capability:

```text
/priv/hermes.pubky.app/v1/profiles/:rw
```

Read and write, confined to this plugin's own private profile directory. It
cannot read your other apps' data, cannot write anywhere else, and does not
hold the root capability. The plugin also refuses locally — before any request
is sent — to touch a path outside that directory.

`hermes pubky logout` revokes the grant through the session's own
`DELETE /auth/grant/session`, which a scoped grant is permitted to call.

## What the homeserver operator can see

`/priv` is **access-controlled, not encrypted**. Your homeserver operator can
read the contents of your private profile document. If you self-host, that is
you. If you do not, treat the portable overlay as visible to whoever runs your
homeserver, and keep genuinely sensitive facts in your local `USER.md`
instead — the plugin never uploads local files unless you explicitly import
them during setup.

Never stored in either document: credentials, conversations, or tool results.

## Offline behavior

The plugin is designed to be used on a laptop that is frequently offline.

- **Startup** reads the profile-scoped cache first, then refreshes from the
  homeserver with a five-second budget. If the network is slow or down, Hermes
  starts on the cache and the refresh finishes in the background.
- **Memory writes** are appended to a local JSONL outbox and mirrored to the
  homeserver asynchronously. They never block a turn.
- **Pending writes survive restarts** and retry with exponential backoff capped
  at 60 seconds.
- The injected prompt block says when it is working from a stale cache.

## Conflict recovery

v0.1 supports **one active writer per profile**. Reading the same profile from
several machines is fine; writing from two at once is not supported.

If the remote profile's revision changes while local writes are still pending,
automatic syncing stops and the plugin asks you to choose:

```bash
hermes pubky sync --prefer remote   # keep the remote profile, drop local writes
hermes pubky sync --prefer local    # keep this machine's profile, drop the remote
```

The two are symmetric: whichever side you drop is written to
`$HERMES_HOME/pubky/<profile-id>/backups/` first, as JSON you can read and
copy from. `--prefer remote` backs up both the cached local profile and the
queued writes; `--prefer local` backs up the remote profile. A base context
pinned only on the remote is carried over rather than lost.

## Base context pinning

A base context is pinned by the SHA-256 of its **raw bytes** at the moment you
approve it. If the author later changes the document, the plugin keeps using
the approved copy and logs a warning; the new content is only adopted after you
review it with `hermes pubky base refresh`. This means someone whose context
you follow cannot silently change your agent's instructions.

## Storage schema

### Public base context

```text
pubky://<author>/pub/hermes.pubky.app/v1/contexts/<context-id>.json
```

```json
{
  "schemaVersion": 1,
  "id": "researcher",
  "name": "Researcher",
  "description": "Research-oriented agent instructions",
  "instructions": "Markdown instructions"
}
```

Must be under `/pub/` and end in `.json`. v0.1 reads public contexts; it does
not publish them.

### Private profile

```text
/priv/hermes.pubky.app/v1/profiles/<profile-id>.json
```

```json
{
  "schemaVersion": 1,
  "profileId": "default",
  "baseContext": {
    "url": "pubky://.../context.json",
    "sha256": "approved-raw-content-hash"
  },
  "user": ["portable user fact"],
  "memory": ["portable agent memory"],
  "revision": 1,
  "updatedAt": "RFC3339 timestamp"
}
```

### Local files

```text
$HERMES_HOME/.env                                 HERMES_PUBKY_GRANT_SECRET (0600)
$HERMES_HOME/pubky/<profile-id>/profile.json      cached private profile
$HERMES_HOME/pubky/<profile-id>/context.json      approved base context, raw bytes
$HERMES_HOME/pubky/<profile-id>/context.meta.json its URL, hash and approval time
$HERMES_HOME/pubky/<profile-id>/outbox.jsonl      writes not yet mirrored
$HERMES_HOME/pubky/<profile-id>/state.json        last synced revision, conflict flag
$HERMES_HOME/pubky/<profile-id>/backups/          discarded sides of conflicts
```

All of it lives under `HERMES_HOME`, so `hermes backup` already captures it.

## Limits

Documents are capped at 64 KiB, entries at 4,000 characters, and each of
`user` / `memory` at 200 entries. Downloads are cut off mid-stream if a server
exceeds the cap, whatever its `Content-Length` says.

## Not in v0.1

Publishing public contexts, semantic retrieval, chat/session archives,
credential storage, Windows wheels, and multi-writer merge. The provider
exposes no tools and performs no recall — it is a context provider, not a
semantic-memory backend.

## Development

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python maturin pytest pyyaml
PYO3_PYTHON="$PWD/.venv/bin/python" .venv/bin/maturin develop

.venv/bin/pytest        # Python tests — no network, no homeserver
cargo test --lib        # Rust unit tests
```

The end-to-end suite runs against a real Pubky v0.11 testnet, which needs a
PostgreSQL for the homeserver and the well-known testnet ports free:

```bash
docker run -d --name hermes-pubky-pg \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_pass -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18-alpine

export TEST_PUBKY_CONNECTION_STRING="postgres://test_user:test_pass@localhost:5432/postgres?pubky-test=true"
cargo test --test e2e -- --ignored --test-threads=1
```

They are `#[ignore]`d and single-threaded because the static testnet binds
fixed ports — stop any `testnet_fixture` before running them.

To exercise the Hermes-facing flows by hand, start a testnet with a
pre-authorized grant and drive the real CLI against it:

```bash
cargo run --example testnet_fixture          # prints the grant secret as JSON
```

```bash
export HERMES_PUBKY_TESTNET=1
export HERMES_PUBKY_GRANT_SECRET="<grant_secret from the JSON>"
hermes-pubky install && hermes memory setup pubky
```

There is also a scripted version of that check:

```bash
HERMES_HOME=/tmp/hermes-home python scripts/check_hermes_integration.py
```

The Pubky Rust SDK is pinned to the v0.11.0 commit
`6a14bdb8fa2e30ef4e4b241fcdd3992c453d2378` and `Cargo.lock` is committed.

## License

MIT
