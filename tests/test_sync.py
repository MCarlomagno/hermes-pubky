"""The checkpoint state machine under failure.

Every remote boundary is broken in turn, the engine restarted, and the outcome
checked against two rules: the active head never references an incomplete
upload, and no pending local operation is silently dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeAgentRemote

from hermes_pubky import models as m
from hermes_pubky.journal import (
    STATE_ACKNOWLEDGED,
    STATE_BLOCKED,
    STATE_CONFLICT,
    Journal,
    Upload,
)
from hermes_pubky.objects import ObjectCache, hash_bytes, stage_file
from hermes_pubky.sync import (
    MAX_BACKOFF,
    Fatal,
    SyncEngine,
    Transient,
    backoff_delay,
    classify,
)

RUNTIME = m.RuntimeInfo("hermes", "0.19.0", "hermes-0.19-sqlite22-v1")


class Fixture:
    """A journal, cache and engine wired to one fake remote."""

    def __init__(self, tmp_path: Path, remote: FakeAgentRemote) -> None:
        self.tmp = tmp_path
        self.remote = remote
        self.journal = Journal(tmp_path / "journal.sqlite3")
        self.cache = ObjectCache(tmp_path / "cache")
        self.recovery = tmp_path / "recovery"
        self.engine = SyncEngine(self.journal, remote, self.cache,
                                 recovery_dir=self.recovery)

    def stage(self, *, files: dict, parent=None, snapshot_id: str = "1" * 32):
        """Seal a candidate with real object files on disk."""
        records, uploads = {}, []
        objects_dir = self.tmp / "staged" / snapshot_id
        for logical, data in files.items():
            source = self.tmp / "src" / logical.replace("/", "_")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            staged = stage_file(source, logical, objects_dir)
            records[logical] = staged.record
            for ref, path in staged.objects.items():
                piece = next(p for p in staged.record.pieces if p.object == ref)
                uploads.append((piece, path))

        snapshot = m.Snapshot(
            agent_id="default", snapshot_id=snapshot_id,
            created_at="2026-09-09T18:00:00Z", device_id="f" * 32,
            runtime=RUNTIME, files=records,
            parent=m.SnapshotRef(snapshot_id=parent[0], sha256=parent[1]) if parent else None)
        body = snapshot.to_bytes()
        path = self.tmp / f"candidate-{snapshot_id}.json"
        path.write_bytes(body)

        checkpoint = self.journal.create_checkpoint(
            snapshot_path=str(path), snapshot_hash=hash_bytes(body),
            parent_snapshot_id=parent[0] if parent else None,
            parent_hash=parent[1] if parent else None)
        self.journal.record_uploads(checkpoint.id, [
            Upload(checkpoint_id=checkpoint.id, object_path=piece.object,
                   sha256=piece.sha256, size=piece.size, local_path=str(local),
                   acknowledged=False)
            for piece, local in uploads])
        return checkpoint, snapshot

    def close(self) -> None:
        self.journal.close()


@pytest.fixture
def fx(tmp_path):
    fixture = Fixture(tmp_path, FakeAgentRemote())
    yield fixture
    fixture.close()


class TestHappyPath:
    def test_a_first_publication_succeeds(self, fx):
        fx.stage(files={"profile/SOUL.md": b"# me\n"})
        result = fx.engine.sync()
        assert result.status == "synced", result.detail
        assert fx.remote.head is not None
        assert fx.journal.active_checkpoints() == []

    def test_objects_go_up_before_the_snapshot_and_the_head_last(self, fx):
        fx.stage(files={"profile/SOUL.md": b"# me\n", "workspace/a.md": b"a\n"})
        fx.engine.sync()
        order = fx.remote.order_of("put_object", "put_snapshot", "write_head")
        assert order[-1] == "write_head", order
        assert order.index("put_snapshot") == len(order) - 2, order
        assert all(c.startswith("put_object") for c in order[:-2]), order

    def test_a_second_checkpoint_builds_on_the_first(self, fx):
        fx.stage(files={"profile/SOUL.md": b"one\n"})
        fx.engine.sync()
        head = m.Head.parse(fx.remote.head)
        fx.stage(files={"profile/SOUL.md": b"two\n"},
                 parent=(head.snapshot_id, head.sha256), snapshot_id="2" * 32)
        assert fx.engine.sync().status == "synced"
        assert m.Head.parse(fx.remote.head).snapshot_id == "2" * 32



class TestFailureAtEveryBoundary:
    @pytest.mark.parametrize("boundary", ["read_head", "put_snapshot", "write_head"])
    def test_a_transient_failure_leaves_the_candidate_intact(self, fx, boundary):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.fail_at = boundary
        fx.remote.fail_with = lambda: ConnectionError("network down")

        result = fx.engine.sync()
        assert result.status == "retry", result.detail
        assert [c.id for c in fx.journal.active_checkpoints()] == [checkpoint.id]
        assert Path(checkpoint.snapshot_path).exists(), "sealed bytes must survive"

    @pytest.mark.parametrize("boundary", ["read_head", "put_snapshot", "write_head"])
    def test_restarting_after_a_failure_completes_the_same_checkpoint(self, fx, boundary):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.fail_at = boundary
        fx.remote.fail_with = lambda: ConnectionError("network down")
        fx.engine.sync()

        result = fx.engine.sync()
        assert result.status == "synced", result.detail
        assert fx.journal.get_checkpoint(checkpoint.id).state == STATE_ACKNOWLEDGED

    def test_a_failure_during_an_object_upload_resumes_without_reuploading(self, fx):
        checkpoint, _ = fx.stage(files={
            "profile/SOUL.md": b"# me\n", "workspace/a.md": b"a\n",
            "workspace/b.md": b"b\n"})
        first = fx.journal.pending_uploads(checkpoint.id)[0]
        fx.remote.fail_at = f"put_object:{first.object_path}"
        fx.remote.fail_with = lambda: ConnectionError("network down")
        assert fx.engine.sync().status == "retry"

        # Nothing reached the head, so the previous state is still valid.
        assert fx.remote.head is None

        before = [c for c in fx.remote.calls if c.startswith("put_object")]
        assert fx.engine.sync().status == "synced"
        after = [c for c in fx.remote.calls if c.startswith("put_object")]
        uploaded_twice = [o for o in set(after) if after.count(o) > 1]
        assert not uploaded_twice, f"re-uploaded {uploaded_twice}"
        assert len(before) < len(after)

    def test_an_interrupted_upload_never_leaves_a_head_referencing_it(self, fx):
        fx.stage(files={"profile/SOUL.md": b"# me\n", "workspace/a.md": b"a\n"})
        fx.remote.fail_at = "put_snapshot"
        fx.remote.fail_with = lambda: ConnectionError("network down")
        fx.engine.sync()
        assert fx.remote.head is None, "the head must not move before the snapshot lands"

    def test_a_lost_response_after_the_head_write_is_recognized_as_success(self, fx):
        # The write landed; only the answer was lost. A retry must converge, not
        # report data loss.
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.lose_at = "write_head"
        first = fx.engine.sync()
        assert first.status == "retry", first.detail
        assert fx.remote.head is not None, "the head did land"

        second = fx.engine.sync()
        assert second.status == "synced", second.detail
        assert fx.journal.get_checkpoint(checkpoint.id).state == STATE_ACKNOWLEDGED

    def test_a_lost_response_after_the_snapshot_write_converges(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.lose_at = "put_snapshot"
        assert fx.engine.sync().status == "retry"
        assert fx.engine.sync().status == "synced"
        assert fx.journal.get_checkpoint(checkpoint.id).state == STATE_ACKNOWLEDGED

    def test_an_auth_failure_blocks_instead_of_retrying(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})

        class PubkyAuthError(Exception):
            pass

        fx.remote.fail_at = "read_head"
        fx.remote.fail_with = lambda: PubkyAuthError("grant was revoked")
        result = fx.engine.sync()
        assert result.status == "blocked", result.detail
        assert fx.journal.get_checkpoint(checkpoint.id).state == STATE_BLOCKED
        assert Path(checkpoint.snapshot_path).exists()

    def test_a_missing_local_object_blocks_rather_than_publishing_a_gap(self, fx):
        checkpoint, _ = fx.stage(files={"workspace/a.md": b"a\n"})
        for upload in fx.journal.pending_uploads(checkpoint.id):
            Path(upload.local_path).unlink()
        result = fx.engine.sync()
        assert result.status == "blocked"
        assert "missing locally" in result.detail
        assert fx.remote.head is None

    def test_a_tampered_sealed_snapshot_blocks(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        Path(checkpoint.snapshot_path).write_bytes(b'{"tampered":true}')
        result = fx.engine.sync()
        assert result.status == "blocked"
        assert "no longer matches" in result.detail


class TestConflicts:
    def _moved_remote(self, fx, snapshot_id="9" * 32):
        other = m.Snapshot(agent_id="default", snapshot_id=snapshot_id,
                           created_at="2026-09-09T19:00:00Z", device_id="e" * 32,
                           runtime=RUNTIME)
        return fx.remote.set_head(other)

    def test_a_head_that_moved_before_upload_conflicts(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"mine\n"},
                                 parent=("0" * 32, "a" * 64))
        self._moved_remote(fx)

        result = fx.engine.sync()
        assert result.status == "conflict", result.detail
        assert fx.journal.get_checkpoint(checkpoint.id).state == STATE_CONFLICT
        assert Path(checkpoint.snapshot_path).exists(), "local work is preserved"

    def test_a_conflict_blocks_further_syncing_until_resolved(self, fx):
        fx.stage(files={"profile/SOUL.md": b"mine\n"}, parent=("0" * 32, "a" * 64))
        self._moved_remote(fx)
        fx.engine.sync()

        again = fx.engine.sync()
        assert again.status == "conflict"
        assert "--prefer" in again.detail

    def test_a_head_created_elsewhere_conflicts_with_a_first_publication(self, fx):
        fx.stage(files={"profile/SOUL.md": b"mine\n"})  # parent is None
        self._moved_remote(fx)
        result = fx.engine.sync()
        assert result.status == "conflict"
        assert "first" in result.detail

    def test_prefer_remote_keeps_the_remote_and_preserves_local(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"mine\n"},
                                 parent=("0" * 32, "a" * 64))
        ref = self._moved_remote(fx)
        fx.engine.sync()

        result = fx.engine.resolve("remote")
        assert result.status == "synced", result.detail
        assert m.Head.parse(fx.remote.head).snapshot_id == ref.snapshot_id
        assert fx.journal.conflicted_checkpoints() == []
        saved = list(fx.recovery.rglob("local-snapshot.json"))
        assert saved, "the discarded local side must be recoverable"

    def test_prefer_local_rebases_onto_the_current_remote_head(self, fx):
        fx.stage(files={"profile/SOUL.md": b"mine\n"}, parent=("0" * 32, "a" * 64))
        ref = self._moved_remote(fx)
        fx.engine.sync()

        result = fx.engine.resolve("local")
        assert result.status == "synced", result.detail
        published = m.Snapshot.parse(fx.remote.snapshots[m.Head.parse(fx.remote.head).snapshot_id])
        assert published.files["profile/SOUL.md"].size == 5
        assert published.parent is not None
        assert published.parent.snapshot_id == ref.snapshot_id, "must build on the remote"
        assert list(fx.recovery.rglob("remote-head.json")), "remote side preserved"



    def test_two_simultaneous_writers_stay_outside_the_guarantee(self, fx):
        """Documented limit: the head is re-read, not compare-and-swapped.

        A writer that moves the head between our final read and our write is not
        prevented. What is guaranteed is that the immutable candidate survives
        and is recoverable, which this asserts.
        """
        checkpoint, snapshot = fx.stage(files={"profile/SOUL.md": b"mine\n"})

        original_write = fx.remote.write_head
        def racing_write(head, timeout_secs=None):
            # Another machine publishes in the same instant.
            other = m.Snapshot(agent_id="default", snapshot_id="7" * 32,
                               created_at="2026-09-09T20:00:00Z",
                               device_id="d" * 32, runtime=RUNTIME)
            fx.remote.set_head(other)
        fx.remote.write_head = racing_write

        result = fx.engine.sync()
        assert result.status == "conflict", result.detail
        # Our snapshot document is still on the server and still readable.
        assert snapshot.snapshot_id in fx.remote.snapshots
        assert Path(checkpoint.snapshot_path).exists()
        fx.remote.write_head = original_write


class TestClassificationAndBackoff:
    @pytest.mark.parametrize("exc,expected", [
        (ConnectionError("down"), Transient),
        (TimeoutError("slow"), Transient),
        (m.SchemaError("bad document"), Fatal),
        (ValueError("bad argument"), Fatal),
    ])
    def test_failures_split_into_retryable_and_not(self, exc, expected):
        assert isinstance(classify(exc), expected)

    @pytest.mark.parametrize("name,expected", [
        ("PubkyAuthError", Fatal),
        ("PubkyValidationError", Fatal),
        ("PubkyTooLargeError", Fatal),
        ("PubkyNetworkError", Transient),
        ("PubkyTimeoutError", Transient),
    ])
    def test_native_error_names_are_classified(self, name, expected):
        exc = type(name, (Exception,), {})("boom")
        assert isinstance(classify(exc), expected)

    def test_a_quota_failure_does_not_retry(self):
        assert isinstance(classify(Exception("storage quota exceeded")), Fatal)

    def test_throttling_is_transient_whatever_type_reports_it(self):
        exc = type("PubkyValidationError", (Exception,), {})("homeserver returned 429: slow down")
        assert isinstance(classify(exc), Transient)

    def test_an_exhausted_deadline_transfers_nothing_and_keeps_the_work(self, fx):
        checkpoint, _ = fx.stage(files={"profile/SOUL.md": b"# me\n"})
        result = fx.engine.sync(deadline=0.0)
        assert result.status == "retry" and "budget" in result.detail
        assert fx.remote.calls == [], "no transfer may start past the deadline"
        assert [c.id for c in fx.journal.active_checkpoints()] == [checkpoint.id]

    def test_backoff_grows_and_is_capped(self):
        assert backoff_delay(1) <= 2.0
        assert backoff_delay(20) <= MAX_BACKOFF
        assert all(backoff_delay(n) > 0 for n in range(1, 10))

    def test_a_retry_after_hint_is_honored(self):
        assert backoff_delay(5, retry_after=3.0) == 3.0
        assert backoff_delay(5, retry_after=9999.0) == MAX_BACKOFF

    def test_the_retry_loop_stops_at_its_deadline(self, fx):
        fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.fail_at = "read_head"
        fx.remote.fail_with = lambda: ConnectionError("down")
        # Re-arm the failure every attempt so only the deadline can end it.
        original = fx.engine.sync
        def always_retry(*a, **k):
            fx.remote.fail_at = "read_head"
            return original(*a, **k)
        fx.engine.sync = always_retry

        # A fake clock that only advances when the engine sleeps, so the
        # deadline is deterministic.
        clock = [1000.0]
        slept = []

        def fake_sleep(delay):
            slept.append(delay)
            clock[0] += delay

        result = fx.engine.sync_with_retries(
            deadline=clock[0] + 5.0, max_attempts=50,
            sleep=fake_sleep, now=lambda: clock[0])
        assert result.status == "retry"
        assert sum(slept) <= 5.0 + 1e-6, f"slept past the deadline: {sum(slept)}"
        assert len(slept) < 50, "must stop at the deadline, not exhaust attempts"

    def test_the_retry_loop_succeeds_after_a_single_failure(self, fx):
        fx.stage(files={"profile/SOUL.md": b"# me\n"})
        fx.remote.fail_at = "read_head"
        fx.remote.fail_with = lambda: ConnectionError("down")
        result = fx.engine.sync_with_retries(sleep=lambda _d: None)
        assert result.status == "synced", result.detail
