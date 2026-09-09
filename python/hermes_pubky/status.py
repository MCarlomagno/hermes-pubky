"""Status reporting for ``hermes pubky status`` and ``hermes memory status``.

Everything here is safe to print: the grant secret is reduced to a short
fingerprint, and error text passes through :func:`config.redact`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import config as cfg
from .outbox import Outbox
from .paths import Layout, hermes_home
from .remote import Remote, native_available
from .store import Store


def _layout(provider_config: Optional[Dict[str, Any]] = None) -> Layout:
    if provider_config and isinstance(provider_config.get("profile_id"), str):
        pid = provider_config["profile_id"].strip() or cfg.DEFAULT_PROFILE_ID
    else:
        pid = cfg.profile_id()
    return Layout(hermes_home(), pid)


def status_snapshot(provider_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A compact, offline status summary."""
    layout = _layout(provider_config)
    store = Store(layout)
    outbox = Outbox(layout.outbox)
    state = store.load_state()
    profile = store.load_profile()
    _context, meta = store.load_context()
    secret = cfg.read_grant_secret(layout.env_file)

    # The profile is authoritative for the pin; the cached body may lag behind
    # it on a machine that has adopted an existing profile but not yet started
    # a session.
    pinned = profile.base_context if profile is not None else None
    if pinned is not None:
        base_context = pinned.url
        cached_body = (
            "yes" if meta is not None and meta.sha256 == pinned.sha256 else "not yet fetched"
        )
    else:
        base_context = "(none)"
        cached_body = "n/a"

    snapshot: Dict[str, Any] = {
        "profile_id": layout.profile_id,
        "native_extension": "available" if native_available() else "MISSING",
        "grant": f"present ({cfg.fingerprint(secret)})" if secret else "not configured",
        "cached_profile": "yes" if profile is not None else "no",
        "revision": state.last_revision,
        "last_synced_at": state.last_synced_at or "never",
        "pending_writes": outbox.count(),
        "base_context": base_context,
        "base_context_cached": cached_body,
    }
    if state.conflict:
        snapshot["conflict"] = state.conflict_detail or "unresolved"
    return snapshot


def full_status(timeout_secs: float = 5.0, *, check_remote: bool = True) -> Dict[str, Any]:
    """Status including a live homeserver check.

    The remote check is best-effort: being offline is an ordinary state for
    this plugin, so a failure is reported as a field rather than raised.
    """
    snapshot = status_snapshot()
    layout = _layout()
    secret = cfg.read_grant_secret(layout.env_file)

    if not check_remote or not secret or not native_available():
        snapshot["homeserver"] = "not checked"
        return snapshot

    try:
        remote = Remote.connect(secret, timeout_secs)
        snapshot["public_key"] = remote.public_key
        snapshot["capabilities"] = ", ".join(remote.capabilities) or "(none)"
        profile = remote.fetch_profile(layout.profile_id, timeout_secs)
        snapshot["homeserver"] = "reachable"
        snapshot["remote_revision"] = profile.revision if profile else 0
        snapshot["remote_profile"] = "present" if profile else "not created yet"
    except Exception as exc:  # noqa: BLE001 - offline is a normal state
        # An expired or revoked grant is an authorization problem the user has
        # to fix, not a network outage; saying "unreachable" would send them
        # looking in the wrong place.
        snapshot["homeserver"] = f"{_failure_kind(exc)} ({cfg.redact(exc)})"
    return snapshot


def _failure_kind(exc: BaseException) -> str:
    """Classify a live-check failure for display."""
    try:
        from hermes_pubky.remote import native

        mod = native()
    except Exception:
        return "unreachable"

    auth = getattr(mod, "PubkyAuthError", None)
    if auth is not None and isinstance(exc, auth):
        return "not authorized"
    timeout = getattr(mod, "PubkyTimeoutError", None)
    if timeout is not None and isinstance(exc, timeout):
        return "timed out"
    return "unreachable"


def format_status(snapshot: Dict[str, Any]) -> str:
    """Render a snapshot as aligned key/value lines."""
    if not snapshot:
        return "  (no status available)\n"
    width = max(len(k) for k in snapshot)
    lines = [f"  {key.replace('_', ' '):<{width}}  {value}"
             for key, value in snapshot.items()]
    return "\n".join(lines) + "\n"
