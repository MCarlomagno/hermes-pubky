# Architecture

## Why the code is split the way it is

The Rust extension is deliberately thin. It holds three things that are hard
to get right in Python and cheap to get right once in Rust:

1. **Pubky protocol access** — the grant auth flow, session restore, and the
   storage verbs, all through the SDK's own `Pubky` facade rather than
   hand-built tokens or transport URLs.
2. **The path policy** (`src/urls.rs`) — parsing is delegated to the SDK's
   `PubkyResource`, which normalizes and rejects traversal; on top of that the
   plugin enforces that public contexts live under `/pub/**.json` and that
   private writes cannot leave `/priv/hermes.pubky.app/v1/profiles/`.
3. **The download cap** (`src/http.rs`) — bodies are read through a streaming
   cap, so a server that lies about (or omits) `Content-Length` still cannot
   exhaust memory.

Everything else — what to store, when to sync, what to inject — is Python, so
it can be unit-tested without a homeserver or a compiled extension.

The `ops` submodules in `src/auth.rs` and `src/session.rs` hold plain async
functions; the `#[pyclass]` wrappers only marshal arguments and release the
GIL. That split is what lets the e2e suite drive the real code paths against a
live testnet instead of testing the SDK by proxy.

## Data flow

```
                    ┌──────────────────────────────┐
   Hermes startup ─▶│ initialize()                 │
                    │  1. read cache  (instant)    │──▶ system_prompt_block()
                    │  2. refresh     (≤5s budget) │
                    └──────────────┬───────────────┘
                                   │
                            ┌──────▼───────┐
                            │  Syncer      │◀── outbox.jsonl
                            └──────┬───────┘
                                   │
   memory tool write ──▶ on_memory_write() ──▶ outbox ──▶ BackgroundSyncer
                                   │                       (backoff ≤60s)
                                   ▼
                          homeserver /priv profile
```

Startup never waits on the network beyond its budget. The cache is read first
and always; the refresh is a thread that Hermes stops *waiting* on after five
seconds but which still completes and lands in the cache for the next turn.

## Trust boundaries

| Source | Trust | Handling |
| --- | --- | --- |
| Local `USER.md` / `MEMORY.md` | trusted | read-only, never modified |
| Private profile (`/priv`) | semi-trusted | schema-validated, size-capped; the homeserver operator can read it |
| Public base context (`/pub`) | untrusted | schema-validated, size-capped, hash-pinned, and labelled in the prompt as user-provided rather than system-level |
| Grant secret | secret | `.env` at 0600, redacted from all logs, never uploaded |

The base context is third-party content that ends up in a system prompt. That
cannot be sanitized into safety, so it is handled two other ways: the user has
to approve it explicitly, and it is pinned by the hash of its raw bytes so the
author cannot change it afterwards without a fresh, explicit re-approval.

## Why one writer per profile

v0.1 stores the overlay as two ordered lists in a single document. A real
multi-writer merge needs per-entry identity and causality (a CRDT, or
server-side conditional writes); guessing at a merge would silently lose
entries. Instead the plugin detects the case precisely — the remote revision
moved while local writes were pending — stops, backs up both sides, and asks.

## Why a plugin shim instead of an entry point

Hermes 0.19 discovers memory providers by scanning `$HERMES_HOME/plugins/` for
directories containing an `__init__.py`. Its `hermes_agent.plugins` entry-point
group exists but routes through a `PluginContext` that has no
`register_memory_provider`, so a pip install alone cannot deliver a memory
provider. `hermes-pubky install` writes three small files that import the
pip-installed package; upgrading the package needs no shim change.
