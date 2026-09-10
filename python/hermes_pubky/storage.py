"""Transport adapters: the single I/O boundary for remote state.

Everything above this module works with typed manifests and local files. These
classes are the only place that talks to a homeserver, which is what lets the
checkpoint and projection logic be tested against a fake with no network.

Each adapter binds one root to one actor at construction: an `AgentRemote`
always carries a session scoped to that agent, and a `PublicTemplateRemote`
never carries a credential at all.

Reference: implementation plan sections 6.3 and 14.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Protocol, Tuple

from .models import (
    Head,
    Piece,
    SchemaError,
    Snapshot,
    SnapshotRef,
    TemplateSnapshot,
    require_hex32,
)
from .objects import IntegrityError, hash_file


class NativeUnavailable(RuntimeError):
    """The compiled extension could not be imported."""


def native() -> Any:
    try:
        from . import _native  # type: ignore

        return _native
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise NativeUnavailable(
            f"the hermes-pubky native extension is not available ({exc}). "
            "Reinstall with 'uv pip install --force-reinstall hermes-pubky'."
        ) from exc


def native_available() -> bool:
    try:
        native()
        return True
    except NativeUnavailable:
        return False


class SnapshotPage(Protocol):
    snapshot_ids: List[str]
    next_cursor: Optional[str]


class AgentRemote:
    """Authenticated access to one private agent root.

    Reads are verified before they are returned: a snapshot must hash to the
    digest its reference claims, and a downloaded object must hash to its
    piece. `write_head` is deliberately not called `compare_and_swap` -- the
    homeserver exposes no conditional write, and the caller is responsible for
    the read/compare/write sequence in `sync`.
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    @staticmethod
    def connect(secret: str, owner: str, agent_id: str,
                timeout_secs: float = 10.0) -> "AgentRemote":
        if not secret:
            raise ValueError("no grant available; run 'hermes-pubky agent login'")
        return AgentRemote(
            native().AgentTransport.open(secret, owner, agent_id, timeout_secs))

    @staticmethod
    def connect_uri(secret: str, uri: str, timeout_secs: float = 10.0) -> "AgentRemote":
        if not secret:
            raise ValueError("no grant available; run 'hermes-pubky agent login'")
        return AgentRemote(native().AgentTransport.from_uri(secret, uri, timeout_secs))

    # -- identity ----------------------------------------------------------

    @property
    def owner(self) -> str:
        return self._transport.owner

    @property
    def agent_id(self) -> str:
        return self._transport.agent_id

    @property
    def uri(self) -> str:
        return self._transport.uri

    @property
    def capability(self) -> str:
        return self._transport.capability

    @property
    def capabilities(self) -> List[str]:
        return list(self._transport.capabilities)

    # -- head --------------------------------------------------------------

    def read_head(self, timeout_secs: float = 10.0) -> Optional[Head]:
        """The current head, or None when this agent does not exist remotely."""
        raw = self._transport.head_get(timeout_secs)
        if raw is None:
            return None
        return Head.parse(bytes(raw))

    def write_head(self, head: Head, timeout_secs: float = 15.0) -> None:
        """Publish a head. Not a compare-and-swap; see `sync`."""
        if head.id != self.agent_id:
            raise SchemaError(
                f"refusing to write a head for {head.id!r} to agent {self.agent_id!r}")
        self._transport.head_put(head.to_bytes(), timeout_secs)

    # -- snapshots ---------------------------------------------------------

    def read_snapshot(self, ref: SnapshotRef, timeout_secs: float = 15.0) -> Snapshot:
        """Fetch and verify the snapshot a reference names."""
        raw = self._transport.snapshot_get(ref.snapshot_id, timeout_secs)
        if raw is None:
            raise IntegrityError(
                f"snapshot {ref.snapshot_id} is missing but the head references it")
        return _verified_snapshot(bytes(raw), ref)

    def put_snapshot(self, snapshot: Snapshot, timeout_secs: float = 20.0) -> SnapshotRef:
        """Upload a snapshot and read its digest back.

        Returns the reference a head may point at, computed from the bytes that
        were actually stored rather than from the bytes we intended to store.
        """
        body = snapshot.to_bytes()
        self._transport.snapshot_put(snapshot.snapshot_id, body, timeout_secs)
        stored = self._transport.snapshot_get(snapshot.snapshot_id, timeout_secs)
        if stored is None:
            raise IntegrityError("snapshot vanished immediately after upload")
        from .objects import hash_bytes

        if bytes(stored) != body:
            raise IntegrityError("stored snapshot bytes differ from what was uploaded")
        return SnapshotRef(snapshot_id=snapshot.snapshot_id, sha256=hash_bytes(body))

    def list_snapshots(self, cursor: Optional[str] = None, limit: int = 100,
                       timeout_secs: float = 20.0) -> Tuple[List[str], Optional[str]]:
        """One explicit page of snapshot ids. Routine sync never calls this."""
        ids, next_cursor = self._transport.list_snapshots(cursor, limit, timeout_secs)
        for snapshot_id in ids:
            require_hex32(snapshot_id, "snapshot id")
        return list(ids), next_cursor

    # -- objects -----------------------------------------------------------

    def read_object_to_path(self, piece: Piece, destination: Path,
                            timeout_secs: float = 30.0) -> int:
        """Download one object, verified, to `destination`.

        The native layer hashes while streaming and installs the file only on a
        match, so this cannot return a path holding partial bytes.
        """
        size = self._transport.object_get(piece.object, str(destination), timeout_secs)
        if size != piece.size:
            raise IntegrityError(
                f"{piece.object} is {size} bytes, manifest says {piece.size}")
        return size

    def put_object_from_path(self, piece: Piece, source: Path,
                             timeout_secs: float = 30.0) -> int:
        """Upload one object, refusing bytes that disagree with its reference."""
        digest, size = hash_file(source)
        if digest != piece.sha256 or size != piece.size:
            raise IntegrityError(
                f"{source} does not match {piece.object} "
                f"({digest}/{size} vs {piece.sha256}/{piece.size})")
        return self._transport.object_put(piece.object, str(source), timeout_secs)

    def revoke(self, timeout_secs: float = 10.0) -> None:
        """Revoke this grant at the homeserver. The remote is unusable after."""
        self._transport.revoke(timeout_secs)

    def __repr__(self) -> str:
        return f"<AgentRemote {self.owner}/{self.agent_id}>"


class PublicTemplateRemote:
    """Unauthenticated reads of a public template. Sends no credential."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    @staticmethod
    def open(owner: str, template_id: str) -> "PublicTemplateRemote":
        return PublicTemplateRemote(native().PublicTemplate.open(owner, template_id))

    @staticmethod
    def from_uri(uri: str) -> "PublicTemplateRemote":
        return PublicTemplateRemote(native().PublicTemplate.from_uri(uri))

    @property
    def owner(self) -> str:
        return self._transport.owner

    @property
    def template_id(self) -> str:
        return self._transport.template_id

    @property
    def uri(self) -> str:
        return self._transport.uri

    def read_head(self, timeout_secs: float = 10.0) -> Optional[Head]:
        raw = self._transport.head_get(timeout_secs)
        if raw is None:
            return None
        return Head.parse(bytes(raw), template=True)

    def read_snapshot(self, ref: SnapshotRef,
                      timeout_secs: float = 15.0) -> TemplateSnapshot:
        raw = self._transport.snapshot_get(ref.snapshot_id, timeout_secs)
        if raw is None:
            raise IntegrityError(
                f"template snapshot {ref.snapshot_id} is missing")
        from .objects import hash_bytes

        body = bytes(raw)
        if hash_bytes(body) != ref.sha256:
            raise IntegrityError(
                "template snapshot does not match the digest its head claims")
        return TemplateSnapshot.parse(body)

    def read_object_to_path(self, piece: Piece, destination: Path,
                            timeout_secs: float = 30.0) -> int:
        size = self._transport.object_get(piece.object, str(destination), timeout_secs)
        if size != piece.size:
            raise IntegrityError(
                f"{piece.object} is {size} bytes, manifest says {piece.size}")
        return size

    def __repr__(self) -> str:
        return f"<PublicTemplateRemote {self.owner}/{self.template_id}>"


class TemplatePublisher:
    """Authenticated writes to one public template root.

    Separate from `AgentRemote` so an agent's grant can never publish, and
    publishing requires its own explicitly requested capability.
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    @staticmethod
    def connect(secret: str, owner: str, template_id: str,
                timeout_secs: float = 10.0) -> "TemplatePublisher":
        if not secret:
            raise ValueError("publishing needs a template-scoped grant")
        return TemplatePublisher(
            native().TemplatePublisher.open(secret, owner, template_id, timeout_secs))

    @property
    def uri(self) -> str:
        return self._transport.uri

    @property
    def capability(self) -> str:
        return self._transport.capability

    def put_snapshot(self, snapshot: TemplateSnapshot,
                     timeout_secs: float = 20.0) -> SnapshotRef:
        from .objects import hash_bytes

        body = snapshot.to_bytes()
        self._transport.snapshot_put(snapshot.snapshot_id, body, timeout_secs)
        return SnapshotRef(snapshot_id=snapshot.snapshot_id, sha256=hash_bytes(body))

    def write_head(self, head: Head, timeout_secs: float = 15.0) -> None:
        if not head.is_template:
            raise SchemaError("refusing to publish a non-template head")
        self._transport.head_put(head.to_bytes(), timeout_secs)

    def put_object_from_path(self, piece: Piece, source: Path,
                             timeout_secs: float = 30.0) -> int:
        digest, size = hash_file(source)
        if digest != piece.sha256 or size != piece.size:
            raise IntegrityError(f"{source} does not match {piece.object}")
        return self._transport.object_put(piece.object, str(source), timeout_secs)

    def __repr__(self) -> str:
        return f"<TemplatePublisher {self.uri}>"


def _verified_snapshot(body: bytes, ref: SnapshotRef) -> Snapshot:
    from .objects import hash_bytes

    if hash_bytes(body) != ref.sha256:
        raise IntegrityError(
            f"snapshot {ref.snapshot_id} does not match the digest its head claims")
    snapshot = Snapshot.parse(body)
    if snapshot.snapshot_id != ref.snapshot_id:
        raise IntegrityError(
            f"snapshot document says {snapshot.snapshot_id}, head says {ref.snapshot_id}")
    return snapshot


# -- address helpers ---------------------------------------------------------

def agent_uri(owner: str, agent_id: str) -> str:
    return native().agent_uri(owner, agent_id)


def agent_capability(owner: str, agent_id: str) -> str:
    return native().agent_capability(owner, agent_id)


def parse_agent_uri(uri: str) -> Tuple[str, str]:
    return native().parse_agent_uri(uri)


def template_uri(owner: str, template_id: str) -> str:
    return native().template_uri(owner, template_id)


def template_capability(owner: str, template_id: str) -> str:
    return native().template_capability(owner, template_id)


def parse_template_uri(uri: str) -> Tuple[str, str]:
    return native().parse_template_uri(uri)
