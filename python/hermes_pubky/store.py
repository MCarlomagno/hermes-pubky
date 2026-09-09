"""Local cache and sync bookkeeping.

The cache exists so Hermes starts normally when the network or homeserver is
unavailable: startup reads the cache first and only then tries to refresh. The
state file records what the last successful sync saw, which is what makes
conflict detection possible.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .paths import Layout, write_private_file
from .schema import BaseContext, Profile, SchemaError


@dataclass
class SyncState:
    """What the last successful sync observed."""

    last_revision: int = 0
    last_synced_at: str = ""
    # Set when the remote moved while local writes were pending. While this is
    # set, automatic syncing stops and the user must choose a side.
    conflict: bool = False
    conflict_detail: str = ""
    conflict_remote_revision: int = 0

    @staticmethod
    def load(path: Path) -> "SyncState":
        raw = _read_json(path)
        if not isinstance(raw, dict):
            return SyncState()
        try:
            return SyncState(
                last_revision=int(raw.get("last_revision") or 0),
                last_synced_at=str(raw.get("last_synced_at") or ""),
                conflict=bool(raw.get("conflict") or False),
                conflict_detail=str(raw.get("conflict_detail") or ""),
                conflict_remote_revision=int(raw.get("conflict_remote_revision") or 0),
            )
        except (TypeError, ValueError):
            return SyncState()

    def save(self, path: Path) -> None:
        write_private_file(
            path, json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8")
        )


@dataclass(frozen=True)
class ContextMeta:
    """Provenance of the cached base context."""

    url: str
    sha256: str
    approved_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "sha256": self.sha256, "approvedAt": self.approved_at}


class Store:
    """Reads and writes everything under the profile's cache directory."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    # -- private profile ---------------------------------------------------

    def load_profile(self) -> Optional[Profile]:
        """Return the cached profile, or None if absent or unreadable.

        A cache that fails to parse is treated as absent: the remote copy is
        canonical, and refusing to start over a stale local file would be
        worse than re-fetching.
        """
        raw = _read_bytes(self.layout.profile_cache)
        if raw is None:
            return None
        try:
            return Profile.parse_bytes(raw)
        except SchemaError:
            return None

    def save_profile(self, profile: Profile) -> None:
        write_private_file(self.layout.profile_cache, profile.to_bytes())

    # -- public base context ----------------------------------------------

    def load_context(self) -> Tuple[Optional[BaseContext], Optional[ContextMeta]]:
        """Return the approved base context and its provenance."""
        meta = self.load_context_meta()
        raw = _read_bytes(self.layout.context_cache)
        if raw is None or meta is None:
            return None, meta
        try:
            return BaseContext.parse_bytes(raw), meta
        except SchemaError:
            return None, meta

    def load_context_bytes(self) -> Optional[bytes]:
        return _read_bytes(self.layout.context_cache)

    def load_context_meta(self) -> Optional[ContextMeta]:
        raw = _read_json(self.layout.context_meta)
        if not isinstance(raw, dict):
            return None
        url = raw.get("url")
        digest = raw.get("sha256")
        if not isinstance(url, str) or not isinstance(digest, str):
            return None
        return ContextMeta(
            url=url, sha256=digest, approved_at=str(raw.get("approvedAt") or "")
        )

    def save_context(self, raw: bytes, url: str, sha256: str) -> ContextMeta:
        meta = ContextMeta(url=url, sha256=sha256, approved_at=_now())
        write_private_file(self.layout.context_cache, raw)
        write_private_file(
            self.layout.context_meta,
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return meta

    def clear_context(self) -> None:
        for path in (self.layout.context_cache, self.layout.context_meta):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # -- sync state --------------------------------------------------------

    def load_state(self) -> SyncState:
        return SyncState.load(self.layout.state)

    def save_state(self, state: SyncState) -> None:
        state.save(self.layout.state)

    # -- conflict backups --------------------------------------------------

    def backup(self, label: str, data: bytes) -> Path:
        """Preserve a document before it is discarded by conflict resolution."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.layout.backups / f"{stamp}-{label}.json"
        write_private_file(path, data)
        return path


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
