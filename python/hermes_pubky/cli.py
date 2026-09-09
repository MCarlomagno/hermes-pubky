"""``hermes pubky ...`` subcommands.

Hermes discovers this module through ``cli.py`` in the installed plugin
directory and calls :func:`register_cli` while building its argument parser,
then dispatches to :func:`pubky_command`.
"""

from __future__ import annotations

from typing import Any, Optional

from . import config as cfg
from .outbox import Outbox, utcnow
from .paths import Layout, hermes_home
from .remote import Remote, native_available
from .schema import Profile
from .status import format_status, full_status
from .store import Store
from .sync import Syncer


def register_cli(subparser: Any) -> None:
    """Build the ``hermes pubky`` command tree."""
    subs = subparser.add_subparsers(dest="pubky_command")

    status = subs.add_parser("status", help="Show sync, grant and cache status")
    status.add_argument(
        "--offline", action="store_true",
        help="Skip the homeserver check and report local state only",
    )

    subs.add_parser("login", help="Authorize this machine via Pubky Ring")
    subs.add_parser(
        "logout",
        help="Revoke the grant and delete the local secret (cache is kept)",
    )

    sync = subs.add_parser("sync", help="Reconcile pending writes with the homeserver")
    sync.add_argument(
        "--prefer", choices=("remote", "local"), default=None,
        help="Resolve a conflict by keeping one side (the other is backed up first)",
    )

    base = subs.add_parser("base", help="Manage the pinned public base context")
    base_subs = base.add_subparsers(dest="base_command")
    base_set = base_subs.add_parser("set", help="Pin a public base context")
    base_set.add_argument("url", help="pubky:// URL of the context document")
    base_subs.add_parser(
        "refresh", help="Re-approve the pinned context after its content changed"
    )
    base_subs.add_parser("clear", help="Remove the pinned base context")


def pubky_command(args: Any) -> None:
    """Dispatch a ``hermes pubky`` invocation."""
    command = getattr(args, "pubky_command", None)
    handlers = {
        "status": cmd_status,
        "login": cmd_login,
        "logout": cmd_logout,
        "sync": cmd_sync,
        "base": cmd_base,
    }
    handler = handlers.get(command or "status", cmd_status)
    handler(args)


# -- context helpers --------------------------------------------------------


def _context() -> tuple:
    layout = Layout(hermes_home(), cfg.profile_id())
    layout.ensure()
    return layout, Store(layout), Outbox(layout.outbox)


def _require_native() -> bool:
    if native_available():
        return True
    print(
        "\n  The hermes-pubky native extension is not available.\n"
        "  Reinstall with: uv pip install --force-reinstall hermes-pubky\n"
    )
    return False


def _connect(layout: Layout, timeout: float = 10.0) -> Optional[Remote]:
    secret = cfg.read_grant_secret(layout.env_file)
    if not secret:
        print("\n  Not authorized. Run 'hermes pubky login' first.\n")
        return None
    try:
        return Remote.connect(secret, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not connect: {cfg.redact(exc)}\n")
        return None


# -- commands ---------------------------------------------------------------


def cmd_status(args: Any) -> None:
    offline = bool(getattr(args, "offline", False))
    print("\nPubky portable context\n" + "─" * 40)
    print(format_status(full_status(check_remote=not offline)))


def cmd_login(args: Any) -> None:
    del args
    if not _require_native():
        return
    from .setup_flow import authorize

    layout, _store, _outbox = _context()
    secret = authorize(layout)
    if not secret:
        print("\n  Not authorized.\n")
        return
    print("\n  Grant secret saved to .env (mode 0600).\n")


def cmd_logout(args: Any) -> None:
    del args
    layout, _store, _outbox = _context()
    secret = cfg.read_grant_secret(layout.env_file)
    if not secret:
        print("\n  Already logged out.\n")
        return

    revoked = "not attempted"
    if native_available():
        try:
            Remote.connect(secret, 10.0).revoke(10.0)
            revoked = "revoked at the homeserver"
        except Exception as exc:  # noqa: BLE001
            revoked = f"could not revoke remotely ({cfg.redact(exc)})"

    cfg.clear_grant_secret(layout.env_file)
    print(f"\n  Local grant secret deleted; {revoked}.")
    print("  The local cache was kept. Run 'hermes pubky login' to reconnect.\n")


def cmd_sync(args: Any) -> None:
    if not _require_native():
        return
    prefer = getattr(args, "prefer", None)
    layout, store, outbox = _context()
    remote = _connect(layout)
    if remote is None:
        return

    syncer = Syncer(store, outbox, layout.profile_id, lambda: remote)
    try:
        result = syncer.sync(prefer=prefer, timeout_secs=15.0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Sync failed: {cfg.redact(exc)}\n")
        return

    print(f"\n  {result.status}: {result.detail}")
    print(f"  revision {result.revision}, {outbox.count()} write(s) still queued\n")


def cmd_base(args: Any) -> None:
    command = getattr(args, "base_command", None)
    if command == "set":
        _base_set(getattr(args, "url", ""))
    elif command == "refresh":
        _base_refresh()
    elif command == "clear":
        _base_clear()
    else:
        _base_show()


def _base_show() -> None:
    _layout, store, _outbox = _context()
    context, meta = store.load_context()
    print("\nPinned base context\n" + "─" * 40)
    if meta is None:
        print("  (none)\n")
        return
    print(f"  url:      {meta.url}")
    print(f"  sha256:   {meta.sha256}")
    print(f"  approved: {meta.approved_at}")
    if context is not None:
        print(f"  id:       {context.id}")
        print(f"  name:     {context.name or '(unnamed)'}")
    print()


def _base_set(url: str) -> None:
    if not _require_native():
        return
    if not url:
        print("\n  Usage: hermes pubky base set <pubky-url>\n")
        return
    from .setup_flow import resolve_base_context

    layout, store, outbox = _context()
    remote = _connect(layout)
    if remote is None:
        return

    try:
        ref, context, raw, digest = resolve_base_context(url)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not use that context: {cfg.redact(exc)}\n")
        return

    print(f"\n  id:          {context.id}")
    print(f"  name:        {context.name or '(unnamed)'}")
    print(f"  description: {context.description or '(none)'}")
    print(f"  size:        {len(raw)} bytes")
    print(f"  sha256:      {digest}")

    if not _write_base_ref(remote, store, outbox, layout.profile_id, ref):
        return
    store.save_context(raw, ref.url, digest)
    print("\n  ✓ Pinned. Start a new session to pick it up.\n")


def _base_refresh() -> None:
    """Re-fetch the pinned URL and re-approve whatever is there now."""
    if not _require_native():
        return
    layout, store, outbox = _context()
    _context_doc, meta = store.load_context()
    profile = store.load_profile()
    url = (profile.base_context.url if profile and profile.base_context
           else (meta.url if meta else ""))
    if not url:
        print("\n  No base context is pinned. Use 'hermes pubky base set <url>'.\n")
        return
    print(f"\n  Refreshing {url}")
    _base_set(url)


def _base_clear() -> None:
    if not _require_native():
        return
    layout, store, outbox = _context()
    remote = _connect(layout)
    if remote is None:
        return
    if not _write_base_ref(remote, store, outbox, layout.profile_id, None):
        return
    store.clear_context()
    print("\n  ✓ Base context cleared.\n")


def _write_base_ref(remote: Remote, store: Store, outbox: Outbox,
                    profile_id: str, ref: Any) -> bool:
    """Update ``baseContext`` on the remote profile, preserving everything else."""
    try:
        profile = remote.fetch_profile(profile_id, 10.0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not read the remote profile: {cfg.redact(exc)}\n")
        return False

    if profile is None:
        profile = Profile(profile_id=profile_id)

    pending = outbox.load()
    if pending:
        print(
            f"\n  {len(pending)} local write(s) are still queued. "
            "Run 'hermes pubky sync' first so they are not lost.\n"
        )
        return False

    profile.base_context = ref
    profile.revision = (profile.revision or 0) + 1
    profile.updated_at = utcnow()
    try:
        remote.put_profile(profile, 15.0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not write the profile: {cfg.redact(exc)}\n")
        return False

    store.save_profile(profile)
    state = store.load_state()
    state.last_revision = profile.revision
    state.last_synced_at = profile.updated_at
    store.save_state(state)
    return True
