# hermes-pubky

Keep a [Hermes](https://github.com/NousResearch/hermes) agent's context on your own
[Pubky](https://github.com/pubky/pubky-core) homeserver instead of on one laptop.

> **0.1, alpha.** A 0.2 rewrite is specified in the
> [implementation plan](docs/IMPLEMENTATION-PLAN.md) and the pinned
> [Hermes integration record](docs/HERMES-COMPATIBILITY.md). It replaces 0.1
> outright, with no migration path. Everything below describes 0.1, which is
> what ships today.

## Why

Hermes stores what it knows about you in `SOUL.md`, `USER.md` and `MEMORY.md` on
one machine. New laptop, fresh container, throwaway VM: the agent starts from
nothing. The usual alternative is a memory SaaS, which makes your agent portable
by moving it into someone else's account.

This adds a third option. A memory provider named `pubky` keeps two things in
documents addressed by your own keypair:

- a **private overlay** of your user facts and agent memory, updated whenever
  Hermes writes memory
- a **public base context**, shareable agent instructions at a `pubky://` URL,
  pinned by hash so its author cannot change your agent's instructions after you
  approve them

Your local `SOUL.md`, `USER.md` and `MEMORY.md` are read, never written, and
still take precedence over anything the plugin injects.

## Install

```bash
cd ~/.hermes/hermes-agent
uv pip install hermes-pubky==0.1.0
hermes-pubky install
hermes memory setup pubky
```

Needs Python 3.11–3.13, Hermes 0.19, and a Pubky homeserver on v0.11 or later.

`hermes-pubky install` writes a three-file shim to `$HERMES_HOME/plugins/pubky/`.
Hermes finds memory providers by scanning that directory rather than through
Python entry points, so a pip install alone leaves the provider invisible. The
shim imports the installed package, so upgrading is `uv pip install -U
hermes-pubky`.

Setup asks for a profile id, opens a [Pubky Ring](https://pubky.org)
authorization URL, then offers to pin a public base context and to import your
existing `USER.md` / `MEMORY.md`. It prints filenames, entry counts and sizes
before copying anything, and never modifies the local files. The grant secret
goes to your profile-scoped `.env` at mode `0600` and stays on the machine.

## Commands

```text
hermes pubky status [--offline]     grant, cache, revision, pending writes
hermes pubky login                  authorize this machine
hermes pubky logout                 revoke the grant, delete the local secret
hermes pubky sync                   reconcile pending writes
hermes pubky sync --prefer remote   on conflict, keep the remote profile
hermes pubky sync --prefer local    on conflict, keep this machine's
hermes pubky base set <pubky-url>   pin a public base context
hermes pubky base refresh           re-approve it after the author changed it
hermes pubky base clear             unpin it
```

## What it can reach

Setup requests one capability:

```text
/priv/hermes.pubky.app/v1/profiles/:rw
```

Read and write inside its own directory. It holds no root capability and cannot
read your other apps' data. The plugin also rejects paths outside that directory
locally, before a request leaves the process. `logout` revokes the grant at the
homeserver through the session's own `DELETE /auth/grant/session`.

## Read this before you store anything

Your homeserver operator can read the overlay. `/priv` is access-controlled, not
encrypted. If you self-host, that operator is you; otherwise assume whoever runs
your homeserver can read it, and keep sensitive facts in your local `USER.md`.
Credentials, conversations and tool results are never stored.

One writer per profile. Read from as many machines as you like. If the remote
revision moves while local writes are pending, syncing stops and asks you to
pick a side with `--prefer`. Whichever side you drop is written to
`$HERMES_HOME/pubky/<profile-id>/backups/` first.

Offline is the normal case. Startup reads the local cache, then refreshes with a
five-second budget. Memory writes queue in a local outbox, survive restarts, and
retry with backoff capped at 60 seconds. The injected prompt block says when it
is working from a stale cache.

Small by design. 64 KiB per document, 4,000 characters per entry, 200 entries
each for `user` and `memory`. A download is cut off mid-stream past the cap
whatever the server claims in `Content-Length`.

Not in 0.1: publishing public contexts, semantic retrieval, session archives,
credential storage, Windows wheels, and multi-writer merge. The provider exposes
no tools and performs no recall.

The storage schema is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the
threat model is in [docs/SECURITY.md](docs/SECURITY.md).

## Development

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python maturin pytest pyyaml
PYO3_PYTHON="$PWD/.venv/bin/python" .venv/bin/maturin develop

.venv/bin/pytest        # Python tests, no network or homeserver needed
cargo test --lib        # Rust unit tests
```

The end-to-end suite runs against a real Pubky v0.11 testnet, so it needs
PostgreSQL for the homeserver and the well-known testnet ports free:

```bash
docker run -d --name hermes-pubky-pg \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_pass -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18-alpine

export TEST_PUBKY_CONNECTION_STRING="postgres://test_user:test_pass@localhost:5432/postgres?pubky-test=true"
cargo test --test e2e -- --ignored --test-threads=1
```

Those tests are `#[ignore]`d and single-threaded because the static testnet binds
fixed ports. Stop any running `testnet_fixture` first.

To drive the real CLI by hand, start a testnet that prints a pre-authorized
grant:

```bash
cargo run --example testnet_fixture
```

```bash
export HERMES_PUBKY_TESTNET=1
export HERMES_PUBKY_GRANT_SECRET="<grant_secret from the JSON>"
hermes-pubky install && hermes memory setup pubky
```

A scripted version of the same check:

```bash
HERMES_HOME=/tmp/hermes-home python scripts/check_hermes_integration.py
```

The Pubky Rust SDK is pinned to the v0.11.0 commit
`6a14bdb8fa2e30ef4e4b241fcdd3992c453d2378`, and `Cargo.lock` is committed.

## License

MIT
