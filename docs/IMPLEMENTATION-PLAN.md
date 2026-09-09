# Implementation plan: homeserver-backed Hermes agents

Status: implementation handoff, 2026-09-09. Target package release: **0.2.0 alpha**. This specifies future work; it does not describe already implemented behavior.

## 1. Instructions for the implementing agent

Repository: `MCarlomagno/hermes-pubky-memory`. Its inspected local checkout is `/Users/marcos/repos/hermes-pubky`, at commit `84c2953`. There is no local `/Users/marcos/repos/hermes-pubky-memory` directory. Do not create a second implementation repository because these names differ.

Read this document, `CONTEXT.md`, `docs/HERMES-COMPATIBILITY.md`, and the existing source. Implement the ordered slices below; satisfy their acceptance criteria before moving on. This is a **clean replacement**, not an upgrade path for 0.1. Reuse useful code, but freely replace or remove old modules, APIs, commands, schemas, and obsolete tests. A full rewrite inside this repository is allowed. Do not declare completion based only on schemas and mocked tests.

Planning baseline: **259 Python tests passed**. This is historical evidence about 0.1, not a required test count or a requirement to preserve its behavior. Retire tests for removed features; retain or replace tests of relevant correctness and security properties. Rust, testnet, wheel, and live Hermes execution were not rerun during planning. Hermes integration facts were checked against the published **hermes-agent 0.19.0 wheel**, which this repository's CI installs. Do not silently substitute upstream `main` or advertise a broader version range. Compatibility with that upstream runtime is still required; compatibility with this project's old implementation is not.

## 2. Outcome and decisions

The homeserver holds the authoritative saved agent state. A local Hermes installation reconstructs a working copy, runs the agent, and saves changes back. Files remain real Markdown, JSON, documents, and a conversation database; manifests describe their names, hashes, and locations.

Acceptance scenario:

1. On A, create a managed agent, customize its instructions, remember a preference, install a skill, and have a conversation that reads reference files and creates a report in the managed workspace.
2. Exit Hermes, synchronize successfully, and record the checkpoint ID.
3. On B, without copying any files from A, install the supported Hermes/plugin versions, authorize the same Pubky identity, and attach the agent address.
4. Recover instructions, memories, skills, settings, conversation history, and the workspace catalogue. Fetch the report and references and verify their hashes.
5. Resume the conversation, complete another turn, and sync. A must then recover the new state.

Selected defaults resolve the remaining design choices:

- One implementation, one provider mode, one supported storage schema, and one CLI. No 0.1/v1 migration, compatibility readers, old command aliases, dual writes, fallback provider, or deprecation period. See [the clean-replacement decision](./adr/0003-clean-replacement.md).
- Hermes **0.19.0**, Python **3.11–3.13**, macOS and Linux first. Other Hermes versions require explicit adapter validation.
- **One active writer per agent**, with sequential device handoff. No automatic multi-device merge.
- **Launcher plus plugin**: the launcher owns remote I/O and prepares a dedicated `HERMES_HOME` before Hermes starts; the plugin supplies lifecycle notifications and workspace tools.
- Full supported saved state includes conversations and a designated workspace. This supersedes the memory-overlay-only design.
- Conversations use a **consistent SQLite snapshot**, preserving Hermes-specific history state. Core files and the database are restored eagerly; workspace documents are fetched on demand. Lazy session/database loading is deferred.
- Public templates are **copied and pinned** on adoption. Include an explicit publish workflow, extending the earlier consume-only proposal. Normal private sync never publishes content.
- Pubky grants, model/tool credentials, OAuth state, and machine-specific settings remain local. Secret portability and client-side encryption are deferred.
- Pending local changes are durable state. A working copy is replaceable only after its checkpoint is acknowledged remotely.
- Use Pubky v0.11 storage/auth as shipped. No homeserver changes, filesystem mount, hosted chat proxy, or permanently running daemon.

| Portable state | Outside this release's portability promise |
| --- | --- |
| `SOUL.md`, `memories/USER.md`, `memories/MEMORY.md`, workspace `AGENTS.md` | OS configuration and files elsewhere on the computer |
| Skill definitions and their regular-file assets | Installed dependencies, build caches, model weights |
| Allowlisted portable settings | API keys, OAuth sessions, grants, raw auth/config secrets |
| Conversation database, including tool calls/results and compaction/rewind state | Running processes, active terminals, gateway/channel ownership, cron jobs |
| Designated workspace files and template copies | Automatic following of template updates; arbitrary local paths mentioned in chat |

Each agent has one workspace; projects can be subdirectories. The workspace is a storage boundary, not an execution sandbox. Existing Hermes tool approvals still apply. Compatibility with OpenClaw requires a future adapter.

## 3. Existing implementation: reusable evidence, not constraints

Use the current repository as a source of verified SDK integration and build knowledge. Keep the Python package/native SDK bridge as the selected implementation stack, but do not build a second product beside the old overlay. Replace the package internals in place. Reuse a function only when it directly satisfies a requirement below without preserving obsolete behavior.

| Existing area | Replacement decision |
| --- | --- |
| `src/auth.rs`, `runtime.rs`, `errors.rs` | Candidate reuse for SDK auth/session/runtime handling; replace signatures and errors as needed for scoped transfers |
| `src/urls.rs` | Replace profile-specific parsing with strict `AgentRoot`/`TemplateRoot` policies; no old URL reader |
| `src/session.rs`, `http.rs` | Replace profile-JSON methods and global 64 KiB assumptions with bounded metadata and streaming objects |
| `remote.py` | Replace with `AgentRemote`/`PublicTemplateRemote`; keep only useful injectable transport patterns |
| `schema.py`, `store.py`, `outbox.py`, `sync.py` | Remove the overlay schemas, JSONL outbox, and old sync path; replace with typed manifests and a durable checkpoint journal |
| `provider.py`, `plugin_template/` | One managed provider and a generated Hermes discovery bridge; no mode router or duplicate memory injection |
| `paths.py`, `config.py`, `setup_flow.py` | Replace with dedicated agent layout, portable/local configuration separation, and fresh setup |
| `__main__.py`, `cli.py` | Replace old installer/command semantics with the CLI in section 10; no deprecated aliases |
| Tests, e2e, scripts, workflows | Reuse valid protocol/security fixtures and build mechanics; rewrite assertions/smoke checks for the new product |

Do not preserve old exports such as `REQUIRED_CAPABILITY` or `MAX_DOCUMENT_BYTES` merely to satisfy old smoke tests. Update those tests to verify the new interface. Do not retain dead overlay modules behind flags. Removing obsolete repository code is in scope; deleting users' old local profiles, grants, or remote documents is not. Leave old data unmodified and unsupported, without discovering or importing it automatically.

Required corrections:

- `paths.py` uses root-level memory filenames. Hermes 0.19.0 uses `memories/USER.md` and `memories/MEMORY.md`.
- `provider.initialize()` discards session ID; managed mode must track session switches.
- Current `sync_turn()` does nothing and the provider exposes no tools.
- Legacy outbox overflow drops operations; malformed lines are skipped. These semantics cannot be used when local changes are the only surviving copy.
- `threading.Lock` is not a cross-process lock.
- Reading a revision and then PUTting a new revision is not compare-and-swap. Concurrent-writer safety is not supplied by the current code or the HS storage API.

## 4. Runtime and local layout

The replacement `hermes-pubky` console script will supervise a child Hermes process and synchronize only for that command's lifetime.

```text
hermes-pubky run <id>
  -> read/verify remote checkpoint
  -> prepare dedicated local profile
  -> start Hermes after setting HERMES_HOME
       -> managed provider emits local events and workspace requests
  -> supervisor captures checkpoints and communicates with Pubky
  -> final capture/sync after child exit
```

Default root: `Path.home() / '.hermes-pubky'`, overridable with `HERMES_PUBKY_HOME`. Do not change OS `HOME`. Never place this root inside the managed workspace.

```text
<root>/device.json
<root>/networks/<network-id>/<owner-z32>/agents/<agent-id>/
  connection.json                  # URI, adapter, local preferences
  credentials.env                  # scoped Pubky grant, 0600
  device-config.yaml               # local-only settings
  secrets.env                      # explicit model/tool credentials, local-only
  journal.sqlite3
  run.lock
  cache/head.json
  cache/snapshots/<snapshot-id>.json
  cache/objects/<digest>.<extension>
  pending/<checkpoint-id>/         # sealed, not-yet-acknowledged data
  recovery/<timestamp>-<id>/        # preserved conflict/crash candidates
  runtime/home/                    # dedicated child HERMES_HOME
    SOUL.md
    memories/USER.md
    memories/MEMORY.md
    skills/...
    config.yaml                    # generated effective configuration
    state.db                       # materialized conversation database
    plugins/pubky/...              # generated from this package
  runtime/workspace/
    AGENTS.md
    ...                            # materialized subset of workspace files
```

Scope local state by **network + owner + agent ID**. Initial network IDs are `mainnet` and `testnet`; different testnet instances must use separate management roots. Verify the grant owner matches the attached URI. Directories are 0700; private files 0600; explicitly executable managed regular files may be 0700. Do not materialize remote symlinks, hardlinks, devices, or sockets. Upload scanning uses an allowlist of sources, never a recursive walk of the management root.

Launch `[sys.executable, '-m', 'hermes_cli.main', ...]` with inherited terminal I/O and the managed workspace as `cwd`. Before any Hermes import, set child `HERMES_HOME`, `TERMINAL_CWD`, `HERMES_PUBKY_MANAGED=1`, and `HERMES_PUBKY_CONNECTION` to the verified connection-file path. The connection identifies the local journal. Keep the Pubky grant in the supervisor and remove `HERMES_PUBKY_GRANT_SECRET` from the child environment. This is responsibility separation, not isolation from arbitrary same-user shell execution.

## 5. Remote storage protocol

Use the new `/v2/` namespace and `schemaVersion: 2` below as fixed identifiers for this replacement. The version label separates its files from the old experiment; it does not imply a v1 reader, migration path, or version negotiation. Only this schema is supported. Do not inspect, rewrite, or delete older remote namespaces.

```text
/priv/hermes.pubky.app/v2/agents/<agent-id>/
  head.json
  snapshots/<snapshot-id>.json
  objects/<sha256>.md
  objects/<sha256>.json
  objects/<sha256>.bin
  objects/<sha256>.chunk

/pub/hermes.pubky.app/v2/templates/<template-id>/
  head.json
  snapshots/<snapshot-id>.json
  objects/...
```

Agent URI: `pubky://<owner>/priv/hermes.pubky.app/v2/agents/<id>/head.json`. Template URI: equivalent public template path. PKARR/DHT discovers the homeserver; it does not store or replicate agent files.

### 5.1 Serialization and validation

- Agent/template IDs: `^[a-z0-9][a-z0-9_-]{0,63}$`. Snapshot, checkpoint, and device IDs: UUID4 hex, 32 lowercase characters.
- Hashes: SHA-256 of exact bytes, 64 lowercase hex characters. A hash detects changed bytes; an HS operator able to replace manifests can replace their hashes too.
- Generated JSON: UTF-8, sorted keys, compact separators, `ensure_ascii=False`, `allow_nan=False`, exactly one final newline. Reject duplicate keys, nonfinite numbers, and unsupported schema versions when reading.
- Logical paths are relative POSIX paths, max 1,024 UTF-8 bytes. Reject absolute paths, empty/`.`/`..` segments, backslashes, NUL/control characters, and Unicode-normalization or case-fold collisions. Normalize new names to NFC; reject noncanonical remote names. Never silently rename collisions.
- Object references are generated relative paths of the form `objects/<digest>.<md|json|bin|chunk>` within the same root. No external URLs or traversal in object references. Validate the local parent chain against symlink escape.
- Validate that piece sizes sum to file size, each piece fits the object cap, each object filename agrees with its digest, and hashes/sizes agree when the same object is referenced repeatedly. Bound arrays and numeric fields before allocating buffers. Example angle-bracket hashes below are placeholders; fixtures must use actual computed hashes.

### 5.2 Head

```json
{
  "schemaVersion": 2,
  "kind": "agent-head",
  "agentId": "default",
  "snapshotId": "0123456789abcdef0123456789abcdef",
  "sha256": "<snapshot-json-hash>"
}
```

Only `head.json` is mutable. Integration-written objects and snapshots are immutable by convention. A path with different existing bytes is an integrity error. Template heads use `kind: template-head` and `templateId`.

### 5.3 Snapshot

```json
{
  "schemaVersion": 2,
  "kind": "agent-snapshot",
  "agentId": "default",
  "name": "My research assistant",
  "snapshotId": "0123456789abcdef0123456789abcdef",
  "parent": null,
  "createdAt": "2026-09-09T18:00:00Z",
  "deviceId": "fedcba9876543210fedcba9876543210",
  "runtime": {"name": "hermes", "version": "0.19.0", "adapter": "hermes-0.19-sqlite22-v1"},
  "lastSessionId": null,
  "template": null,
  "files": {
    "profile/SOUL.md": {
      "sha256": "<whole-file-hash>",
      "size": 123,
      "executable": false,
      "pieces": [{"object": "objects/<hash>.md", "sha256": "<hash>", "size": 123}]
    }
  }
}
```

`parent` is null initially, otherwise `{snapshotId, sha256}`. `template` is null or `{url, snapshotId, sha256, adoptedAt, managedPaths}`; `managedPaths` maps imported logical paths to their last-adopted hashes. Every adopted file is also represented by private objects and the private file inventory, so recovery does not depend on the author's HS remaining online.

| Logical path | Runtime destination | Encoding/capture |
| --- | --- | --- |
| `profile/SOUL.md` | `home/SOUL.md` | Exact Markdown bytes |
| `profile/memories/USER.md` | `home/memories/USER.md` | Exact Hermes-format bytes |
| `profile/memories/MEMORY.md` | `home/memories/MEMORY.md` | Exact Hermes-format bytes |
| `profile/skills/**` | `home/skills/**` | Regular files and explicit executable flag |
| `config/portable.json` | Generated `home/config.yaml` | Typed settings, never raw config copy |
| `conversations/state.sqlite3` | Adapter-restored `home/state.db` | Consistent snapshot, not live DB copy |
| `workspace/**` | `workspace/**` | User files, on-demand materialization |

`workspace/AGENTS.md` is startup-required. Absent optional memory files mean empty memory; a missing referenced object means unavailable/corrupt data. The snapshot inventory includes remote-only files that have never been fetched locally.

### 5.4 Chunking, limits, and retention

- Files up to 1 MiB use one raw object: `.md` for Markdown, `.json` for JSON, `.bin` otherwise. Meaningful filenames remain in the manifest. Markdown content is therefore stored as Markdown on the HS, not embedded in JSON strings.
- Larger files use fixed 1 MiB chunks, final chunk shorter, stored as `.chunk`. The database always uses chunks. Preserve ordered pieces; verify each piece and the assembled whole-file hash.
- Empty files: `size: 0`, empty-byte SHA-256, `pieces: []`.
- Initial application limits: head 4 KiB; snapshot 1 MiB; core Markdown 64 KiB/file; other workspace/skill files 64 MiB/file; DB 256 MiB; 5,000 logical files; 2 GiB logical bytes/agent. These are selected application limits, not HS quotas. Fail explicitly and retain local data when exceeded; never truncate.
- Stream hashing, assembly, and transfers. At most four transfers concurrently. Never load the whole archive/database into Python bytes.
- Local immutable-object cache target: 256 MiB. Evict only acknowledged reconstructible objects. Pending/recovery data and the working profile are not disposable cache and are excluded from this budget. SQLite staging temporarily needs additional disk space.
- Upload only changed/missing objects. Create no checkpoint when normalized content and relevant metadata are unchanged. An object name alone is not proof of remote integrity.
- No automatic remote garbage collection in 0.2. Older snapshots/objects remain recoverable and consume quota. Deleting a logical file removes it from the current inventory, not from history; do not claim secure erasure.

## 6. Durable capture and sync

### 6.1 Journal and local coordination

Use stdlib SQLite with WAL and `synchronous=FULL` for the new journal. Required tables:

- `checkpoints(id PRIMARY KEY, parent_snapshot_id, parent_hash, snapshot_path, snapshot_hash, state, created_at, last_error)`.
- `uploads(checkpoint_id, object_path, sha256, size, local_path, acknowledged, PRIMARY KEY(checkpoint_id, object_path))`.
- `materialized(logical_path PRIMARY KEY, base_hash, present, dirty, explicit_delete)`.
- `requests(id PRIMARY KEY, run_id, kind, payload_json, state, result_json, created_at)`.
- `settings(key PRIMARY KEY, value_json)` for acknowledged head, identity, generation counters, and unfinished-run status.

States: `staged -> uploading -> snapshot_written -> head_written -> acknowledged`, plus `blocked` and `conflict`. Retain staged bytes through errors. Corruption must not be interpreted as an empty queue or a new agent.

One POSIX `flock` protects a connection during a managed run or offline management operation. The supervisor owns it; child hooks use journal requests. A second local run fails. `agent sync` while a run is active queues a supervisor request rather than writing to Pubky itself.

### 6.2 Capture

1. Scan only allowlisted materialized paths. Retain descriptors for unchanged and remote-only files. Absence of a never-fetched file is not a deletion.
2. Stage stable copies. Compare opened-file metadata before/after reading and hash staged bytes; retry if a file changed. Reject links and unsupported file kinds.
3. Capture the DB using section 8. Check local generation before/after capture; if a foreground turn or tracked write intervened, keep the previous checkpoint and retry after the next safe boundary.
4. Fsync objects and the sealed manifest before committing journal references. Never queue a reference to an already-deleted temporary file.
5. Skip upload if normalized content/configuration and relevant metadata are unchanged. Wall-clock snapshot timestamps are not themselves a change trigger.

A background checkpoint is a recoverable observation of saved state, not a global transaction over arbitrary tool effects. Clean handoff requires child exit, final capture, and verified final sync. Background processes that continue editing files prevent a clean handoff until files stabilize.

### 6.3 Remote publication

1. Restore and validate the scoped grant and owner.
2. Read head; require it to equal the candidate's recorded parent ID/hash, or be absent for creation. Otherwise enter conflict.
3. Upload missing immutable objects. After uncertain outcomes, verify size/hash before treating an object as acknowledged.
4. Upload snapshot JSON and read back its exact digest.
5. Re-read head; if it moved, retain the candidate snapshot and enter conflict.
6. PUT head, read it back, and verify the expected ID/hash.
7. Only then acknowledge locally. Retire that checkpoint only; later changes stay dirty.

Objects-first/head-last prevents the active head from intentionally referencing incomplete uploads under the single-writer contract. It assumes no HS multi-file transaction, rename, append, or conditional PUT. Re-reading head does **not** eliminate simultaneous-writer races. Do not call this CAS or a distributed lock.

Retry connection/timeouts, 429, and retryable server errors with 2–60 second exponential backoff plus jitter; honor `Retry-After` where available. Auth/capability, quota, and validation errors stop automatic retries until corrected. Show affected paths and pending status; never drop writes or log secrets/document contents.

### 6.4 Offline, crash, and conflicts

- Fresh attach needs the network. Never create an empty substitute for an unavailable existing agent.
- A valid existing working copy can use `--offline`; show the last acknowledged checkpoint. Remote-only files cannot be fetched offline.
- Online startup has a five-second metadata-refresh budget. A valid local copy may continue stale on failure, with visible pending status. Fresh materialization and document transfers have separate progress/timeouts.
- After child exit, allow 15 seconds for final sync. If incomplete, preserve all pending data, print `saved locally; not yet saved to homeserver`, and use exit code 2 when Hermes otherwise succeeded.
- Apply that final-sync deadline to the entire operation, not separately to every object. Cancel/join transfer work before releasing the connection lock; do not leave a network-writing worker alive after the supervisor reports exit.
- On crash recovery, capture surviving dirty runtime state **before** replacing it from remote. Never overwrite dirty state during attach or startup.
- Do not evict pending data for cache limits. Local disk-full is a persistence failure, not successful sync.
- Require A to stop and sync before B writes. Changed-head detection freezes uploads and preserves both candidates.
- `agent sync --prefer remote|local` requires an idle runtime. First save a complete recovery candidate; then adopt remote or publish local based on the newly read remote head. Neither choice destroys the discarded side's recoverable objects.
- `agent history` uses explicit paginated snapshot listing; `agent restore` verifies an old snapshot and republishes it as a new checkpoint. Routine operation reads known paths and does not recursively poll HS storage. A future live remote subscription should use Pubky events.
- Concurrent writers remain unsupported. Immutable snapshots retain unexpected candidates; they do not guarantee automatic conflict detection in every head race or a merged latest state.

## 7. Managed provider and supervisor contract

Implement `PubkyMemoryProvider` as the sole provider, containing only the managed lifecycle bridge. The generated profile-local discovery shim imports this class; it exists because Hermes requires plugin discovery, not to support the old project. Require both the launch environment and a validated local connection. A downloaded config file alone must not activate arbitrary local paths. A direct/unmanaged invocation must report that the user should start through `hermes-pubky run`; never silently fall back to an overlay or remote prompt injection.

Provider contract:

- `is_available()` checks the managed connection, running supervisor marker, and local journal without network access. It does not require a grant in the child environment.
- `initialize(session_id, **kwargs)` retains the session ID, validates the supplied home against the connection, and records foreground/nonforeground context.
- `system_prompt_block()` supplies only a concise workspace/status notice and instructions for listing/fetching remote workspace files. Hermes already loads `SOUL.md`, memory files, and skill definitions; do not inject their contents again.
- `on_memory_write()` queues a dirty notification after a successful local write. Capture the actual Markdown file, not a reconstructed string-list overlay.
- `sync_turn(user_content, assistant_content, *, session_id='', messages=None)` requests capture of completed saved work. Do not serialize the two flattened strings as the whole conversation. Database snapshots supply the transcript.
- `on_session_switch(new_session_id, *, parent_session_id='', reset=False, rewound=False, **kwargs)` updates the current ID, marks conversation state dirty, and invalidates stale per-session state.
- `on_session_end()`, `on_pre_compress()`, and `shutdown()` enqueue capture hints; return an empty compression string. None performs remote I/O. Notifications are best-effort triggers; shutdown capture in the supervisor remains necessary.
- Nonprimary contexts do not independently schedule remote writes. The supervisor may still capture files legitimately changed by the parent run or its subagents within the managed profile. Do not claim this filters file writes by author.

The supervisor processes journal requests, periodically checks local dirty state while active, and owns all network calls. A two-second local scan is acceptable initially with stat caching; do not poll remote storage every two seconds. Serialize capture and publication. The lifecycle callback may run after another turn begins, so events are hints, not proof of a globally quiescent state. Final handoff requires process exit.

Expose exactly two initial model tools, through `get_tool_schemas()` and `handle_tool_call()`:

1. `pubky_file_list(path='', cursor=null, limit=100)`: list the logical workspace catalogue, including size, materialized/remote-only/dirty state, and relative path. Cap `limit` at 200. Read the cached manifest; no whole-tree remote listing.
2. `pubky_file_fetch(path)`: fetch one existing remote workspace file into its proper local path through a journal request. Return local path, size, hash, and status. Verify bytes before exposing the file. A request timeout returns `pending` and its request ID; do not repeat a completed download or overwrite a dirty file on retry.

The integration's file-fetch request has a stable UUID and is idempotent. The supervisor accepts only known request kinds and validates paths again. Process at most one mutation/capture coordinator at once, although object transfers inside it may be concurrent. On supervisor exit, outstanding requests become interrupted/pending with an actionable status. A restarted run may retry safely.

Existing Hermes filesystem tools edit fetched files and create outputs. A newly created local path colliding with a remote-only path is a conflict, not permission to overwrite the remote file. Fetch/compare or require an explicit local-vs-remote choice after preserving both versions. Do not create empty placeholder files for remote-only content: existing tools would mistake them for real data.

## 8. Hermes adapter and conversation recovery

Place all Hermes-specific behavior in `python/hermes_pubky/hermes_adapter.py` and keep the storage/sync modules independent of Hermes imports. The initial adapter ID is `hermes-0.19-sqlite22-v1`; its final `v1` identifies this adapter's first revision, not the old overlay protocol.

### 8.1 Verified filesystem mapping

- Identity: `<HERMES_HOME>/SOUL.md`.
- Memories: `<HERMES_HOME>/memories/{USER,MEMORY}.md`; Hermes entries are separated by the exact delimiter `\n§\n`. Preserve exact bytes on import and subsequent capture rather than repeatedly parsing/reformatting.
- Skills: `<HERMES_HOME>/skills/`.
- Configuration: `<HERMES_HOME>/config.yaml`.
- Conversation database: `<HERMES_HOME>/state.db`, schema version **22** in the inspected package.
- Workspace instructions: the managed working directory's `AGENTS.md`.

Skill directories may contain scripts/resources. Preserve regular-file contents and the explicit executable flag; do not install referenced packages or run scripts merely because a template was attached. Allow packaged Hermes skills to behave normally, but verify that the managed user skill resolves correctly when a bundled skill has the same name.

### 8.2 Why snapshots, not the current export/import helpers

`SessionDB.import_sessions()` skips existing IDs. It also reconstructs messages as active rows, so it is not a complete inverse for rewind/compaction history. Its import bounds are 5 MiB/session and 10,000 messages/session. Do not silently lose state, reactivate inactive messages, or add a second upsert implementation around this API.

Use SQLite's supported online backup mechanism to obtain a consistent database snapshot, including committed WAL data. Never copy only the live `state.db` file and never upload `state.db-wal`/`state.db-shm` independently. A snapshot must remain usable when the original machine is gone.

### 8.3 Capture algorithm

1. If no DB exists yet, initialize an empty DB using supported Hermes `SessionDB` in the dedicated profile or represent the optional DB as absent until the first session. Do not touch the user's ordinary profile.
2. Open the source read-only through `sqlite3`, take an online backup into a task-specific temporary DB, and close the source. Bound busy retries; defer capture while busy rather than copying inconsistent bytes.
3. Validate schema version and structural fingerprint against a fixture generated with Hermes 0.19.0. Use fixed table/column identifiers in adapter code. Never execute SQL provided by a downloaded manifest.
4. Normalize the temporary copy as described below. Use `PRAGMA secure_delete=ON` before clearing known runtime/configuration records. Compute a deterministic logical digest of the durable tables, ordered by primary key and excluding device/runtime-only fields. Reuse the prior DB descriptor if the logical digest is unchanged, even if SQLite header counters or layout bytes differ. Record `lastSessionId` only if that session exists in the captured DB; an empty newly opened conversation must not become an invalid resume target.
5. For changed durable data, close the normalized snapshot in DELETE journal mode so it is self-contained, run SQLite integrity/foreign-key checks, and chunk/hash that file. Do not `VACUUM` on each checkpoint: it may rearrange many pages and increase retransfers.
6. Persist the logical digest locally with the candidate. It is an optimization, not a replacement for verifying remote object/file hashes.

Preserve `sessions`, `messages`, and `session_model_usage` durable records, message IDs/order, tool-call IDs and payloads, reasoning fields, `api_content`, `active`/`compacted` flags, titles, archives, and compression/branch relationships. Preserve required schema and FTS structures. Do not synthesize a new conversation from user/assistant strings.

Normalize runtime-specific state in the snapshot only:

- Empty `gateway_routing`, `compression_locks`, `async_delegations`, and `state_meta`. Optional Telegram routing/binding tables, if present, must also be emptied. Unknown application tables require an explicit adapter decision rather than automatic upload.
- Clear session channel routing/ownership fields: `session_key`, `chat_id`, `chat_type`, `thread_id`, `display_name`, `origin_json`, and `profile_name`; reset `expiry_finalized` to 0.
- Clear `handoff_state`, `handoff_platform`, `handoff_error`, compression-failure cooldown/error, and reset the fallback streak. Do not replay delivery jobs or take over an old live channel on restore.
- Keep session model IDs, but clear opaque session `model_config`; regenerate runtime provider settings from portable config plus the new device's local configuration. Clear device-specific billing endpoint URLs while retaining historical usage totals.
- Convert structured `cwd` and `git_repo_root` fields inside the managed workspace to `pubky-workspace:/<relative-path>`. Outside-workspace roots become null and are reported in import/capture diagnostics. On restore, map only those recognized markers to the new workspace.
- Do not rewrite arbitrary text in messages, stored prompts, or historical tool arguments. They are records of what happened. The managed provider notice tells the resumed agent the current workspace path and that historical absolute paths may refer to a previous device.

Do not promise that no secret can occur in a transcript: users or tools may have placed sensitive text in it. Explicit credential files/configuration are excluded, but private conversations retain their content. `/priv` is operator-visible storage in this release.

### 8.4 Restore algorithm

1. Require the connection lock and an idle child. Preserve dirty local state before any replacement.
2. Download/verify every DB chunk, reassemble into a staging file, verify its whole-file hash, SQLite header, integrity, and known schema. Enforce the DB size cap before opening it. Disable extension loading; validate the expected schema before modification.
3. Apply only structured workspace-marker rebasing and runtime-state resets to the staging copy. Do not import into an already existing DB with duplicate IDs.
4. Materialize core files/skills/config into a staging profile. Validate all paths and content hashes. Refresh clean workspace files that were previously materialized when their remote descriptors changed; do not leave stale on-disk bytes for Hermes to read. Never-fetched files stay remote-only. Remote deletions remove only proven-clean tracked local files, retaining a recoverable previous generation.
5. Close all handles. Swap the **idle dedicated profile** to the validated generation using same-filesystem renames and a local install journal. Retain the previous generation until success. Never replace a database under a running Hermes process; never reuse its old WAL/SHM sidecars.
6. Start Hermes with the supported version and resume using its existing `--resume <id>` flow. Preserve history even if the selected session has no currently active tool process. Do not replay unfinished tool calls automatically through new integration code.

On a cold device, the database is downloaded in full, but streamed and reconstructed on disk. This can take time for a large archive. Do not claim that this release fetches only the selected conversation. Warm starts reuse verified unchanged chunks. Workspace documents remain lazy.

Known local attachments outside the managed workspace are not automatically portable. During import and status checks, report such references when identifiable; users must import wanted files into the workspace. Arbitrary filenames embedded in free text cannot be resolved reliably and are not a basis for uploading unrelated local files.

## 9. Portable configuration and startup rules

`config/portable.json` has this versioned structure:

```json
{
  "schemaVersion": 2,
  "model": "",
  "toolsets": ["hermes-cli"],
  "agent": {"max_turns": 90},
  "memory": {
    "memory_enabled": true,
    "user_profile_enabled": true,
    "memory_char_limit": 2200,
    "user_char_limit": 1375
  }
}
```

These keys form the initial allowlist. Unknown keys require a schema update. Model is a string of at most 1,024 characters; an empty identifier means use local/default selection. Toolsets are at most 100 strings of at most 128 characters each and must resolve to installed toolsets at launch. `max_turns` is an integer from 1 to 1,000; memory character limits are integers from 1 to 64,000; enable flags are booleans. Reject booleans where an integer is expected. Do not copy raw `providers`, `mcp_servers`, shell commands, credentials, arbitrary URLs, or the full `config.yaml` into this object.

Generate effective runtime configuration in this order: supported Hermes defaults -> portable allowlist -> explicit local device settings -> required integration settings. Required integration settings include `memory.provider: pubky`, managed mode, `terminal.backend: local`, and the actual managed workspace `terminal.cwd`. Disallow local/CLI overrides that redirect the managed home or workspace or launch worktree/gateway/cron modes. Local model endpoints/credentials may override a portable model selection; report unavailable tools/models rather than pretending the environment was reproduced.

The downloaded configuration must never disable existing permission gates or auto-approve tool execution. Keep approval settings local and use Hermes defaults where unspecified.

Changes made through a running Hermes UI to allowlisted settings are extracted at checkpoint time. Differences in device-only or required integration fields remain local. Keep the generated configuration separate from source portable settings so re-rendering does not accidentally upload secrets.

## 10. CLI and user-visible behavior

Replace the existing CLI with the commands below. Remove the old `install|uninstall|status` interface and `hermes pubky ...` integration; do not keep forwarding aliases. Profile-local plugin registration is automatic during preparation, so there is no separate install command. Agent operations live under `hermes-pubky agent`, with a top-level `run` command:

```text
hermes-pubky agent init <id> [--from-hermes-home PATH] [--workspace PATH]
hermes-pubky agent attach <private-agent-uri>
hermes-pubky agent list
hermes-pubky agent status <id> [--json]
hermes-pubky agent login <id>
hermes-pubky agent logout <id>
hermes-pubky run <id> [--resume SESSION_ID] [--offline] [--query TEXT]
hermes-pubky agent sync <id> [--prefer local|remote]
hermes-pubky agent files <id> list [PREFIX]
hermes-pubky agent files <id> fetch PATH
hermes-pubky agent files <id> fetch --all
hermes-pubky agent files <id> import LOCAL_PATH --to RELATIVE_PATH
hermes-pubky agent files <id> remove RELATIVE_PATH
hermes-pubky agent history <id>
hermes-pubky agent restore <id> <snapshot-id>
hermes-pubky template adopt <id> <public-template-uri>
hermes-pubky template update <id>
hermes-pubky template init DIRECTORY
hermes-pubky template inspect DIRECTORY [--json]
hermes-pubky template publish DIRECTORY --id TEMPLATE_ID [--confirm-public SHA256]
```

Commands resolve IDs within a selected local identity/network. If two connections have the same short ID, require `--owner <z32>` rather than selecting by recency. Attach saves the exact canonical URI. `agent list` initially lists known local connections; do not request broad private-directory permissions just to discover every remotely stored agent.

Init creates a new remote agent only after successful scoped auth. With no import arguments, create a fresh profile with starter instructions, empty memories/skills/workspace, and default portable settings; do not search ordinary Hermes homes or old plugin state. An explicit `--from-hermes-home`/`--workspace` requests the one-time import described in section 13. Preview selected paths, counts, sizes, and excluded categories before publication; cancellation/EOF means no import/publication. A provided workspace is **copied** into the managed workspace, not adopted in place. An existing remote ID means use attach or choose a new ID; never silently replace it.

Attach verifies head/snapshot and required files before declaring success. Dirty existing local state is preserved and reconciled, not reset. It asks the user to configure any missing model/tool credentials locally; no mnemonic/private-key entry.

Use existing SDK auth URL generation and Ring approval. Main grant: `/priv/hermes.pubky.app/v2/agents/<id>/:rw`. Grant scopes are checked by the server and by the integration. Wrong-owner and under-scoped sessions fail before writes. Logout revokes the scoped session, removes its local credential, and retains local state with that behavior stated explicitly.

Status must distinguish: `synced`, `dirty`, `syncing`, `offline`, `auth-required`, `quota-blocked`, `conflict`, and `corrupt`. Show checkpoint ID/time, pending bytes/files, materialized versus remote-only counts, and failed paths. File-size/usage reporting must distinguish logical active bytes from any measured retained history and from local staging bytes.

Exit codes: 0 completed; 1 invalid usage/general failure; 2 saved locally but remote sync incomplete; 3 conflict; 4 authentication/capability required; 5 quota/size limit; 6 integrity/unsupported schema or runtime. Preserve a nonzero Hermes child exit code and additionally report sync status; document possible overlap. Structured `--json` statuses must carry a stable error code rather than making callers parse prose.

Avoid a general `--` passthrough in 0.2: expose and translate the specific supported launch options above. Verify the pinned Hermes parser accepts their mappings in the compatibility test. Add additional options only with tests proving they cannot redirect the managed profile or workspace.

## 11. Workspace inclusion and deletion

Include regular files beneath the managed workspace and installed managed skills. Exclude directory segments `.git`, `.venv`, `node_modules`, `__pycache__`, `target`, `dist`, and `build`; basenames `.DS_Store`, `.env`, `credentials.env`, `secrets.env`, `auth.json`, `credentials.json`, `id_rsa`, and `id_ed25519`; names starting `.env.`; suffixes `.swp`, `.swo`, `.tmp`, and `~`; and unsupported file kinds. Detect PEM private-key headers in explicitly imported text key files and reject them as outside scope. This is an explicit default policy, not a claim to identify every possible secret.

For additional exclusions, define `.pubkyignore` as a deliberately small format: UTF-8, one exact workspace-relative path per line, or a directory prefix ending `/`; blank lines and lines starting `#` are ignored. Wildcards, negation, absolute paths, and traversal are rejected with line-numbered errors. Do not describe it as full gitignore compatibility. `.pubkyignore` itself is a tracked workspace file so the selected policy travels with the agent. Skills use the fixed exclusion list only in 0.2.

Always report exclusions in import previews and expose them through status. A user may explicitly import an ordinary excluded output after choosing a destination, but credential files and unsupported file kinds remain outside v0.2. Never follow links to collect content elsewhere.

`files remove` creates a tombstone for a logical path, even when it was remote-only. Removing a previously materialized file with ordinary tools also becomes a deletion when the inventory proves it was present; never infer deletion from the absence of a never-fetched file. Changes to ignore rules require a preview and must not silently delete previously tracked remote files.

File conflicts preserve both candidates under recovery. A fetch cannot overwrite a local dirty file. A rename is modeled as adding the new logical path and deleting the old one while reusing matching objects. Changing a file's content keeps its logical name and creates new content hashes.

## 12. Public templates

Templates contain only reusable instructions, skills/assets, optional workspace `AGENTS.md`, and portable settings. No memories, conversations, private workspace documents, credentials, or private object references.

Public snapshot schema shares file descriptors with agent snapshots but uses `kind: template-snapshot`, `templateId`, `name`, `description`, `snapshotId`, `parent`, `createdAt`, supported runtime metadata, and `files`. It has no `deviceId`, `lastSessionId`, or private template metadata. Allowed logical paths are `profile/SOUL.md`, `profile/skills/**`, `workspace/AGENTS.md`, and `config/portable.json`.

`template init DIRECTORY` creates a clean staging bundle with `template.json` (ID/name/description/runtime), `SOUL.md`, optional `AGENTS.md`, `skills/`, and `portable.json`. Publish reads this **explicit directory**, never the entire active agent. `template inspect` validates the bundle and displays the selected contents and a deterministic review digest over metadata plus sorted path/hash/size/executable descriptors. Interactive publish displays the same preview before asking for publication; EOF/cancellation means no publication. Noninteractive publish requires `--confirm-public <review-digest>` and rechecks the exact bytes before upload. Publication must never use a default-yes confirmation.

Publishing asks for a separate `/pub/hermes.pubky.app/v2/templates/<id>/:rw` grant. Keep its local credential/publication receipt beneath `<root>/networks/<network>/<owner>/templates/<id>/`, outside every scanned agent workspace. It uses immutable objects/snapshot/head-last publication. The normal agent grant cannot write publicly. Publishing does not also change a private agent's adoption state.

Adoption:

1. Fetch the public head/snapshot and validate its scope, schema, limits, paths, and all object hashes. Reject private/foreign object references and nested imports.
2. Present files, instructions, required skills/toolsets, executable assets, and snapshot digest for review.
3. Copy verified bytes into the private agent's object store and proposed effective inventory. If local paths already exist, show a diff and require the user to choose which to keep; no automatic concatenation of instructions.
4. Store source provenance and per-path adopted hashes; publish a private checkpoint. Afterwards, the source may disappear without breaking recovery.

Update is explicit. Compare three versions for each previously imported path: last-adopted template hash, current personal hash, and new template hash. If personal content is unchanged, propose the new version. Preserve locally modified paths and present a conflict when upstream also changed/deleted them. Never silently delete a customized skill or reset memory. Unchanged/new upstream files are adopted only as part of the reviewed update. After a resolved update, `managedPaths` records the new template's hashes, even where the user explicitly retained a differing personal version. A path deleted upstream but retained personally leaves `managedPaths` and becomes personal-only content.

For 0.2, if any conflicted path is unresolved, apply **none** of the update. Do not partially advance the overall template pin. After explicit resolution, copy every needed byte privately and publish one checkpoint. No recursive template composition, automatic update subscriptions, or template marketplace work.

## 13. Fresh setup and optional native Hermes import

Fresh setup is the default and must work without any previous plugin or Hermes profile. It creates a dedicated profile and the first complete checkpoint under a newly authorized agent root. Missing credentials are configured locally before the first model run.

There is **no migration from the old Pubky plugin**. Do not read its schemaVersion 1 profile/base-context documents, JSONL outbox, cached overlays, grants, root-level memory files, or installed configuration. There is no merging old overlay entries into new Markdown and no automatic cleanup of old installations. An old-format URI is rejected with a clear unsupported-format error before any write.

Optional `agent init --from-hermes-home PATH` is a one-time content import from the **pinned native Hermes 0.19.0 layout**, not a backward-compatibility layer:

1. Require an explicit source path and a stopped source Hermes process for a complete import. Never create a perpetual sync relationship with that source or modify it.
2. Inventory only `SOUL.md`, `memories/USER.md`, `memories/MEMORY.md`, `skills/`, allowlisted config keys, and the known-schema conversation DB. Ignore all plugin directories and plugin-specific state. Do not fall back to root-level memory filenames or try older Hermes schema adapters.
3. Copy a workspace only when `--workspace PATH` explicitly selects it. When supplied with a native profile import, map structured paths beneath that original workspace to the new managed workspace markers before storage. Do not rewrite historical free text.
4. Preview counts, bytes, external attachments, rejected paths, and excluded credentials/dependencies. Use the same path, file-kind, size, and exclusion policies as ordinary capture. Require confirmation; cancellation/EOF leaves remote content unchanged.
5. Request the new agent-directory capability through normal scoped authentication. Do not reuse or expand old plugin grants.
6. Stage exact Markdown/skill/workspace bytes and extract only portable config. Use the same validated online SQLite backup/normalization pipeline used for ongoing checkpoints. Never upload raw source configuration, WAL sidecars, or unrelated directories.
7. Validate the candidate and publish its complete initial checkpoint. Only after verified acknowledgement report the new agent URI, import summary, and launch command. A failed upload retains a retryable local candidate; it never modifies the source or claims the agent is portable.
8. Reject an already existing destination agent ID. Use attach to recover an existing new-format agent or choose a different ID for a new import.

On creation/import, validate memory content against both core-file storage limits and the selected Hermes memory limits. Offer larger supported prompt limits when necessary; never silently trim memory. Storage limits and model context limits are different constraints.

## 14. New modules and internal interfaces

Replace the existing `python/hermes_pubky/` internals with one cohesive package; do not add a parallel `managed/` implementation beside the old product. Suggested files are responsibilities, not permission to build a generic framework:

```text
python/hermes_pubky/
  __init__.py
  models.py          # Head, Snapshot, FileRecord, Piece, PortableConfig; strict parsers
  paths.py           # connection layout, identity partitioning, safe logical paths
  storage.py         # AgentRemote / PublicTemplateRemote transport adapters
  objects.py         # streaming chunk/hash/cache/assemble
  journal.py         # durable checkpoints, materialization inventory, requests
  sync.py            # checkpoint state machine, retries, conflict resolution
  projection.py      # allowlisted files, staged materialization, dirty scan
  hermes_adapter.py  # pinned paths/config/SQLite compatibility only
  provider.py        # managed lifecycle bridge and model tool schemas
  supervisor.py      # child lifecycle, lock, request processing, final sync
  templates.py       # staging, publish, adopt, three-way update comparison
  onboarding.py      # fresh profile creation and explicit native Hermes import
  cli.py             # parser registration, output and exit code mapping
```

Keep pure functions for validation, manifest construction, inclusion rules, and state transitions. Do not let `models.py` import Hermes or native bindings. `AgentRemote` is the injectable I/O boundary for unit tests, following the current `Remote` pattern. Use explicit dataclasses/protocols rather than untyped dictionaries across every internal boundary.

Required high-level Python interfaces (new design, not existing APIs):

```text
AgentRemote.read_head() -> Head | None
AgentRemote.read_snapshot(ref) -> verified Snapshot
AgentRemote.put_snapshot(snapshot_bytes) -> SnapshotRef
AgentRemote.read_object_to_path(piece, staging_path) -> verified local object
AgentRemote.put_object_from_path(piece, local_path) -> acknowledged object
AgentRemote.write_head(head) -> None
AgentRemote.list_snapshots(cursor, limit) -> page

Projection.capture(base_snapshot) -> sealed Candidate | NoChange
Projection.materialize(snapshot, required_paths) -> installed generation
SyncEngine.sync(candidate) -> structured SyncResult
HermesAdapter.capture_database(source, staging_dir) -> DBFileRecord | NoChange
HermesAdapter.restore_database(record, destination, workspace) -> None
Supervisor.run(connection, resume_id, offline, query) -> RunResult
```

All remote writes go through the sync engine or the explicit template publisher. Neither hooks nor CLI handlers may bypass the journal with ad hoc PUTs. `read_object_to_path` cannot report success until size/hash verification is complete. `write_head` is deliberately not named `compare_and_swap`.

Use `src/storage.rs` for scoped native operations and `src/objects.rs` for bounded streaming transfers; replace `src/urls.rs` policies with typed `AgentRoot`/`TemplateRoot` validation. Reuse SDK session/runtime/error helpers only where useful. Remove obsolete profile methods/exports and their registrations from the native module instead of keeping a second API surface. Native operations must distinguish private agents and public templates and require the correct actor/root for each operation. Never accept an unconstrained arbitrary write URL from Python.

Native requirements:

- Small metadata get/put for head/snapshot under exact allowed paths, with separate 4 KiB/1 MiB caps. Replace the old global document limit; no old 64 KiB profile API remains.
- Object read-to-file and put-from-file for generated object paths, with a 1 MiB object cap and streamed byte counting/hashing. Partial downloads remain temporary and cannot replace verified cached content.
- Public template GET uses public storage and sends no private grant. Publishing uses an authenticated, template-scoped session.
- Paginated snapshot listing uses the actual SDK `list(...).limit(...).cursor(...).send()` API; cursor advances from the last returned addressed entry. Reject repeated cursors, unexpected owners/roots, and unbounded pages.
- Clone/reuse SDK clients and keep `py.detach` around blocking native work. Add Tokio filesystem/I/O features and a streaming adapter such as `tokio-util::io::ReaderStream` only as needed; pass its body through the SDK's existing `put<P,B: Into<reqwest::Body>>`.
- Preserve status information needed for retry/auth/quota decisions. Do not misclassify every HTTP error as a retryable network outage.

The standalone managed CLI needs YAML parsing for Hermes config. Promote `pyyaml>=6,<7` from test-only usage to a runtime dependency if it is imported outside a guaranteed Hermes environment. Avoid additional services and large sync libraries. Add dependencies only when used by a selected responsibility.

## 15. Ordered implementation slices

### Slice 0 — Verify the upstream integration contract

Files: `docs/HERMES-COMPATIBILITY.md`, `scripts/check_hermes_integration.py`, `tests/test_hermes_contract.py`, adapter fixture generator.

- Install/test precisely Hermes 0.19.0 in an isolated test environment. Verify the wheel digest documented in the compatibility note.
- Exercise the real memory file locations, provider discovery, lifecycle signatures, `--resume`, configuration loading, and SQLite schema 22.
- Generate test fixtures for a normal conversation, a tool call/result, compression lineage, rewind/inactive messages, and a workspace path. Capture expected durable row values and schema structures.
- Demonstrate that a dedicated `HERMES_HOME` plus explicit working directory isolates the test from ordinary profiles. A test must fail if either path falls back to the user's real home.

Done when the real integration fixture proves these contracts. If the package does not match the documented contract, update the adapter/spec and evidence before continuing; do not fake a compatible Hermes class.

### Slice 1 — Typed storage and a private file round trip

Files: replacement native storage modules; Python models/paths/storage/objects; unit tests; Rust e2e tests.

- Implement IDs/paths, strict manifests, whole/piece hashing, caps, scoped capabilities, and streamed native object operations.
- Round-trip a raw `SOUL.md`, a JSON config, and a multi-chunk binary file against real Pubky v0.11 testnet.
- Validate private auth, wrong owner/scope rejection, read-without-auth rejection, public read without private credentials, and pagination.

Done when the actual backend stores/retrieves exact bytes and rejects invalid paths/sizes. No need to launch Hermes yet.

### Slice 2 — Durable checkpoint protocol

Files: journal/sync/objects, deterministic fake remote, fault tests, native e2e coverage.

- Implement sealed candidates, checkpoint state machine, content reuse, head-last publication, and acknowledgement after read-back.
- Inject failure before/after every object, snapshot, head, and local acknowledgement boundary. Restart from the journal and prove idempotent recovery.
- Add local interprocess locking, changed-head conflict preservation, explicit resolution, and history restore.

Done when partial uploads never expose an incomplete new active checkpoint and no pending local operation/object is silently dropped. Include a test showing that simultaneous-writer races remain outside the guarantee rather than asserting a fictional lock.

### Slice 3 — Launch an agent from remote Markdown and skills

Files: projection, replacement CLI/supervisor/provider, generated Hermes discovery bridge, configuration renderer.

- Implement create/attach, scoped auth, dedicated runtime generation, package-generated shim, effective configuration, and supported child launch arguments.
- Restore `SOUL.md`, actual memory paths, managed skills, and workspace `AGENTS.md` before starting Hermes.
- Wire dirty notifications and final-exit capture. Replace the old provider and remove its prompt-overlay/outbox path; there must be no mode router or parallel writer.
- Expose clear synced/dirty/offline status and retain pending work across supervisor restarts.

Done when a clean second profile starts with identical instructions/memory/skill content and no duplicate injected memory. This is an intermediate milestone, not completion of the full plan.

### Slice 4 — Managed workspace with on-demand files

Files: projection inventory, provider tools, supervisor requests, files CLI, inclusion rules.

- List remote-only files without downloading them, fetch/verify on demand, and capture new outputs with normal Hermes filesystem tools.
- Handle rename, explicit deletion, ordinary deletion of materialized files, ignore-rule changes, and local/remote-only name collisions.
- Bound cache eviction; pending work stays intact. Provide visible offline failure for unfetched documents.

Done when a reference PDF and report survive A-to-B transfer with exact hashes and no full workspace download on attach. Missing local files must not erase remote-only files.

### Slice 5 — Complete conversation recovery

Files: Hermes adapter, DB object chunking, generation installer, recovery tests.

- Implement consistent SQLite backup, runtime-state normalization, deterministic durable-content fingerprint, structural validation, chunked storage, and idle-only restore.
- Preserve active/compacted flags, tool-call/result IDs, reasoning/api-content fields, message order, titles, usage, and session relationships.
- Rebase only structured workspace paths and use Hermes' existing resume flow.
- Verify foreground end/interrupt/crash behavior; incomplete work must not be represented as remotely saved.

Done when the primary A-to-B acceptance scenario works with actual Hermes, including compression and rewind fixtures. A successful JSON export/import is not sufficient evidence.

### Slice 6 — Demonstrate public template sharing

Files: template schemas/module/CLI, scoped publisher, private-copy adoption tests.

- Create a clean template staging bundle, review/publish to testnet, adopt under a second identity, and create a private checkpoint.
- Prove template scopes never publish memories/conversations/private file references.
- Remove the test author's public content and prove the adopter can still recover from their own private copies.
- Exercise explicit updates with clean, modified, new, and deleted paths. Unresolved conflict means no update is applied.

Done when one person's template becomes another person's separate personal agent, without sharing private state or following silent upstream changes.

### Slice 7 — Finish onboarding and remove obsolete product surfaces

Files: onboarding/CLI, package exports, native registrations, replacement tests, README, architecture/security docs.

- Complete fresh initialization and the explicit pinned-native-Hermes import flow from section 13. Test previews, source preservation, stopped-source requirements, exact memory bytes, excluded secrets, and new scoped authorization.
- Delete remaining unused overlay schemas, outbox/mirroring code, old setup/installer commands, old exports, and obsolete tests. Remove dead imports, registrations, dependencies, and documentation references. Do not move old code into a `legacy/` package.
- Replace old CLI/wheel smoke assertions with checks for the supported command tree, one provider, and the new transport API. Existing test count is not a success criterion.
- Document local credentials, HS operator visibility, historical retention, offline recovery, unsupported concurrency, and that this release replaces the experiment without an upgrade path.
- Verify old-format addresses fail clearly and old local/remote data is neither auto-imported nor modified. Old installation cleanup, if wanted, remains an explicit user action.

Done when a fresh install works with no old profile present, an explicitly selected native Hermes profile can seed a new agent, and the installed package has no legacy provider/migration branch. Documentation describes only the implemented product, apart from clearly marked historical notes.

### Slice 8 — Release verification

Files: wheel smoke scripts, workflows, e2e runner, recovery test matrix, release notes.

- Replace the old overlay discovery scenario in CI with actual Hermes 0.19 managed-provider startup, lifecycle, and recovery tests. No legacy test lane is required.
- Run Rust fmt/clippy/unit/e2e, Python unit/integration, and built-wheel smoke checks on the supported matrix.
- Exercise two independent local management roots and a local deterministic test model endpoint against actual Hermes and actual Pubky testnet. The fake model is only for predictable conversational behavior; do not replace Pubky or Hermes in this acceptance test.
- Build macOS universal2 and Linux x86_64/aarch64 wheels using the existing build mechanics where useful. Target Python 3.11–3.13 with ABI3 wheels as supported by the selected native build. Verify the replacement console script, generated plugin assets, package modules, and runtime dependencies are in the installed wheel. Wheel packaging does not create a commitment to old Python/native APIs.
- Update both package versions and lockfiles to 0.2.0 only when release scope is implemented. Prepare release notes; actual tagging/publishing remains a separate user-requested action.

Done when the matrix below passes, residual limits are documented, and a fresh-install demonstration meets the primary acceptance scenario. Do not expand advertised compatibility based on untested assumptions.

## 16. Acceptance and failure matrix

| Scenario | Required observable result | Primary test layer |
| --- | --- | --- |
| A-to-B after successful sync | Same core files, skills, durable conversations, file catalogue, and fetched bytes | Actual Hermes + Pubky testnet |
| Same ID under different owners/networks | Separate local data and credentials | Unit + integration |
| Clean replacement with old experimental data present | One provider/CLI/schema; no old-state discovery, mutation, fallback, or migration | CLI + installed-wheel integration |
| Fresh init / explicit native import | Fresh init needs no source; optional import preserves supported bytes and never changes source | Process + adapter integration |
| Memory file update | Exact `memories/` file uploaded once; no duplicate prompt entries | Actual Hermes integration |
| Tool-generated report | New managed file visible/fetchable on B | End-to-end |
| Unfetched remote file absent locally | File remains in next checkpoint inventory | Unit + integration |
| File fetch interrupted | No partial file becomes visible as verified content | Transfer fault test |
| Same path created locally before remote fetch | Conflict with both byte versions preserved | Integration |
| Rename/delete | Correct logical inventory; retained history remains readable | Integration |
| Mid-upload failure or retry | Previous head stays valid; resume avoids corrupt/duplicate effects | Fault injection + testnet |
| Lost response after head PUT | Read-back recognizes success or preserves uncertainty; no silent data loss | Fault injection |
| New local write during upload | New write remains pending for another checkpoint | Concurrency unit test |
| Cross-process duplicate run | Second local writer is rejected | Process integration |
| Changed remote head with pending work | Upload freezes; recovery candidates retained | Integration |
| Two truly simultaneous remote writers | Documented unsupported case; immutable candidates remain accessible | Adversarial integration |
| Offline with complete local profile | Runs locally, queues changes, reports unsynced status | Process integration |
| Offline fresh attach | Clear failure; no empty replacement agent | Process integration |
| Quota/413/auth revoked | Typed failure, visible pending state, no dropped bytes/retry storm | Testnet or protocol fault proxy |
| Crash/SIGKILL during run/install | Dirty source retained; journal safely resumes or diagnoses corruption | Process fault tests |
| Cache pressure | Only acknowledged reconstructible objects evicted | Unit + integration |
| SQLite WAL data | Committed messages survive source process loss | Actual SQLite/Hermes integration |
| Compaction and rewind | Correct lineage and inactive flags after restore; no reactivated undone messages | Actual Hermes fixtures |
| DB restore while child active | Refused/deferred; no live DB replacement | Process integration |
| Old workspace paths | Structured paths resolve to B; historical text remains intact | Adapter test |
| Unsupported Hermes/schema | Refused before rewriting working state | Adapter + CLI tests |
| Traversal/case collision/links | Rejected before read/write outside allowed roots | Unit + native tests |
| Template author offline after adoption | Agent recovers from private copies | Two-identity testnet |
| Template update vs personal edits | Explicit conflict; no partial update or memory reset | Integration |
| Template publication | Only reviewed allowed bundle files become public | Testnet + request inspection |
| Oversized metadata/files/DB | Limits enforced while streaming; original local state retained | Native + process tests |

Performance evidence to collect (targets to validate, not measurements already made):

- A warm launch with unchanged files sends no object uploads.
- Editing one small Markdown file uploads that object plus snapshot/head metadata, not the workspace/database.
- A new conversation turn uploads only changed DB chunks, plus any actually changed files and metadata. Record upload amplification; SQLite normalization must not gratuitously rewrite the whole database.
- Cold attach fetches core files/skills/DB but no ordinary workspace document until requested.
- Compare peak supervisor RSS on a 32 MiB and 128 MiB DB fixture; confirm the code streams/chunks rather than holding either whole file as a byte buffer. Record HS memory separately from local supervisor and Hermes child memory.
- An idle run without dirty requests does not repeatedly create remote checkpoints or traverse the HS tree.

Create a reproducible opt-in performance fixture with 1,000 files of 64 KiB, ten files of 5 MiB, and a 32 MiB conversation DB. Keep regular CI fixtures smaller; do not make timing-sensitive benchmarks the only correctness signal.

## 17. Verification commands and final handoff checklist

Use isolated homes/testnet resources. Never point integration tests at a user's normal Hermes profile or production identity.

Baseline verification commands for the selected Python/Rust stack (update test contents and scripts for the replacement; do not preserve obsolete behavior):

```bash
.venv/bin/pytest -q
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --lib
cargo test --test e2e -- --ignored --test-threads=1
```

Use the existing PostgreSQL service/testnet configuration from README/CI as the starting point for the Rust integration suite. If the replacement reorganizes test targets, update these commands and CI together. Record actual commands/results; do not mark ignored e2e tests as passed because the ordinary unit command skipped them.

Add `scripts/check_managed_hermes_integration.py` and `scripts/check_managed_handoff.py` with documented arguments for scratch roots, testnet configuration, and optional deterministic model endpoint. They must create temporary directories themselves and fail if they resolve to a production home. Include offline and crash recovery modes, and emit checkpoint IDs/hashes without credentials.

Final implementation handoff must include:

- Feature summary of the replacement product and an explicit note that old plugin data/APIs/commands are unsupported.
- Storage/schema and CLI docs matching the implemented interfaces.
- Evidence for fresh setup, fresh-machine recovery, template sharing, and explicit native Hermes import.
- Checks run, checks skipped, supported versions/platforms, and remaining limitations.
- Recovery instructions for unsynced local changes, conflicting heads, bad grants, quota exhaustion, and interrupted materialization.
- Self-reviewed diff with no production secrets, copied user profiles, large fixture databases, or temporary wheel/source downloads committed.

Do not finish with only a scaffold, provider prompt injection, or a filesystem sync demo. The release outcome requires the restored agent to continue a real saved Hermes conversation with its managed files available.
