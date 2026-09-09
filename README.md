# hermes-pubky

Run a [Hermes](https://github.com/NousResearch/hermes) agent whose saved state
lives on your own [Pubky](https://github.com/pubky/pubky-core) homeserver. The
laptop becomes a working copy, but the agent belongs to you.

## Why

Hermes keeps everything it knows in one directory on one machine: `SOUL.md`,
your memories, its learned skills, your conversations. New laptop, fresh
container, throwaway VM, and the agent starts from nothing. The usual
alternative is a memory SaaS, which makes your agent portable by moving it into
someone else's account.

This is a third option. The homeserver holds the authoritative copy of the
agent, a local Hermes reconstructs a working copy, runs it, and saves changes
back. What travels:

- instructions (`SOUL.md`), user and agent memories
- learned skills and their assets
- allowlisted settings
- the conversation database, including tool calls, compaction and rewind history
- a designated workspace: notes, references, outputs

What stays on the machine: API keys, OAuth sessions, the Pubky grant itself, and
anything else machine-specific.

## Install

```bash
uv pip install hermes-pubky==0.2.0 "hermes-agent==0.19.0"
hermes-pubky agent init default
hermes-pubky run default
```

Needs Python 3.11–3.13, Hermes 0.19.0 exactly, and a Pubky homeserver on v0.11
or later. `agent init` asks Pubky Ring to authorize one scoped capability, then
publishes the agent's first checkpoint.

On a second computer:

```bash
uv pip install hermes-pubky==0.2.0 "hermes-agent==0.19.0"
hermes-pubky agent attach pubky://<owner>/priv/hermes.pubky.app/v2/agents/default/head.json
hermes-pubky run default
```

Nothing is copied between machines. Instructions, memories, skills, settings and
conversations come back from the homeserver; workspace documents are fetched
when the agent asks for them.

## Commands

```text
hermes-pubky run <id> [--resume SESSION] [--offline] [--query TEXT]

hermes-pubky agent init <id> [--from-hermes-home PATH] [--workspace PATH]
hermes-pubky agent attach <pubky-uri>
hermes-pubky agent list
hermes-pubky agent status <id> [--json] [--offline]
hermes-pubky agent login <id>
hermes-pubky agent logout <id>
hermes-pubky agent sync <id> [--prefer local|remote]
hermes-pubky agent files <id> list|fetch|import|remove
hermes-pubky agent history <id>
hermes-pubky agent restore <id> <snapshot-id>

hermes-pubky template init|inspect|publish <directory>
hermes-pubky template adopt <agent-id> <pubky-uri>
hermes-pubky template update <agent-id>
```

Exit codes: 0 done, 1 usage, 2 saved locally but not yet on the homeserver,
3 conflict, 4 authorization needed, 5 quota, 6 integrity or unsupported runtime.

## What it can reach

`agent init` requests one capability:

```text
/priv/hermes.pubky.app/v2/agents/<id>/:rw
```

Read and write inside that agent's own directory. No root capability, no access
to your other apps. The launcher also refuses paths outside that directory
locally, before a request leaves the process, and keeps the grant out of the
Hermes child's environment entirely.

Publishing a template needs its own separate capability under `/pub/`. An
agent's grant can never publish.

## Read this before you store anything

Your homeserver operator can read your agent. `/priv` is access-controlled, not
encrypted. If you self-host, that operator is you, otherwise assume whoever runs
your homeserver can read your instructions, memories and conversations.
Credentials are never uploaded, but a conversation can contain anything you or a
tool put in it.

One writer at a time. Read from as many machines as you like, but stop and sync
on one before writing from another. If the remote checkpoint moves while you
have unsaved work, syncing stops and asks you to choose with `--prefer`;
whichever side you drop is preserved under `recovery/` first.

Offline works. Startup reads the local working copy, then refreshes with a
five-second budget. Changes are sealed into local checkpoints that survive
restarts and retry with backoff capped at 60 seconds. A run that could not reach
the homeserver exits 2 and says `saved locally; not yet saved to homeserver`.

A cold attach downloads the conversation database in full. It is streamed and
reassembled on disk, never held in memory, but a large history takes time.
Workspace documents stay remote until requested.

History is kept. Older checkpoints remain recoverable and count against your
homeserver quota. Removing a file drops it from the current inventory, not from
history; this is not secure erasure.

Not in 0.2: multi-device merge, client-side encryption, portable credentials,
semantic retrieval, Windows wheels, and harnesses other than Hermes 0.19.0.

## How it fits together

```text
hermes-pubky run <id>
  -> read and verify the remote checkpoint
  -> materialize a dedicated HERMES_HOME and workspace
  -> start Hermes as a child, with HERMES_HOME set before it imports
       -> the in-process plugin reports changes and serves two workspace tools
  -> the supervisor owns the lock, the journal and every network call
  -> final capture and sync after the child exits
```

Storage is content-addressed. A checkpoint is an immutable snapshot document
naming immutable objects; only `head.json` is mutable, and it moves last. The
homeserver offers no conditional write, so this is ordering, not a lock:
concurrent writers remain unsupported and are detected rather than merged.

```text
/priv/hermes.pubky.app/v2/agents/<id>/{head,snapshots/*,objects/*}.json
/pub/hermes.pubky.app/v2/templates/<id>/{head,snapshots/*,objects/*}.json
```

Markdown is stored as Markdown and JSON as JSON, so what is on your homeserver
is readable there. Files over 1 MiB, and the database always, are split into
1 MiB chunks.

## Development

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python maturin pytest "hermes-agent==0.19.0"
PYO3_PYTHON="$PWD/.venv/bin/python" .venv/bin/maturin develop

.venv/bin/pytest        # Python tests; the Hermes ones skip if it is absent
cargo test --lib        # Rust unit tests
```

The end-to-end and acceptance suites need PostgreSQL for the homeserver and the
well-known testnet ports free:

```bash
docker run -d --name hermes-pubky-pg \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_pass -e POSTGRES_DB=postgres \
  -p 5432:5432 postgres:18-alpine

export TEST_PUBKY_CONNECTION_STRING="postgres://test_user:test_pass@localhost:5432/postgres?pubky-test=true"
cargo test --test v2 -- --ignored --test-threads=1
```

A real managed Hermes turn, against a local fake model so no credentials are
needed:

```bash
python scripts/check_managed_hermes_integration.py
```

The full recovery scenario, one agent across two machines:

```bash
cargo run --example testnet_fixture > /tmp/fixture.json &
HERMES_PUBKY_TESTNET=1 python scripts/check_managed_handoff.py --fixture /tmp/fixture.json
```

Pinned upstream versions, changed only as a deliberate compatibility decision
with tests to match:

| | |
| --- | --- |
| Hermes | `hermes-agent==0.19.0`, conversation schema 22 |
| Pubky SDK | v0.11.0 commit `6a14bdb8fa2e30ef4e4b241fcdd3992c453d2378` |

`Cargo.lock` is committed. `tests/test_hermes_contract.py` holds the upstream
contract as executable tests and fails if the installed package drifts from it.

## License

MIT
