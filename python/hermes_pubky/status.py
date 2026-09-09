"""Status reporting.

Everything here is safe to print: the grant is reduced to a fingerprint, and
the state names are the ones the plan requires so a caller can branch on a
stable value rather than parsing prose.

Reference: implementation plan section 10.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

from .journal import Journal, JournalError
from .paths import GRANT_ENV, Layout, read_env_file

# The states a caller may branch on.
SYNCED = "synced"
DIRTY = "dirty"
SYNCING = "syncing"
OFFLINE = "offline"
AUTH_REQUIRED = "auth-required"
QUOTA_BLOCKED = "quota-blocked"
CONFLICT = "conflict"
CORRUPT = "corrupt"


def fingerprint(secret: str) -> str:
    """A short, non-reversible label for a grant, safe to display."""
    if not secret:
        return "(none)"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def agent_status(layout: Layout, network: str, *,
                 check_remote: bool = True) -> Dict[str, Any]:
    """Describe one agent's local and, optionally, remote state."""
    grant = read_env_file(layout.credentials_file).get(GRANT_ENV, "")
    snapshot: Dict[str, Any] = {
        "agentId": layout.agent_id,
        "owner": layout.owner,
        "network": network,
        "root": str(layout.root),
        "workspace": str(layout.workspace),
        "grant": fingerprint(grant),
        "state": SYNCED,
        "detail": "",
    }

    try:
        journal = Journal(layout.journal_file)
    except JournalError as exc:
        snapshot["state"] = CORRUPT
        snapshot["detail"] = str(exc)
        return snapshot

    try:
        active = journal.active_checkpoints()
        conflicts = journal.conflicted_checkpoints()
        blocked = [c for c in active if c.state == "blocked"]
        inventory = journal.list_materialized()
        pending_bytes = sum(
            u.size for c in active for u in journal.all_uploads(c.id)
            if not u.acknowledged)
        snapshot.update({
            "checkpoint": journal.get_setting("acknowledged_head", {}) or {},
            "lastSyncedAt": journal.get_setting("acknowledged_at", "") or "never",
            "pendingCheckpoints": len(active),
            "pendingBytes": pending_bytes,
            "materialized": sum(1 for r in inventory if r.present),
            "remoteOnly": sum(1 for r in inventory if not r.present
                              and not r.explicit_delete),
            "dirtyPaths": journal.dirty_paths(),
            "failedPaths": [c.last_error for c in blocked if c.last_error],
        })

        if conflicts:
            snapshot["state"] = CONFLICT
            snapshot["detail"] = (
                conflicts[0].last_error
                or "the remote head moved while local writes were pending")
        elif not grant:
            snapshot["state"] = AUTH_REQUIRED
            snapshot["detail"] = (
                f"run 'hermes-pubky agent login {layout.agent_id}'")
        elif active:
            snapshot["state"] = DIRTY
            snapshot["detail"] = (
                f"{len(active)} checkpoint(s) saved locally, not yet on the "
                "homeserver")
        elif journal.dirty_paths():
            snapshot["state"] = DIRTY
            snapshot["detail"] = "local changes have not been checkpointed"

        if check_remote and grant and snapshot["state"] not in (CONFLICT, CORRUPT):
            _add_remote(snapshot, layout, grant)
    finally:
        journal.close()
    return snapshot


def _add_remote(snapshot: Dict[str, Any], layout: Layout, grant: str) -> None:
    """Add live homeserver facts, classifying a failure rather than raising."""
    try:
        from .storage import AgentRemote

        remote = AgentRemote.connect(grant, layout.owner, layout.agent_id)
        head = remote.read_head()
        snapshot["homeserver"] = "reachable"
        snapshot["capabilities"] = remote.capabilities
        snapshot["remoteSnapshot"] = head.snapshot_id if head else None
    except Exception as exc:  # noqa: BLE001 - offline is an ordinary state
        name = type(exc).__name__
        message = str(exc)
        if name == "PubkyAuthError" or "401" in message or "revoked" in message.lower():
            snapshot["state"] = AUTH_REQUIRED
            snapshot["homeserver"] = f"not authorized ({message})"
        elif "quota" in message.lower() or "413" in message:
            snapshot["state"] = QUOTA_BLOCKED
            snapshot["homeserver"] = f"quota exceeded ({message})"
        else:
            if snapshot["state"] == SYNCED:
                snapshot["state"] = OFFLINE
            snapshot["homeserver"] = f"unreachable ({message})"


def format_status(snapshot: Dict[str, Any]) -> str:
    """Render a status dictionary as aligned lines."""
    order = ["agentId", "owner", "network", "state", "detail", "grant",
             "lastSyncedAt", "pendingCheckpoints", "pendingBytes",
             "materialized", "remoteOnly", "homeserver", "remoteSnapshot",
             "capabilities", "workspace"]
    rows = [(key, snapshot[key]) for key in order if key in snapshot
            and snapshot[key] not in ("", None, [])]
    if not rows:
        return "  (no status available)\n"
    width = max(len(key) for key, _ in rows)
    lines = [f"\n{snapshot.get('agentId', 'agent')}\n" + "-" * 44]
    for key, value in rows:
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"  {_label(key):<{width + 2}} {value}")
    dirty = snapshot.get("dirtyPaths") or []
    if dirty:
        lines.append(f"  {'changed':<{width + 2}} {len(dirty)} path(s)")
        for path in dirty[:10]:
            lines.append(f"    {path}")
    return "\n".join(lines) + "\n"


def _label(key: str) -> str:
    out = []
    for char in key:
        if char.isupper():
            out.append(" ")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)
