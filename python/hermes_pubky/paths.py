"""Local layout for managed agents.

Local state is partitioned by network, owner and agent id, so the same agent id
under two identities or two testnets never shares a journal, a cache or a
grant. Nothing here reaches the network.

The management root is never placed inside a managed workspace: the workspace
is scanned for upload, and a journal or credential file inside it would be a
candidate for publication.

Reference: implementation plan section 4.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

# Environment the launcher reads.
ROOT_ENV = "HERMES_PUBKY_HOME"
# Environment the launcher sets for its child.
MANAGED_ENV = "HERMES_PUBKY_MANAGED"
CONNECTION_ENV = "HERMES_PUBKY_CONNECTION"
GRANT_ENV = "HERMES_PUBKY_GRANT_SECRET"
HERMES_HOME_ENV = "HERMES_HOME"
TERMINAL_CWD_ENV = "TERMINAL_CWD"

# Network partitions. Different testnet instances must use separate roots.
NETWORK_MAINNET = "mainnet"
NETWORK_TESTNET = "testnet"
NETWORKS = (NETWORK_MAINNET, NETWORK_TESTNET)

PROVIDER_NAME = "pubky"

DIR_MODE = 0o700
FILE_MODE = 0o600


def default_root() -> Path:
    """The management root. Never inside a managed workspace."""
    override = os.environ.get(ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".hermes-pubky"


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(DIR_MODE)
    except OSError:
        pass
    return path


def write_private(path: Path, data: bytes) -> None:
    """Write atomically at 0600, so a torn file never looks like real content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, FILE_MODE)
    except OSError:
        pass
    os.replace(temp, path)


def read_optional(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except OSError:
        return None


@dataclass(frozen=True)
class Layout:
    """Every path belonging to one (network, owner, agent) connection."""

    root: Path
    network: str
    owner: str
    agent_id: str

    # -- connection --------------------------------------------------------

    @property
    def base(self) -> Path:
        return (self.root / "networks" / self.network / self.owner
                / "agents" / self.agent_id)

    @property
    def connection_file(self) -> Path:
        return self.base / "connection.json"

    @property
    def credentials_file(self) -> Path:
        """The scoped Pubky grant. Local only, never uploaded."""
        return self.base / "credentials.env"

    @property
    def device_config_file(self) -> Path:
        return self.base / "device-config.yaml"

    @property
    def secrets_file(self) -> Path:
        """Model and tool credentials the user configures per machine."""
        return self.base / "secrets.env"

    @property
    def journal_file(self) -> Path:
        return self.base / "journal.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.base / "run.lock"

    # -- cache -------------------------------------------------------------

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def cached_head(self) -> Path:
        return self.cache / "head.json"

    @property
    def cached_snapshots(self) -> Path:
        return self.cache / "snapshots"

    def cached_snapshot(self, snapshot_id: str) -> Path:
        return self.cached_snapshots / f"{snapshot_id}.json"

    @property
    def cached_objects(self) -> Path:
        return self.cache / "objects"

    # -- pending and recovery ---------------------------------------------

    @property
    def pending(self) -> Path:
        """Sealed candidates not yet acknowledged remotely. Not disposable."""
        return self.base / "pending"

    def pending_checkpoint(self, checkpoint_id: str) -> Path:
        return self.pending / checkpoint_id

    @property
    def recovery(self) -> Path:
        """Preserved conflict and crash candidates."""
        return self.base / "recovery"

    # -- the child's working copy -----------------------------------------

    @property
    def runtime(self) -> Path:
        return self.base / "runtime"

    @property
    def hermes_home(self) -> Path:
        """The dedicated HERMES_HOME. Never the user's ordinary profile."""
        return self.runtime / "home"

    @property
    def workspace(self) -> Path:
        return self.runtime / "workspace"

    @property
    def soul_file(self) -> Path:
        return self.hermes_home / "SOUL.md"

    @property
    def memories_dir(self) -> Path:
        # Hermes 0.19 keeps these in a subdirectory, not at the home root.
        return self.hermes_home / "memories"

    @property
    def user_memory_file(self) -> Path:
        return self.memories_dir / "USER.md"

    @property
    def agent_memory_file(self) -> Path:
        return self.memories_dir / "MEMORY.md"

    @property
    def skills_dir(self) -> Path:
        return self.hermes_home / "skills"

    @property
    def hermes_config_file(self) -> Path:
        """Generated. Kept separate from the portable source settings."""
        return self.hermes_home / "config.yaml"

    @property
    def state_db(self) -> Path:
        return self.hermes_home / "state.db"

    @property
    def plugin_dir(self) -> Path:
        """Where the generated Hermes discovery bridge is written."""
        return self.hermes_home / "plugins" / PROVIDER_NAME

    @property
    def agents_md(self) -> Path:
        return self.workspace / "AGENTS.md"

    # -- staging -----------------------------------------------------------

    @property
    def staging(self) -> Path:
        """Scratch space for a capture in progress."""
        return self.base / "staging"

    def ensure(self) -> "Layout":
        """Create the directories a run needs, owner-only."""
        for path in (self.base, self.cache, self.cached_snapshots,
                     self.cached_objects, self.pending, self.recovery,
                     self.runtime, self.hermes_home, self.memories_dir,
                     self.skills_dir, self.workspace, self.staging):
            secure_dir(path)
        return self

    def assert_outside_workspace(self) -> None:
        """Refuse a root that some agent's capture would try to upload.

        The danger is a management root nested inside a managed workspace: that
        workspace is scanned for upload, so a journal or credential file inside
        it would become a publication candidate. Checked against this layout's
        own workspace and every other connection known locally.
        """
        root = self.root.resolve()

        # Connections under this root can be checked exactly.
        for other in iter_connections(self.root):
            if other.base == self.base:
                continue
            resolved = other.workspace.resolve()
            if root == resolved or resolved in root.parents:
                raise ValueError(
                    f"the management root {root} is inside the managed workspace "
                    f"{resolved}; move it with {ROOT_ENV}")

        # A root under a *different* management root cannot be discovered, so
        # fall back to the layout's own signature: `.../runtime/workspace`.
        for candidate in (root, *root.parents):
            if candidate.name == "workspace" and candidate.parent.name == "runtime":
                raise ValueError(
                    f"the management root {root} is inside what looks like a "
                    f"managed workspace ({candidate}); a capture there would "
                    f"try to upload this agent's journal and credentials. "
                    f"Move it with {ROOT_ENV}")

    # -- child environment -------------------------------------------------

    def child_environment(self, base: Optional[dict] = None) -> dict:
        """The environment for the Hermes child process.

        `HERMES_HOME` must be set before Hermes is imported, because
        `hermes_state.DEFAULT_DB_PATH` is bound at module import time. The
        grant is deliberately removed: the supervisor owns all remote I/O.
        """
        env = dict(os.environ if base is None else base)
        env[HERMES_HOME_ENV] = str(self.hermes_home)
        env[TERMINAL_CWD_ENV] = str(self.workspace)
        env[MANAGED_ENV] = "1"
        env[CONNECTION_ENV] = str(self.connection_file)
        env.pop(GRANT_ENV, None)
        # A stale root override would send the child's own tooling elsewhere.
        env.pop(ROOT_ENV, None)
        return env


def layout_for(owner: str, agent_id: str, *, network: str = NETWORK_MAINNET,
               root: Optional[Path] = None) -> Layout:
    if network not in NETWORKS:
        raise ValueError(f"unknown network {network!r}; expected one of {NETWORKS}")
    return Layout(root=root or default_root(), network=network, owner=owner,
                  agent_id=agent_id)


def iter_connections(root: Optional[Path] = None) -> Iterator[Layout]:
    """Every locally known connection, for `agent list`.

    A directory scan, not a remote query: listing agents must not need broad
    permission to read a private directory tree.
    """
    base = (root or default_root()) / "networks"
    if not base.is_dir():
        return
    for network_dir in sorted(base.iterdir()):
        if not network_dir.is_dir() or network_dir.name not in NETWORKS:
            continue
        for owner_dir in sorted(network_dir.iterdir()):
            if not owner_dir.is_dir():
                continue
            agents = owner_dir / "agents"
            if not agents.is_dir():
                continue
            for agent_dir in sorted(agents.iterdir()):
                if (agent_dir / "connection.json").is_file():
                    yield Layout(root=root or default_root(),
                                 network=network_dir.name,
                                 owner=owner_dir.name,
                                 agent_id=agent_dir.name)


def template_dir(owner: str, template_id: str, *, network: str = NETWORK_MAINNET,
                 root: Optional[Path] = None) -> Path:
    """Where a publication receipt and its grant live.

    Outside every scanned agent workspace, so a publishing credential is never
    a candidate for upload.
    """
    return ((root or default_root()) / "networks" / network / owner
            / "templates" / template_id)


@dataclass(frozen=True)
class DeviceIdentity:
    """This machine's stable id, recorded in every snapshot it produces."""

    device_id: str

    @staticmethod
    def load_or_create(root: Optional[Path] = None) -> "DeviceIdentity":
        import json

        from .journal import new_id

        base = secure_dir(root or default_root())
        path = base / "device.json"
        raw = read_optional(path)
        if raw is not None:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                device_id = parsed.get("deviceId")
                if isinstance(device_id, str) and len(device_id) == 32:
                    return DeviceIdentity(device_id=device_id)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        identity = DeviceIdentity(device_id=new_id())
        write_private(path, json.dumps(
            {"deviceId": identity.device_id}, indent=2).encode("utf-8"))
        return identity


def read_env_file(path: Path) -> dict:
    """Parse a `KEY=value` file, ignoring comments and blanks."""
    values: dict = {}
    raw = read_optional(path)
    if raw is None:
        return values
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def write_env_file(path: Path, values: dict) -> None:
    """Write a `KEY=value` file at 0600, replacing its contents."""
    body = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    write_private(path, body.encode("utf-8"))


def is_managed_child() -> bool:
    """True inside a Hermes process the launcher started."""
    return os.environ.get(MANAGED_ENV) == "1"


def managed_connection_file() -> Optional[Path]:
    """The connection this child belongs to, or None when unmanaged."""
    value = os.environ.get(CONNECTION_ENV, "").strip()
    if not value:
        return None
    return Path(value)


def dir_is_owner_only(path: Path) -> bool:
    try:
        return stat.S_IMODE(path.stat().st_mode) == DIR_MODE
    except OSError:
        return False


def executable_bit(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o100)
    except OSError:
        return False


def relative_logical_paths(base: Path, paths: List[Path]) -> List[str]:
    """Turn absolute paths under `base` into POSIX logical paths."""
    out: List[str] = []
    resolved_base = base.resolve()
    for path in paths:
        relative = path.resolve().relative_to(resolved_base)
        out.append(relative.as_posix())
    return out
