# Hermes compatibility evidence for managed agent storage

Inspected 2026-09-09 for the [0.2 implementation plan](./IMPLEMENTATION-PLAN.md). This is a source inspection record, not proof that managed mode has been implemented or run successfully.

Compatibility in this document means integration with the pinned upstream Hermes runtime, not support for this project's old plugin, APIs, commands, or data. The selected design is a [clean replacement](./adr/0003-clean-replacement.md) with no 0.1 migration path.

## Pinned baseline

- Integration repository commit: `84c2953` in `MCarlomagno/hermes-pubky-memory`.
- Existing CI installs `hermes-agent==0.19.0` in `.github/workflows/ci.yml`.
- Source inspected: published `hermes_agent-0.19.0-py3-none-any.whl`.
- SHA-256: `bd0bac012aee38a60894781f4597dc29ee7bedb3448540249921f10d3bef327f`.
- [Release metadata](https://pypi.org/pypi/hermes-agent/0.19.0/json).
- [Exact wheel](https://files.pythonhosted.org/packages/e5/30/c85be8290e9565dc3c7a9720e93f3e59e09b1b163487be4946c3aa848f80/hermes_agent-0.19.0-py3-none-any.whl).
- Pubky SDK remains pinned by `Cargo.toml` to `6a14bdb8fa2e30ef4e4b241fcdd3992c453d2378` (v0.11.0).

For reproducible inspection, download the wheel to a temporary directory, verify the digest, and unzip it there. Do not commit the wheel or extracted third-party code. Its Python module paths below are relative to the extracted wheel. Treat changes to this version/digest/schema as a compatibility decision requiring tests, not a routine automatic dependency bump.

## Contracts checked in the wheel

| Source | Observed contract | Consequence |
| --- | --- | --- |
| `hermes_constants.py:get_hermes_home` | Resolves explicit context/process override and `HERMES_HOME` | Set the child environment before Hermes imports |
| `agent/prompt_builder.py:load_soul_md` | Reads `<HERMES_HOME>/SOUL.md` | Restore before prompt construction |
| `tools/memory_tool.py:get_memory_dir` | Returns `<HERMES_HOME>/memories` | Current plugin root-level path assumptions are wrong for this package |
| `tools/memory_tool.py:MemoryStore` | Uses `\n§\n` entry delimiter and atomic local file replacement | Preserve exact files; callbacks signal capture |
| `tools/skills_tool.py:_skills_dir` | Reads profile-scoped `skills` | Materialize managed skills before startup |
| `hermes_cli/config.py:DEFAULT_CONFIG` | Has string `model`, `toolsets`, `agent.max_turns`, memory settings, terminal cwd | Render a small allowlist into a generated local config |
| `agent/memory_provider.py` | Has full `messages` in `sync_turn`, session-switch, memory-write, and lifecycle hooks | Bridge lifecycle locally; do not store only flattened turn strings |
| `agent/memory_manager.py:sync_all` | Dispatches provider sync in a background worker | Callback timing alone is not a global snapshot barrier |
| `run_agent.py` | Memory sync skips interrupted/empty turns | Supervisor must handle final exit and crash recovery separately |
| `hermes_cli/main.py` | Supports module execution and resume; can restore saved cwd | Launch a separate process and rebase structured saved paths |
| `hermes_state.py:DEFAULT_DB_PATH` | Uses `<HERMES_HOME>/state.db` | Database belongs to the dedicated projection |
| `hermes_state.py:SCHEMA_VERSION` | Value is 22 | Initial adapter explicitly targets schema 22 |

The console-script entry point is `hermes = hermes_cli.main:main`. Test `python -m hermes_cli.main` with the selected options; the source has a main guard. The current plugin demonstrates filesystem-based discovery, not an imagined `hermes_agent.memory_providers` entry point. Generate the required discovery bridge in the new dedicated profile; keeping this Hermes integration mechanism does not require retaining the old installer or provider.

## Conversation database details that affect the design

`SessionDB.export_session()` obtains active messages by default. `export_session_lineage()` groups compression segments. `import_sessions()` skips existing IDs, resets live routing state, and inserts message rows as active. It therefore does not provide lossless replacement of the whole database's history semantics.

The import helper also limits each session to 5 MiB and 10,000 messages. The snapshot design does not alter or bypass those helpers; it uses an independently validated, same-version database backup instead.

The database includes durable tables `sessions`, `messages`, and `session_model_usage`, schema metadata, and FTS structures. It also includes runtime/control tables `state_meta`, `gateway_routing`, `compression_locks`, and `async_delegations`. Optional Telegram binding tables are created by specialized paths. Snapshot normalization must be explicit about which state survives.

Important message data includes insertion order, tool-call/result pairing, reasoning payloads, `api_content`, and `active`/`compacted` flags. Compression and branches use session relationships. Test these values after restoration; a transcript that looks correct in a text preview is insufficient.

The importer intentionally does not resume live channel/process ownership. The new adapter must preserve that boundary while recovering conversations. Structured cwd rebasing is necessary because CLI resume consults the saved session directory.

## Pubky contracts checked in the pinned checkout

- [`storage/verbs.rs`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-sdk/src/actors/storage/verbs.rs): session `get`, `stats`, `put` accepting a `reqwest::Body`, and `delete` exist.
- [`storage/list.rs`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-sdk/src/actors/storage/list.rs): `list`, `limit`, `cursor`, `shallow`, and `send` are available. Results are addressed entries, not an invented database cursor object.
- [`tenant read handler`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/src/client_server/routes/tenants/read.rs): streams file reads and supports conditional GET.
- [`tenant write handler`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/src/client_server/routes/tenants/write.rs): streams PUT and applies quotas. No conditional-PUT/CAS or multi-file transaction is exposed in this path.
- [`storage configuration`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/config.sample.toml): disk-backed and other storage backends; user storage quotas are configurable. Do not reuse the old skill's 0.9-era defaults as v0.11 limits.
- [`OpenAPI`](https://github.com/pubky/pubky-core/blob/v0.11.0/pubky-homeserver/openapi-client.yml): `/priv` access is capability-gated. `/priv` is not end-to-end encryption from the operator.

The SDK/source supports the required primitives, but the current plugin wrapper only exposes profile-oriented JSON methods. Replace that wrapper surface with the new scoped metadata/streaming operations, reusing useful SDK helpers without retaining the old profile API. This remains implementation work, not already available functionality.

## Verified by Slice 0

Checked on 2026-09-09 against the installed wheel, not by reading source alone.
`tests/test_hermes_contract.py` holds these as tests (34 pass with the package
installed, 13 with it absent), and
`tests/fixtures/hermes_0_19_0_schema22.json` is the recorded database contract,
regenerated by `scripts/generate_hermes_fixture.py`.

- Wheel SHA-256 matches the pin above.
- `hermes_state.SCHEMA_VERSION` is 22 at runtime. A generated database has 20
  tables, including `messages_fts` and `messages_fts_trigram`.
- `get_memory_dir()` returns `<HERMES_HOME>/memories`, and `ENTRY_DELIMITER` is
  `"\n§\n"`.
- `DEFAULT_CONFIG` supplies every key in the plan's portable allowlist at the
  documented values: `model: ""`, `toolsets: ["hermes-cli"]`,
  `agent.max_turns: 90`, `memory_char_limit: 2200`, `user_char_limit: 1375`,
  `terminal.backend: "local"`.
- Every `sessions` and `messages` column the adapter preserves or clears exists,
  including `active`, `compacted`, `tool_call_id`, `tool_calls`, `api_content`,
  `reasoning`, `parent_session_id`, `rewind_count`, and the channel-routing and
  handoff fields.
- Setting `HERMES_HOME` redirects both `get_hermes_home()` and
  `hermes_state.DEFAULT_DB_PATH`. With it unset, both resolve to `~/.hermes`,
  which is the fallback the supervisor must always prevent for a child.

### Two corrections to the table above

**`DEFAULT_DB_PATH` is bound at import time.** `hermes_state.py:153` evaluates
`get_hermes_home() / "state.db"` at module level, so the child's `HERMES_HOME`
has to be set before the import, not merely before first use. `_skills_dir()`
resolves per call instead, so the two are not interchangeable.

**`--query` is not a Hermes option.** The launcher option in the plan maps to
`-z` / `--oneshot`. The pinned parser also accepts `--resume`, `--model`,
`--toolsets`, and `--no-restore-cwd`; the last is worth passing because the
adapter rebases a session's recorded working directory itself.

`--worktree`, `--yolo`, `--accept-hooks`, and the `gateway`, `cron`, and `proxy`
subcommands exist upstream and must stay unreachable from the launcher and from
portable configuration. The contract test pins their names so a rename shows up
as a failure rather than a silently stale blocklist.

### Two API details for the adapter

`SessionDB(db_path=...)` requires a `Path`; a string raises `AttributeError`.
Its connection is private (`_conn`), so the fixture generator writes through
`create_session` / `append_message` and then adjusts flags over plain
`sqlite3`. The adapter should do the same rather than reaching into internals.

## Verification still required by later slices

Slice 0 establishes the contract and the recorded fixtures. These still need a
running agent:

1. Child-process startup of a full agent with a dedicated home and workspace
   (Slice 3). Slice 0 proves only that `python -m hermes_cli.main` honors the
   dedicated home and resolves its database inside it.
2. Provider discovery and notification dispatch, including session switches and
   interrupts (Slice 3).
3. Consistent online backup while Hermes has committed WAL writes (Slice 5).
4. Snapshot normalization and restore without losing inactive or compacted
   message state (Slice 5), against the fixtures Slice 0 generated.
5. Resume and a further successful turn through a deterministic local
   test-model endpoint (Slice 5).
6. Tool-written workspace changes and on-demand file fetch through the running
   provider (Slice 4).
7. macOS and Linux wheel behavior, and schema/FTS compatibility (Slice 8).
