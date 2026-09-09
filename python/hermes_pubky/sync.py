"""The checkpoint state machine: objects first, head last, verify then trust.

The homeserver exposes no conditional write and no multi-file transaction, so
durability here comes from ordering rather than from a lock. Immutable objects
and the snapshot go up before the head moves, and the head is re-read before
and after it is written. That prevents the active head from ever referencing an
incomplete upload *under the single-writer contract this release promises*. It
does not eliminate a genuine simultaneous-writer race, and nothing in this
module should be described as compare-and-swap.

Reference: implementation plan sections 6.3 and 6.4.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from .journal import (
    STATE_ACKNOWLEDGED,
    STATE_BLOCKED,
    STATE_CONFLICT,
    STATE_HEAD_WRITTEN,
    STATE_SNAPSHOT_WRITTEN,
    STATE_UPLOADING,
    Checkpoint,
    Journal,
    utcnow,
)
from .models import Head, SchemaError, Snapshot, SnapshotRef
from .objects import IntegrityError, ObjectCache, hash_bytes

logger = logging.getLogger("hermes_pubky.sync")

# Retry window for transient failures (plan 6.3).
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 60.0


class Transient(Exception):
    """A failure worth retrying: connection, timeout, 429, retryable 5xx."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Fatal(Exception):
    """A failure that will keep failing until a human fixes something."""


class ConflictDetected(Exception):
    """The remote head moved while this machine had work pending."""


class RemoteProtocol(Protocol):
    """The subset of `AgentRemote` the state machine uses."""

    def read_head(self) -> Optional[Head]: ...
    def write_head(self, head: Head) -> None: ...
    def read_snapshot(self, ref: SnapshotRef) -> Snapshot: ...
    def put_snapshot(self, snapshot: Snapshot) -> SnapshotRef: ...
    def put_object_from_path(self, piece, source: Path) -> int: ...


@dataclass
class SyncResult:
    """What one publication attempt did."""

    status: str  # synced | up-to-date | conflict | blocked | retry
    detail: str = ""
    checkpoint_id: str = ""
    snapshot_id: str = ""
    uploaded_objects: int = 0
    remaining: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("synced", "up-to-date")


def classify(exc: BaseException) -> Exception:
    """Split a failure into retryable and not.

    Auth, capability, quota and validation problems stop automatic retries:
    repeating them burns the backoff budget and hides something the user has to
    correct. Everything network-shaped is transient.
    """
    if isinstance(exc, (Transient, Fatal, ConflictDetected)):
        return exc  # type: ignore[return-value]
    if isinstance(exc, (SchemaError, IntegrityError, ValueError)):
        return Fatal(str(exc))

    name = type(exc).__name__
    message = str(exc)
    # Native exception names, matched without importing the extension so this
    # stays testable with a fake remote.
    if name in ("PubkyAuthError", "PubkyValidationError", "PubkyTooLargeError"):
        return Fatal(message)
    if name in ("PubkyTimeoutError", "PubkyNetworkError"):
        return Transient(message)
    if "429" in message or "Retry-After" in message:
        return Transient(message)
    if "quota" in message.lower() or " 413" in message:
        return Fatal(message)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return Transient(message)
    return Transient(message)


def backoff_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Exponential backoff with jitter, honoring a server's Retry-After."""
    if retry_after is not None:
        return max(0.0, min(retry_after, MAX_BACKOFF))
    base = min(INITIAL_BACKOFF * (2 ** max(0, attempt - 1)), MAX_BACKOFF)
    return base * (0.5 + random.random() / 2)


class SyncEngine:
    """Publishes sealed checkpoints, one at a time, in order."""

    def __init__(
        self,
        journal: Journal,
        remote: RemoteProtocol,
        cache: ObjectCache,
        *,
        recovery_dir: Optional[Path] = None,
    ) -> None:
        self.journal = journal
        self.remote = remote
        self.cache = cache
        self.recovery_dir = recovery_dir

    # -- one attempt --------------------------------------------------------

    def sync(self, checkpoint: Optional[Checkpoint] = None) -> SyncResult:
        """Publish the oldest active checkpoint, or report there is nothing to do."""
        if self.journal.conflicted_checkpoints() and checkpoint is None:
            return SyncResult(
                status="conflict",
                detail="a conflicting checkpoint is unresolved; run "
                       "'hermes-pubky agent sync --prefer remote|local'",
            )
        candidate = checkpoint or self.journal.next_checkpoint()
        if candidate is None:
            return SyncResult(status="up-to-date", detail="nothing pending")

        try:
            return self._publish(candidate)
        except ConflictDetected as exc:
            self.journal.set_checkpoint_state(candidate.id, STATE_CONFLICT, str(exc))
            logger.warning("hermes-pubky: %s", exc)
            return SyncResult(status="conflict", detail=str(exc),
                              checkpoint_id=candidate.id)
        except Fatal as exc:
            self.journal.set_checkpoint_state(candidate.id, STATE_BLOCKED, str(exc))
            return SyncResult(status="blocked", detail=str(exc),
                              checkpoint_id=candidate.id)
        except Transient as exc:
            # Stay in whatever state we reached; the bytes are still staged.
            self.journal.set_checkpoint_state(candidate.id, candidate.state, str(exc))
            return SyncResult(status="retry", detail=str(exc),
                              checkpoint_id=candidate.id)

    def _publish(self, candidate: Checkpoint) -> SyncResult:
        snapshot = self._load_candidate(candidate)

        # 0. If the head already names this candidate, a previous attempt
        #    succeeded and only its response was lost. Acknowledge rather than
        #    reporting a conflict against our own write.
        existing = self._call(self.remote.read_head)
        if (existing is not None
                and existing.snapshot_id == snapshot.snapshot_id
                and existing.sha256 == candidate.snapshot_hash):
            self.journal.acknowledge_checkpoint(candidate.id)
            self.journal.set_setting("acknowledged_head", existing.to_dict())
            self.journal.set_setting("acknowledged_at", utcnow())
            return SyncResult(
                status="synced",
                detail=f"checkpoint {candidate.id[:8]} was already published; "
                       "a previous attempt lost only its response",
                checkpoint_id=candidate.id,
                snapshot_id=snapshot.snapshot_id,
                remaining=len(self.journal.active_checkpoints()),
            )

        # 1. The head must still be where this candidate was built from.
        self._require_expected_parent(candidate, phase="before upload")

        # 2. Immutable objects first. A missing object here can never be
        #    referenced by the active head, because the head moves last.
        uploaded = self._upload_objects(candidate)

        # 3. The snapshot document, whose stored digest we read back.
        self.journal.set_checkpoint_state(candidate.id, STATE_UPLOADING)
        ref = self._call(lambda: self.remote.put_snapshot(snapshot))
        if ref.sha256 != candidate.snapshot_hash:
            raise Fatal(
                f"stored snapshot hashes to {ref.sha256}, staged bytes say "
                f"{candidate.snapshot_hash}")
        self.journal.set_checkpoint_state(candidate.id, STATE_SNAPSHOT_WRITTEN)

        # 4. Re-read the head immediately before moving it. This narrows, but
        #    does not close, a simultaneous-writer window.
        self._require_expected_parent(candidate, phase="before head write")

        # 5. Move the head, then confirm what is actually there.
        head = Head(kind=Head.AGENT, id=snapshot.agent_id,
                    snapshot_id=snapshot.snapshot_id, sha256=ref.sha256)
        self._call(lambda: self.remote.write_head(head))
        self.journal.set_checkpoint_state(candidate.id, STATE_HEAD_WRITTEN)

        stored = self._call(self.remote.read_head)
        if stored is None:
            raise Transient("head disappeared immediately after being written")
        if (stored.snapshot_id, stored.sha256) != (head.snapshot_id, head.sha256):
            raise ConflictDetected(
                f"head now points at {stored.snapshot_id} rather than "
                f"{head.snapshot_id}; another writer intervened")

        # 6. Only now is the work durable elsewhere.
        self.journal.acknowledge_checkpoint(candidate.id)
        self.journal.set_setting("acknowledged_head", head.to_dict())
        self.journal.set_setting("acknowledged_at", utcnow())
        return SyncResult(
            status="synced",
            detail=f"checkpoint {candidate.id[:8]} is saved to the homeserver",
            checkpoint_id=candidate.id,
            snapshot_id=snapshot.snapshot_id,
            uploaded_objects=uploaded,
            remaining=len(self.journal.active_checkpoints()),
        )

    # -- steps --------------------------------------------------------------

    def _load_candidate(self, candidate: Checkpoint) -> Snapshot:
        """Read the sealed snapshot bytes back off disk and verify them."""
        path = Path(candidate.snapshot_path)
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise Fatal(
                f"sealed snapshot {path} is unreadable ({exc}); the checkpoint "
                "cannot be published and was not discarded") from exc
        if hash_bytes(body) != candidate.snapshot_hash:
            raise Fatal(f"sealed snapshot {path} no longer matches its recorded hash")
        return Snapshot.parse(body)

    def _require_expected_parent(self, candidate: Checkpoint, *, phase: str) -> None:
        head = self._call(self.remote.read_head)
        if head is None:
            if candidate.parent_snapshot_id is None:
                return  # first publication for this agent
            raise ConflictDetected(
                f"head is absent {phase} but this checkpoint expected parent "
                f"{candidate.parent_snapshot_id}")
        if candidate.parent_snapshot_id is None:
            raise ConflictDetected(
                f"head already exists ({head.snapshot_id}) {phase} but this "
                "checkpoint was built as the agent's first")
        if (head.snapshot_id != candidate.parent_snapshot_id
                or head.sha256 != candidate.parent_hash):
            raise ConflictDetected(
                f"remote head is {head.snapshot_id} {phase}, but this machine "
                f"built on {candidate.parent_snapshot_id}; local work is preserved")

    def _upload_objects(self, candidate: Checkpoint) -> int:
        """Upload every object this checkpoint still needs.

        After an uncertain outcome the object is re-verified rather than assumed
        present: an object name alone is not proof of remote integrity.
        """
        uploaded = 0
        for upload in self.journal.pending_uploads(candidate.id):
            source = Path(upload.local_path)
            if not source.is_file():
                cached = self.cache.path_for(upload.object_path)
                if not cached.is_file():
                    raise Fatal(
                        f"object {upload.object_path} is missing locally; the "
                        "checkpoint cannot be completed")
                source = cached
            piece = _piece_of(upload)
            self._call(lambda p=piece, s=source: self.remote.put_object_from_path(p, s))
            self.journal.mark_uploaded(candidate.id, upload.object_path)
            uploaded += 1
        return uploaded

    def _call(self, action: Callable[[], object]):
        """Run one remote call, translating failures into our two kinds."""
        try:
            return action()
        except BaseException as exc:  # noqa: BLE001 - re-raised as a typed error
            raise classify(exc) from exc

    # -- retry loop ---------------------------------------------------------

    def sync_with_retries(self, deadline: Optional[float] = None,
                          max_attempts: int = 8,
                          sleep: Callable[[float], None] = time.sleep,
                          now: Callable[[], float] = time.monotonic) -> SyncResult:
        """Attempt publication, backing off on transient failures.

        `deadline` is a monotonic timestamp covering the whole operation, not
        each object, so a shutdown budget cannot be extended by retrying.
        """
        result = SyncResult(status="up-to-date", detail="nothing pending")
        for attempt in range(1, max_attempts + 1):
            result = self.sync()
            if result.status != "retry":
                return result
            if deadline is not None and now() >= deadline:
                return SyncResult(
                    status="retry",
                    detail=f"{result.detail} (gave up at the deadline)",
                    checkpoint_id=result.checkpoint_id)
            delay = backoff_delay(attempt)
            if deadline is not None:
                remaining = deadline - now()
                if remaining <= 0:
                    return result
                delay = min(delay, remaining)
            sleep(delay)
        return result

    # -- conflict resolution ------------------------------------------------

    def resolve(self, prefer: str) -> SyncResult:
        """Resolve a conflict by keeping one side, after preserving both.

        Neither choice destroys the discarded side's recoverable bytes: the
        objects stay in the cache and a copy of the discarded document is
        written under `recovery/`.
        """
        if prefer not in ("remote", "local"):
            raise ValueError("prefer must be 'remote' or 'local'")
        conflicts = self.journal.conflicted_checkpoints()
        if not conflicts:
            return SyncResult(status="up-to-date", detail="no conflict to resolve")

        remote_head = self._call(self.remote.read_head)
        for candidate in conflicts:
            self._preserve(candidate, remote_head)

        if prefer == "remote":
            for candidate in conflicts:
                self.journal.set_checkpoint_state(
                    candidate.id, STATE_ACKNOWLEDGED,
                    "discarded in favour of the remote copy")
            if remote_head is not None:
                self.journal.set_setting("acknowledged_head", remote_head.to_dict())
            return SyncResult(
                status="synced",
                detail=f"kept the remote profile; {len(conflicts)} local "
                       "checkpoint(s) preserved under recovery/")

        # prefer == "local": rebase each candidate onto what the remote is now.
        newest = conflicts[-1]
        snapshot = self._load_candidate(newest)
        rebased = self._rebase(newest, snapshot, remote_head)
        for candidate in conflicts[:-1]:
            self.journal.set_checkpoint_state(
                candidate.id, STATE_ACKNOWLEDGED,
                "superseded by the rebased local checkpoint")
        return self.sync(checkpoint=rebased)

    def _rebase(self, candidate: Checkpoint, snapshot: Snapshot,
                remote_head: Optional[Head]) -> Checkpoint:
        """Re-seal a candidate so its parent is the head we just read."""
        snapshot.parent = (
            SnapshotRef(snapshot_id=remote_head.snapshot_id, sha256=remote_head.sha256)
            if remote_head else None)
        body = snapshot.to_bytes()
        path = Path(candidate.snapshot_path)
        rebased_path = path.with_name(path.name + ".rebased")
        rebased_path.write_bytes(body)

        rebased = self.journal.create_checkpoint(
            snapshot_path=str(rebased_path),
            snapshot_hash=hash_bytes(body),
            parent_snapshot_id=remote_head.snapshot_id if remote_head else None,
            parent_hash=remote_head.sha256 if remote_head else None,
        )
        # Carry the object list across; the objects themselves are unchanged.
        uploads = self.journal.all_uploads(candidate.id)
        for upload in uploads:
            upload.checkpoint_id = rebased.id
            upload.acknowledged = False
        self.journal.record_uploads(rebased.id, uploads)
        self.journal.set_checkpoint_state(
            candidate.id, STATE_ACKNOWLEDGED, "rebased onto the current remote head")
        return rebased

    def _preserve(self, candidate: Checkpoint, remote_head: Optional[Head]) -> None:
        """Write both sides of a conflict somewhere recoverable."""
        if self.recovery_dir is None:
            return
        target = self.recovery_dir / f"{candidate.created_at.replace(':', '')}-{candidate.id[:8]}"
        target.mkdir(parents=True, exist_ok=True)
        source = Path(candidate.snapshot_path)
        if source.is_file():
            (target / "local-snapshot.json").write_bytes(source.read_bytes())
        if remote_head is not None:
            (target / "remote-head.json").write_bytes(remote_head.to_bytes())
        (target / "checkpoint.json").write_text(
            json.dumps({
                "id": candidate.id,
                "state": candidate.state,
                "parentSnapshotId": candidate.parent_snapshot_id,
                "createdAt": candidate.created_at,
                "lastError": candidate.last_error,
                "objects": [u.object_path for u in self.journal.all_uploads(candidate.id)],
            }, indent=2, sort_keys=True), encoding="utf-8")


def _piece_of(upload):
    from .models import Piece

    return Piece(object=upload.object_path, sha256=upload.sha256, size=upload.size)
