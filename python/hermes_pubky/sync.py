"""Reconciliation between the local outbox and the remote private profile.

The model is deliberately small, because v0.1 supports exactly one writer per
profile:

* With nothing queued, the remote profile is canonical and simply replaces the
  cache.
* With writes queued and the remote still at the revision we last synced, the
  queue is applied and pushed as ``revision + 1``.
* With writes queued and the remote moved underneath us, something else wrote
  to this profile. That is outside what v0.1 promises to merge, so automatic
  syncing stops and the user picks a side with ``--prefer remote|local``. The
  discarded side is always backed up first.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from .outbox import Operation, Outbox, apply_operations, utcnow
from .schema import Profile
from .store import Store, SyncState

logger = logging.getLogger("hermes_pubky.sync")

# Retry schedule for background pushes, capped so a long outage settles into
# a once-a-minute poll instead of hammering the homeserver.
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 60.0


class ConflictError(RuntimeError):
    """The remote profile moved while local writes were pending."""


@dataclass
class SyncResult:
    """What a sync attempt did, for the CLI and for logging."""

    status: str  # "synced" | "up-to-date" | "conflict" | "skipped" | "failed"
    detail: str = ""
    pushed: int = 0
    revision: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("synced", "up-to-date")


class Syncer:
    """Applies the outbox to the remote profile and maintains sync state."""

    def __init__(
        self,
        store: Store,
        outbox: Outbox,
        profile_id: str,
        connect: Callable[[], "object"],
    ) -> None:
        self.store = store
        self.outbox = outbox
        self.profile_id = profile_id
        # A factory rather than a live session: a session can expire between
        # syncs, and reconnecting is cheap next to the network round trip.
        self._connect = connect
        self._lock = threading.Lock()

    # -- core ---------------------------------------------------------------

    def sync(self, prefer: Optional[str] = None, timeout_secs: float = 10.0) -> SyncResult:
        """Run one reconciliation pass.

        ``prefer`` is ``"remote"`` or ``"local"`` and is only meaningful when a
        conflict is recorded; it resolves the conflict and clears the flag.
        """
        with self._lock:
            return self._sync_locked(prefer=prefer, timeout_secs=timeout_secs)

    def _sync_locked(self, prefer: Optional[str], timeout_secs: float) -> SyncResult:
        state = self.store.load_state()
        pending = self.outbox.load()

        if state.conflict and prefer is None:
            return SyncResult(
                status="conflict",
                detail=(
                    "the remote profile changed while local writes were pending; "
                    "resolve with 'hermes pubky sync --prefer remote' or "
                    "'hermes pubky sync --prefer local'"
                ),
                pushed=len(pending),
                revision=state.last_revision,
            )

        remote = self._connect()
        remote_profile = remote.fetch_profile(self.profile_id, timeout_secs)

        if prefer is not None:
            return self._resolve(prefer, remote, remote_profile, pending, state, timeout_secs)

        if not pending:
            return self._adopt_remote(remote_profile, state)

        if remote_profile is not None and remote_profile.revision != state.last_revision:
            return self._record_conflict(remote_profile, state, len(pending))

        return self._push(remote, remote_profile, pending, state, timeout_secs)

    def _retire(self, handled: List[Operation]) -> int:
        """Drop the handled operations from the queue, keeping any newcomers.

        A write mirrored while the push was in flight is already on disk but
        was not part of this push. Clearing the whole file would delete it
        before it ever reached the homeserver, so the queue is rewritten to
        exactly the operations that arrived late. Returns how many remain.
        """
        handled_ids = {op.op_id for op in handled}
        remaining = [op for op in self.outbox.load() if op.op_id not in handled_ids]
        self.outbox.replace(remaining)
        return len(remaining)

    # -- outcomes -----------------------------------------------------------

    def _adopt_remote(self, remote_profile: Optional[Profile], state: SyncState) -> SyncResult:
        """Nothing queued: take the remote copy as canonical."""
        if remote_profile is None:
            return SyncResult(
                status="up-to-date",
                detail="no remote profile yet",
                revision=state.last_revision,
            )
        self.store.save_profile(remote_profile)
        state.last_revision = remote_profile.revision
        state.last_synced_at = utcnow()
        state.conflict = False
        state.conflict_detail = ""
        state.conflict_remote_revision = 0
        self.store.save_state(state)
        return SyncResult(
            status="up-to-date",
            detail="remote profile adopted",
            revision=remote_profile.revision,
        )

    def _record_conflict(
        self, remote_profile: Profile, state: SyncState, pending_count: int
    ) -> SyncResult:
        detail = (
            f"remote profile is at revision {remote_profile.revision} but the last "
            f"sync from this machine saw {state.last_revision}, and "
            f"{pending_count} local write(s) are still pending"
        )
        state.conflict = True
        state.conflict_detail = detail
        state.conflict_remote_revision = remote_profile.revision
        self.store.save_state(state)
        logger.warning("hermes-pubky: %s", detail)
        return SyncResult(
            status="conflict", detail=detail, pushed=pending_count,
            revision=state.last_revision,
        )

    def _push(
        self,
        remote: object,
        remote_profile: Optional[Profile],
        pending: List[Operation],
        state: SyncState,
        timeout_secs: float,
        base: Optional[Profile] = None,
    ) -> SyncResult:
        """Apply the queue to ``base`` and push it as the next revision.

        ``base`` defaults to the remote profile, which is right for the normal
        path: the remote has not moved, so replaying the queue on top of it
        loses nothing. Conflict resolution passes an explicit base when the
        user has chosen which side to keep.
        """
        if base is None:
            base = (
                remote_profile
                or self.store.load_profile()
                or Profile(profile_id=self.profile_id)
            )
            # Build from the remote's own pin so a base context set on another
            # machine is not clobbered by this machine's stale cache.
            if remote_profile is not None:
                base.base_context = remote_profile.base_context

        applied = apply_operations(base, pending)
        base.profile_id = self.profile_id
        # Always advance past the remote, or the homeserver keeps the old copy.
        base.revision = (remote_profile.revision if remote_profile else 0) + 1
        base.updated_at = utcnow()

        remote.put_profile(base, timeout_secs)  # type: ignore[attr-defined]

        self.store.save_profile(base)
        left = self._retire(pending)
        state.last_revision = base.revision
        state.last_synced_at = base.updated_at
        state.conflict = False
        state.conflict_detail = ""
        state.conflict_remote_revision = 0
        self.store.save_state(state)
        detail = f"{applied} of {len(pending)} queued write(s) changed the profile"
        if left:
            # Writes that landed mid-push are still queued; go round again
            # rather than leaving them for the next trigger.
            detail += f"; {left} arrived during the push and are still queued"
        return SyncResult(
            status="synced",
            detail=detail,
            pushed=len(pending),
            revision=base.revision,
        )

    def _resolve(
        self,
        prefer: str,
        remote: object,
        remote_profile: Optional[Profile],
        pending: List[Operation],
        state: SyncState,
        timeout_secs: float,
    ) -> SyncResult:
        """Resolve a conflict by discarding one side, after backing it up."""
        if prefer not in ("remote", "local"):
            raise ValueError("prefer must be 'remote' or 'local'")

        if prefer == "remote":
            # Discarding local: preserve both the queue and the cached profile,
            # so nothing the user wrote is unrecoverable.
            local_profile = self.store.load_profile()
            if local_profile is not None:
                self.store.backup("local-profile", local_profile.to_bytes())
            if pending:
                import json

                queued = json.dumps(
                    [op.to_dict() for op in pending], ensure_ascii=False, indent=2
                ).encode("utf-8")
                self.store.backup("local-outbox", queued)
            self._retire(pending)
            result = self._adopt_remote(remote_profile, state)
            return SyncResult(
                status="synced" if result.ok else result.status,
                detail=f"kept the remote profile; {len(pending)} local write(s) backed up",
                pushed=0,
                revision=result.revision,
            )

        # prefer == "local": this machine's view wins and the remote copy is
        # discarded, mirroring what "remote" does to the local side.
        if remote_profile is not None:
            self.store.backup("remote-profile", remote_profile.to_bytes())

        local_base = self.store.load_profile() or Profile(profile_id=self.profile_id)
        # A pin only this machine knows about would otherwise be lost; fall
        # back to the remote's pin when the local cache has none.
        if local_base.base_context is None and remote_profile is not None:
            local_base.base_context = remote_profile.base_context

        state.conflict = False
        state.conflict_detail = ""
        state.conflict_remote_revision = 0
        result = self._push(
            remote, remote_profile, pending, state, timeout_secs, base=local_base
        )
        return SyncResult(
            status=result.status,
            detail="kept this machine's profile; the remote copy was backed up first",
            pushed=result.pushed,
            revision=result.revision,
        )


class BackgroundSyncer:
    """Runs :class:`Syncer` off the agent's critical path.

    Memory writes must never block a turn, and a homeserver that is slow or
    down must never be felt by the user, so pushes happen on a daemon thread
    with exponential backoff.
    """

    def __init__(self, syncer: Syncer) -> None:
        self.syncer = syncer
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last: Optional[SyncResult] = None
        self._backoff = INITIAL_BACKOFF

    @property
    def last_result(self) -> Optional[SyncResult]:
        return self._last

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hermes-pubky-sync", daemon=True
        )
        self._thread.start()

    def request(self) -> None:
        """Ask for a sync soon. Cheap and safe to call on every write."""
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait for work, but wake periodically so a queue left over from a
            # previous run still drains without a new write to trigger it.
            self._wake.wait(timeout=self._backoff)
            self._wake.clear()
            if self._stop.is_set():
                return
            self._attempt()

    def _attempt(self) -> None:
        try:
            result = self.syncer.sync()
            self._last = result
            if result.status == "conflict":
                # A conflict needs a human; stop retrying until they resolve it.
                self._backoff = MAX_BACKOFF
            elif result.ok:
                self._backoff = INITIAL_BACKOFF
            else:
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
        except Exception as exc:  # noqa: BLE001 - a sync must never crash Hermes
            from .config import redact

            self._last = SyncResult(status="failed", detail=redact(exc))
            logger.debug("hermes-pubky: background sync failed: %s", redact(exc))
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
