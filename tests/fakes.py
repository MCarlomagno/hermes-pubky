"""In-memory stand-ins for a homeserver.

The Python suite runs without a network: the transport adapters are the only
boundary the launcher crosses, so substituting them here covers every sync
path. Calls are recorded so tests can assert on ordering, and the failure hooks
drive the retry, conflict and recovery paths.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# /v2 protocol
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402
from typing import Callable, List, Optional  # noqa: E402

from hermes_pubky.models import Head, Snapshot, SnapshotRef  # noqa: E402
from hermes_pubky.objects import hash_bytes  # noqa: E402


class Boom(Exception):
    """A deliberate transport failure, injected at a named boundary."""


class LostResponse(Exception):
    """The write landed but the answer never arrived."""


class FakeAgentRemote:
    """A deterministic stand-in for `AgentRemote`, with failure injection.

    `fail_at` names a boundary to break exactly once, so a test can restart the
    engine and prove recovery is idempotent. `lose_at` performs the write and
    then raises, which is the case that must not be read as data loss.
    """

    def __init__(self, agent_id: str = "default") -> None:
        self.agent_id = agent_id
        self.head: Optional[bytes] = None
        self.snapshots: dict = {}
        self.objects: dict = {}
        self.calls: List[str] = []
        self.fail_at: Optional[str] = None
        self.lose_at: Optional[str] = None
        self.fail_with: Callable[[], Exception] = lambda: Boom("injected failure")

    # -- injection ---------------------------------------------------------

    def _maybe_fail(self, boundary: str) -> None:
        if self.fail_at == boundary:
            self.fail_at = None
            raise self.fail_with()
        # Recorded only when the call actually happens, so a failed attempt is
        # not mistaken for a completed transfer.
        self.calls.append(boundary)

    def _maybe_lose(self, boundary: str) -> None:
        if self.lose_at == boundary:
            self.lose_at = None
            raise LostResponse(f"response lost after {boundary}")

    # -- protocol ----------------------------------------------------------

    def read_head(self) -> Optional[Head]:
        self._maybe_fail("read_head")
        return Head.parse(self.head) if self.head else None

    def write_head(self, head: Head) -> None:
        self._maybe_fail("write_head")
        self.head = head.to_bytes()
        self._maybe_lose("write_head")

    def read_snapshot(self, ref: SnapshotRef) -> Snapshot:
        self._maybe_fail("read_snapshot")
        return Snapshot.parse(self.snapshots[ref.snapshot_id])

    def put_snapshot(self, snapshot: Snapshot) -> SnapshotRef:
        self._maybe_fail("put_snapshot")
        body = snapshot.to_bytes()
        self.snapshots[snapshot.snapshot_id] = body
        self._maybe_lose("put_snapshot")
        return SnapshotRef(snapshot_id=snapshot.snapshot_id, sha256=hash_bytes(body))

    def put_object_from_path(self, piece, source: Path) -> int:
        self._maybe_fail(f"put_object:{piece.object}")
        data = Path(source).read_bytes()
        self.objects[piece.object] = data
        self._maybe_lose(f"put_object:{piece.object}")
        return len(data)

    # -- helpers for assertions -------------------------------------------

    def set_head(self, snapshot: Snapshot) -> SnapshotRef:
        """Place a head as if another machine had published it."""
        body = snapshot.to_bytes()
        self.snapshots[snapshot.snapshot_id] = body
        ref = SnapshotRef(snapshot_id=snapshot.snapshot_id, sha256=hash_bytes(body))
        self.head = Head(kind=Head.AGENT, id=snapshot.agent_id,
                         snapshot_id=ref.snapshot_id, sha256=ref.sha256).to_bytes()
        return ref

    def order_of(self, *prefixes: str) -> List[str]:
        """The observed call order, filtered to the boundaries a test cares about."""
        return [c for c in self.calls if c.startswith(prefixes)]
