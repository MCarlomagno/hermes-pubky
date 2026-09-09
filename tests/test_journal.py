"""Durable local bookkeeping.

The properties that matter: a checkpoint's bytes survive every failure, a
corrupt journal is never mistaken for an empty one, and two processes cannot
hold the same connection.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from hermes_pubky.journal import (
    REQUEST_DONE,
    REQUEST_INTERRUPTED,
    REQUEST_PENDING,
    STATE_ACKNOWLEDGED,
    STATE_CONFLICT,
    STATE_STAGED,
    STATE_UPLOADING,
    ConnectionLock,
    Journal,
    JournalError,
    LockUnavailable,
    MaterializedFile,
    Upload,
    new_id,
)


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    with Journal(tmp_path / "journal.sqlite3") as j:
        yield j


def checkpoint(journal: Journal, tmp_path: Path, *, parent=None):
    body = b'{"snapshot":true}'
    path = tmp_path / f"snap-{new_id()[:6]}.json"
    path.write_bytes(body)
    from hermes_pubky.objects import hash_bytes

    return journal.create_checkpoint(
        snapshot_path=str(path), snapshot_hash=hash_bytes(body),
        parent_snapshot_id=parent[0] if parent else None,
        parent_hash=parent[1] if parent else None)


class TestDurability:
    def test_uses_wal_and_full_synchronous(self, journal):
        mode = journal._conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = journal._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert mode.lower() == "wal"
        assert sync == 2, "synchronous must be FULL"

    def test_the_journal_file_is_owner_only(self, tmp_path):
        import stat

        path = tmp_path / "j.sqlite3"
        with Journal(path):
            pass
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_corrupt_journal_raises_instead_of_looking_empty(self, tmp_path):
        path = tmp_path / "j.sqlite3"
        path.write_bytes(b"this is not a database" * 40)
        with pytest.raises(JournalError, match="not a usable journal"):
            Journal(path)

    def test_reopening_preserves_pending_work(self, tmp_path):
        path = tmp_path / "j.sqlite3"
        with Journal(path) as first:
            created = checkpoint(first, tmp_path)
            first.set_checkpoint_state(created.id, STATE_UPLOADING)
        with Journal(path) as second:
            active = second.active_checkpoints()
            assert [c.id for c in active] == [created.id]
            assert active[0].state == STATE_UPLOADING


class TestCheckpoints:
    def test_a_new_checkpoint_is_staged_and_active(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        assert created.state == STATE_STAGED
        assert created.is_active
        assert journal.has_pending()

    def test_states_advance_and_acknowledgement_retires_only_one(self, journal, tmp_path):
        first = checkpoint(journal, tmp_path)
        second = checkpoint(journal, tmp_path)
        journal.acknowledge_checkpoint(first.id)
        assert journal.get_checkpoint(first.id).state == STATE_ACKNOWLEDGED
        assert [c.id for c in journal.active_checkpoints()] == [second.id]

    def test_an_unknown_state_is_refused(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        with pytest.raises(ValueError, match="unknown checkpoint state"):
            journal.set_checkpoint_state(created.id, "nonsense")

    def test_checkpoints_are_published_oldest_first(self, journal, tmp_path):
        ids = [checkpoint(journal, tmp_path).id for _ in range(3)]
        assert journal.next_checkpoint().id == ids[0]

    def test_a_conflicted_checkpoint_is_reported_separately(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        journal.set_checkpoint_state(created.id, STATE_CONFLICT, "head moved")
        assert [c.id for c in journal.conflicted_checkpoints()] == [created.id]
        assert journal.active_checkpoints() == []
        assert journal.has_pending(), "a conflict is still pending work"


class TestUploads:
    def _uploads(self, checkpoint_id: str, count: int = 3):
        return [
            Upload(checkpoint_id=checkpoint_id,
                   object_path=f"objects/{i:064x}.bin",
                   sha256=f"{i:064x}", size=10 + i,
                   local_path=f"/tmp/o{i}", acknowledged=False)
            for i in range(count)
        ]

    def test_uploads_are_recorded_and_drain_individually(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        journal.record_uploads(created.id, self._uploads(created.id))
        assert len(journal.pending_uploads(created.id)) == 3

        journal.mark_uploaded(created.id, f"objects/{0:064x}.bin")
        assert len(journal.pending_uploads(created.id)) == 2
        assert len(journal.all_uploads(created.id)) == 3

    def test_recording_the_same_object_twice_is_idempotent(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        journal.record_uploads(created.id, self._uploads(created.id, 1))
        journal.record_uploads(created.id, self._uploads(created.id, 1))
        assert len(journal.all_uploads(created.id)) == 1

    def test_objects_of_unacknowledged_checkpoints_are_protected(self, journal, tmp_path):
        pending = checkpoint(journal, tmp_path)
        journal.record_uploads(pending.id, self._uploads(pending.id, 2))
        done = checkpoint(journal, tmp_path)
        journal.record_uploads(done.id, [Upload(
            checkpoint_id=done.id, object_path="objects/" + "f" * 64 + ".bin",
            sha256="f" * 64, size=1, local_path="/tmp/f", acknowledged=False)])
        journal.acknowledge_checkpoint(done.id)

        protected = journal.protected_objects()
        assert len(protected) == 2
        assert "objects/" + "f" * 64 + ".bin" not in protected

    def test_a_conflicted_checkpoints_objects_stay_protected(self, journal, tmp_path):
        created = checkpoint(journal, tmp_path)
        journal.record_uploads(created.id, self._uploads(created.id, 1))
        journal.set_checkpoint_state(created.id, STATE_CONFLICT, "moved")
        assert journal.protected_objects(), "a conflict's only copy is local"


class TestMaterialization:
    def test_records_round_trip(self, journal):
        record = MaterializedFile(logical_path="workspace/a.md", base_hash="a" * 64,
                                  present=True, dirty=False, explicit_delete=False)
        journal.set_materialized(record)
        assert journal.get_materialized("workspace/a.md") == record

    def test_marking_dirty_creates_a_record_when_absent(self, journal):
        journal.mark_dirty("profile/memories/USER.md")
        assert journal.dirty_paths() == ["profile/memories/USER.md"]

    def test_a_tombstone_survives_for_a_never_fetched_file(self, journal):
        journal.mark_deleted("workspace/remote-only.pdf")
        record = journal.get_materialized("workspace/remote-only.pdf")
        assert record.explicit_delete and record.dirty and not record.present

    def test_clearing_dirty_removes_tombstones_and_resets_flags(self, journal):
        journal.set_materialized(MaterializedFile(
            logical_path="workspace/a.md", base_hash="a" * 64, present=True,
            dirty=True, explicit_delete=False))
        journal.mark_deleted("workspace/gone.md")
        journal.clear_dirty()
        assert journal.dirty_paths() == []
        assert journal.get_materialized("workspace/gone.md") is None
        assert journal.get_materialized("workspace/a.md").present


class TestRequests:
    def test_enqueue_and_claim(self, journal):
        request = journal.enqueue_request("file_fetch", {"path": "workspace/a.md"}, "run-1")
        assert request.state == REQUEST_PENDING
        claimed = journal.claim_requests()
        assert [c.id for c in claimed] == [request.id]
        assert journal.claim_requests() == [], "claiming twice must not re-yield"

    def test_enqueue_is_idempotent_on_the_request_id(self, journal):
        one = journal.enqueue_request("file_fetch", {"path": "a"}, "run-1",
                                      request_id="fixed")
        two = journal.enqueue_request("file_fetch", {"path": "a"}, "run-1",
                                      request_id="fixed")
        assert one.id == two.id == "fixed"
        assert len(journal.claim_requests()) == 1

    def test_finishing_records_a_result(self, journal):
        request = journal.enqueue_request("file_fetch", {"path": "a"}, "run-1")
        journal.claim_requests()
        journal.finish_request(request.id, REQUEST_DONE, {"size": 12})
        assert journal.get_request(request.id).result == {"size": 12}

    def test_supervisor_exit_marks_outstanding_requests_interrupted(self, journal):
        journal.enqueue_request("file_fetch", {"path": "a"}, "run-1")
        pending = journal.enqueue_request("file_fetch", {"path": "b"}, "run-1")
        journal.claim_requests()
        journal.enqueue_request("file_fetch", {"path": "c"}, "run-1")

        assert journal.interrupt_running_requests() >= 2
        assert journal.get_request(pending.id).state == REQUEST_INTERRUPTED
        assert "supervisor exited" in journal.get_request(pending.id).result["error"]


class TestGeneration:
    def test_bumping_advances_the_counter(self, journal):
        assert journal.generation == 0
        assert journal.bump_generation() == 1
        assert journal.bump_generation() == 2
        assert journal.generation == 2


def _hold_lock(path: str, ready, done) -> None:
    lock = ConnectionLock(Path(path))
    lock.acquire()
    ready.set()
    done.wait(timeout=30)
    lock.release()


class TestConnectionLock:
    def test_a_second_holder_in_this_process_is_refused(self, tmp_path):
        first = ConnectionLock(tmp_path / "run.lock")
        first.acquire()
        try:
            with pytest.raises(LockUnavailable):
                ConnectionLock(tmp_path / "run.lock").acquire()
        finally:
            first.release()

    def test_a_second_holder_in_another_process_is_refused(self, tmp_path):
        # threading.Lock would not catch this; the lock must be a real
        # cross-process one.
        path = tmp_path / "run.lock"
        ctx = mp.get_context("spawn")
        ready, done = ctx.Event(), ctx.Event()
        worker = ctx.Process(target=_hold_lock, args=(str(path), ready, done))
        worker.start()
        try:
            assert ready.wait(timeout=30), "helper never acquired the lock"
            with pytest.raises(LockUnavailable):
                ConnectionLock(path).acquire()
        finally:
            done.set()
            worker.join(timeout=30)

    def test_the_lock_records_its_holder(self, tmp_path):
        import os

        lock = ConnectionLock(tmp_path / "run.lock")
        lock.acquire()
        try:
            assert lock.holder_pid() == os.getpid()
        finally:
            lock.release()

    def test_releasing_allows_a_new_holder(self, tmp_path):
        path = tmp_path / "run.lock"
        ConnectionLock(path).__enter__().release()
        second = ConnectionLock(path)
        second.acquire()
        second.release()
