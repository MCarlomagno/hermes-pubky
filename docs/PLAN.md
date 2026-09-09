# Homeserver-backed agent storage

Status: historical discussion notes, superseded on 2026-09-09 by [IMPLEMENTATION-PLAN.md](./IMPLEMENTATION-PLAN.md). Implement from that document; the open questions and recommendations below are retained only as discussion history.

## Objective

Run Hermes locally while the homeserver holds the authoritative saved state of the user's agent. After authorizing a new computer, the user should recover that state without manually copying a Hermes profile from the previous computer.

## Existing implementation

The current provider injects a public base context and private user/memory lists into the prompt. Both remote documents are JSON. Local Hermes context files remain independent and take precedence. Conversations, skills, configuration, and generated files are outside the existing storage model.

Relevant starting points are `python/hermes_pubky/provider.py`, `schema.py`, `paths.py`, and `sync.py`; the existing behavior is described in `README.md` and `docs/ARCHITECTURE.md`.

## Agreed direction

- The homeserver holds the authoritative saved agent state.
- Hermes runs locally and accesses that state through the integration.
- Store Markdown and other assets as files; JSON may describe the collection and its references.
- Separate reusable public template content from personal private content.
- Keep this work in the separate Hermes integration repository.

## Decisions to resolve in order

1. Recovery scope: which information and assets must survive moving to a fresh computer?
2. Agent boundaries: how agents, conversations, projects, and public templates relate.
3. Runtime integration: how the selected Hermes version reads and updates that state, including anything beyond memory-provider hooks.
4. Storage behavior: which files are materialized locally, which are fetched on demand, and what counts as successfully saved.
5. Device handoff and offline work: unsaved changes, interrupted uploads, concurrent writers, and recovery.
6. Private access: user expectations for credentials, device authorization, and homeserver operator visibility.
7. Public templates: adoption, updates, ownership of edits, and continued availability of adopted content.
8. Migration: treatment of existing Hermes files and the current JSON profile format.
9. Delivery: implementation slices, supported versions, acceptance tests, and release scope.

## Planning constraints

Definitions belong in `CONTEXT.md`; implementation decisions and acceptance criteria belong here. The preceding proposal is not evidence that Hermes supports a particular integration point: verify the relevant source before selecting it.

A local cache can be reconstructed only when its contents are safely stored remotely. An offline outbox containing unsynchronized changes is durable local state and must not be described as disposable.

A content hash can detect a changed template but cannot recover missing bytes. Any recovery promise that includes public templates must account for their author's homeserver being unavailable or their files being removed.

## Capacity considerations

Verified against Pubky v0.11.0: the filesystem backend saves file contents on disk, and tenant GET/PUT handlers stream file transfers. Stored agent data therefore does not need to remain resident in server RAM. Actual memory usage still depends on the storage backend, concurrent transfers, database usage, and caching; no capacity benchmark has been run.

Sources: [storage configuration](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/config.sample.toml), [read handler](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/src/client_server/routes/tenants/read.rs), [write handler](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/src/client_server/routes/tenants/write.rs).

Recommended design, pending scope agreement:

- Fetch core instructions and the active conversation for startup; fetch other documents when needed.
- Synchronize changed files and bounded conversation segments instead of repeatedly uploading an entire growing archive. Streaming a PUT is not a server-side append API.
- Keep document storage separate from the text selected for the model's current context.
- Surface storage usage and quota failures without discarding unsynchronized work.
- Define the managed workspace and large-file policy explicitly; model weights, dependency directories, and build caches should not be uploaded by default.
- Validate peak memory and transfer volume with a representative workload once the storage adapter exists. File counts, document sizes, and sync frequency remain planning inputs.
