"""Local, profile-scoped storage layout.

Everything lives under ``$HERMES_HOME/pubky/<profile-id>/`` so a Hermes
profile switch (which moves ``HERMES_HOME``) and a Pubky profile switch both
give a clean, separate cache. Nothing here reaches the network.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

# Name of the environment variable holding the grant secret.
GRANT_SECRET_ENV = "HERMES_PUBKY_GRANT_SECRET"

# Provider / plugin identity.
PROVIDER_NAME = "pubky"


def hermes_home(explicit: Optional[str] = None) -> Path:
    """Resolve the active HERMES_HOME.

    Prefers the value Hermes hands us, then Hermes' own resolver (which knows
    about profiles), then the documented default.
    """
    if explicit:
        return Path(explicit).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"


class Layout:
    """Filesystem locations for one (hermes_home, profile_id) pair."""

    def __init__(self, home: Path, profile_id: str) -> None:
        self.home = Path(home)
        self.profile_id = profile_id

    @property
    def root(self) -> Path:
        return self.home / "pubky" / self.profile_id

    @property
    def profile_cache(self) -> Path:
        """Last known good copy of the remote private profile."""
        return self.root / "profile.json"

    @property
    def context_cache(self) -> Path:
        """Raw bytes of the approved base context, exactly as downloaded."""
        return self.root / "context.json"

    @property
    def context_meta(self) -> Path:
        """URL + hash of the approved base context."""
        return self.root / "context.meta.json"

    @property
    def outbox(self) -> Path:
        """Append-only log of memory writes not yet mirrored remotely."""
        return self.root / "outbox.jsonl"

    @property
    def state(self) -> Path:
        """Sync bookkeeping: last synced revision, conflict flag."""
        return self.root / "state.json"

    @property
    def backups(self) -> Path:
        """Where a discarded side of a conflict is preserved."""
        return self.root / "backups"

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def user_md(self) -> Path:
        return self.home / "USER.md"

    @property
    def memory_md(self) -> Path:
        return self.home / "MEMORY.md"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict(self.root)


def _restrict(path: Path) -> None:
    """Make a directory owner-only. Best effort — Windows and odd filesystems
    simply keep their defaults."""
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def write_private_file(path: Path, data: bytes) -> None:
    """Write a file atomically with 0600 permissions.

    Atomic because a torn cache file would look like corruption on the next
    start; 0600 because these files hold the user's portable memory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, path)
