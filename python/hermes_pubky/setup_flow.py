"""The ``hermes memory setup pubky`` wizard.

Hermes delegates the whole flow to ``post_setup``, so this module owns
everything: profile choice, Pubky Auth, storing the grant, creating or
adopting the remote profile, optional base-context pinning, optional import of
existing local memory, and finally activation.

Nothing is destructive. Local ``USER.md`` / ``MEMORY.md`` are only ever read.
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import config as cfg
from .outbox import utcnow
from .paths import Layout, hermes_home
from .prompt import normalize_entry
from .remote import Remote, fetch_public_context, native_available, parse_context_url
from .schema import (
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    BaseContext,
    BaseContextRef,
    Profile,
)
from .store import Store

# How long to wait for the user to approve in Pubky Ring.
APPROVAL_TIMEOUT = 300.0


def _out(text: str = "") -> None:
    print(text)


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        sys.stdout.write(f"  {label}{suffix}: ")
        sys.stdout.flush()
        value = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return value or default


def _confirm(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _ask(f"{label} ({hint})").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def run_setup(hermes_home_path: str, config: Dict[str, Any]) -> None:
    """Entry point invoked by Hermes' memory-setup command."""
    _out("\n  Pubky portable context setup\n" + "  " + "─" * 44)

    if not native_available():
        _out(
            "\n  The native extension is missing. Reinstall with:\n"
            "    uv pip install --force-reinstall hermes-pubky\n"
        )
        return

    home = hermes_home(hermes_home_path)

    # 1. Profile id
    profile_id = _ask("Profile id", cfg.DEFAULT_PROFILE_ID).strip()
    try:
        from . import _native  # type: ignore

        _native.validate_profile_id(profile_id)
    except Exception as exc:
        _out(f"\n  Invalid profile id: {cfg.redact(exc)}\n")
        return

    layout = Layout(home, profile_id)
    layout.ensure()
    store = Store(layout)

    # 2. Authorize
    secret = cfg.read_grant_secret(layout.env_file)
    if secret and _confirm("An existing grant was found. Reuse it?", True):
        # The secret may have come from the environment rather than the file
        # (a `hermes pubky login` in another shell, or an exported value).
        # Persist it either way, so "saved to .env" is always true and the
        # next session finds a grant.
        cfg.write_grant_secret(layout.env_file, secret)
    else:
        secret = authorize(layout)
        if not secret:
            _out("\n  Setup cancelled. Nothing was saved.\n")
            return

    # 3. Connect
    try:
        remote = Remote.connect(secret, 10.0)
    except Exception as exc:
        _out(f"\n  Could not use that grant: {cfg.redact(exc)}\n")
        return
    _out(f"\n  Signed in as {remote.public_key}")
    _out(f"  Capabilities: {', '.join(remote.capabilities) or '(none)'}")

    # 4. Load or create the remote profile
    try:
        profile = remote.fetch_profile(profile_id, 10.0)
    except Exception as exc:
        _out(f"\n  Could not read the remote profile: {cfg.redact(exc)}\n")
        return

    if profile is not None:
        _out(
            f"\n  Found an existing profile '{profile_id}' "
            f"(revision {profile.revision}, {len(profile.user)} user fact(s), "
            f"{len(profile.memory)} memory entr(ies))."
        )
        new_profile = False
    else:
        _out(f"\n  No profile '{profile_id}' yet — creating one.")
        profile = Profile(profile_id=profile_id)
        new_profile = True

    # 5. New profiles: offer a base context and an import
    if new_profile:
        _configure_base_context(profile, store)
        _offer_import(profile, layout)

    # 6. Persist
    profile.profile_id = profile_id
    profile.revision = (profile.revision or 0) + 1
    profile.updated_at = utcnow()
    try:
        remote.put_profile(profile, 15.0)
    except Exception as exc:
        _out(f"\n  Could not write the profile: {cfg.redact(exc)}\n")
        return

    store.save_profile(profile)
    state = store.load_state()
    state.last_revision = profile.revision
    state.last_synced_at = profile.updated_at
    state.conflict = False
    state.conflict_detail = ""
    store.save_state(state)

    # 7. Activate
    cfg.set_provider_config(config, {"profile_id": profile_id})
    cfg.activate(config)
    if not cfg.save_hermes_config(config):
        _out("\n  Warning: could not write config.yaml; activate manually with:")
        _out("    memory:\n      provider: pubky\n")

    _out(
        f"\n  Memory provider: pubky (profile '{profile_id}', "
        f"revision {profile.revision})"
    )
    _out("  Grant secret saved to .env (mode 0600)")
    _out("\n  Start a new session to activate.\n")


def authorize(layout: Layout) -> str:
    """Run Pubky Auth and store the resulting grant secret.

    Returns the secret, or an empty string if the user cancelled or the
    request was never approved.
    """
    from . import _native  # type: ignore

    try:
        flow = _native.AuthFlow()
    except Exception as exc:
        _out(f"\n  Could not start Pubky Auth: {cfg.redact(exc)}\n")
        return ""

    _out("\n  Authorize in Pubky Ring. This plugin is requesting only:")
    _out(f"    {flow.capabilities}")
    _out("\n  Authorization URL:\n")
    _out(f"    {flow.authorization_url}\n")
    try:
        webbrowser.open(flow.authorization_url)
    except Exception:
        pass

    _out("  Waiting for approval (Ctrl-C to cancel)...")
    try:
        secret = flow.await_approval(APPROVAL_TIMEOUT)
    except KeyboardInterrupt:
        return ""
    except Exception as exc:
        _out(f"\n  Authorization failed: {cfg.redact(exc)}\n")
        return ""

    cfg.write_grant_secret(layout.env_file, secret)
    _out("  ✓ Authorized.")
    return secret


def _configure_base_context(profile: Profile, store: Store) -> None:
    """Optionally pin a public base context onto a new profile."""
    if not _confirm("\n  Pin a public base context?", False):
        return
    url = _ask("Base context pubky:// URL").strip()
    if not url:
        return
    try:
        ref, context, raw, digest = resolve_base_context(url)
    except Exception as exc:
        _out(f"  Could not use that context: {cfg.redact(exc)}\n")
        return

    _out(f"\n    id:          {context.id}")
    _out(f"    name:        {context.name or '(unnamed)'}")
    _out(f"    description: {context.description or '(none)'}")
    _out(f"    size:        {len(raw)} bytes")
    _out(f"    sha256:      {digest}")
    if not _confirm("\n  Approve and pin this context?", True):
        return
    profile.base_context = ref
    store.save_context(raw, ref.url, digest)
    _out("  ✓ Pinned.")


def resolve_base_context(url: str) -> Tuple[BaseContextRef, BaseContext, bytes, str]:
    """Fetch, validate and hash a public base context."""
    _author, _path, normalized = parse_context_url(url)
    raw, digest = fetch_public_context(normalized, 10.0)
    context = BaseContext.parse_bytes(raw)
    return BaseContextRef(url=normalized, sha256=digest), context, raw, digest


def _offer_import(profile: Profile, layout: Layout) -> None:
    """Offer to copy existing local memory into the new portable profile."""
    candidates = [
        ("user", layout.user_md, profile.user),
        ("memory", layout.memory_md, profile.memory),
    ]
    available = [(target, path, dest) for target, path, dest in candidates if path.exists()]
    if not available:
        return

    _out("\n  Existing local context files:")
    previews: List[Tuple[str, Path, List[str], List[str]]] = []
    for target, path, dest in available:
        entries = read_local_entries(path)
        size = path.stat().st_size
        _out(f"    {path.name:<12} {len(entries):>4} entr(ies)  {size:>7} bytes")
        previews.append((target, path, dest, entries))

    _out(
        "\n  Importing copies these entries into your private Pubky profile.\n"
        "  The local files are never modified or deleted."
    )
    if not _confirm("  Import them?", False):
        return

    for target, path, dest, entries in previews:
        if not entries:
            continue
        added = 0
        for entry in entries:
            if len(dest) >= MAX_ENTRIES:
                break
            if entry not in dest:
                dest.append(entry)
                added += 1
        _out(f"    imported {added} entr(ies) from {path.name}")


def read_local_entries(path: Path) -> List[str]:
    """Extract bullet-style entries from a Hermes memory file.

    Hermes writes these as markdown lists; anything that is not a list item
    (headings, prose) is left behind rather than guessed at.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return []

    entries: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(("- ", "* ", "+ ")):
            continue
        entry = normalize_entry(stripped)
        if entry and len(entry) <= MAX_ENTRY_CHARS and entry not in entries:
            entries.append(entry)
        if len(entries) >= MAX_ENTRIES:
            break
    return entries
