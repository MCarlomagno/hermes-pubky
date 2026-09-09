# Security notes

## What this plugin holds

One secret: the Pubky **grant secret**, stored in the profile-scoped
`$HERMES_HOME/.env` at mode `0600`. It is bearer-equivalent until the grant
expires or is revoked. It is never written to the homeserver, never placed in
`config.yaml` or the cache, and is scrubbed from log and error output by
shape-matching (`pubky-grant-credential-*` and JWS-shaped strings) as well as
by exact value.

Status output shows only an 8-character SHA-256 fingerprint of the secret.

## Capability scope

Setup requests exactly `/priv/hermes.pubky.app/v1/profiles/:rw` — no root
capability, no access to other apps' data. The plugin refuses locally, before
any request leaves the process, to touch a path outside that directory, so a
bug cannot turn into a request the homeserver has to reject.

Revocation uses the session's own `DELETE /auth/grant/session`. The
account-level `GrantManager::revoke` is not used because the homeserver
restricts it to root-capability sessions, which this grant deliberately is not.

## `/priv` is access-controlled, not encrypted

Your homeserver operator can read your private profile. This is a property of
Pubky homeserver v0.11, not of this plugin. Keep genuinely sensitive facts in
your local `USER.md`, which the plugin only ever reads and never uploads
unless you explicitly import during setup.

## Untrusted input

Both documents are attacker-influenceable and are treated as such:

- **Size**: capped at 64 KiB, enforced by a streaming read rather than by
  trusting `Content-Length`.
- **Schema**: strictly validated and rejected rather than repaired. Entries
  are bounded (4,000 chars each, 200 per list); instructions at 40,000 chars.
- **Paths**: normalized by the SDK (which rejects `.`, `..` and `//`) and then
  policy-checked. Percent-encoded traversal is re-encoded into a literal
  segment, not resolved.
- **Base context content**: pinned by the SHA-256 of its raw bytes. Changed
  content is refused and the approved copy kept until the user re-approves.

## Prompt injection

An approved base context is third-party text that reaches the system prompt.
That is the feature, and it cannot be sanitized away. The mitigations are
consent and immutability: the user approves a specific document, is shown its
id, name, description, size and hash first, and the pin means the author
cannot change it afterwards. The injected block labels the content as
user-provided instructions rather than system-level authority.

Treat pinning someone's base context as running their instructions in your
agent. Pin contexts from authors you trust.

## Contexts that never write

Writes are suppressed for `subagent`, `cron` and `flush` agent contexts. A
cron run is not the user talking, and a subagent's memory belongs to its
parent; mirroring either would pollute the portable overlay.

## Reporting

Please open a security issue at
<https://github.com/MCarlomagno/hermes-pubky-memory/issues>.
