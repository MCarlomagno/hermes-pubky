"""Reconciliation: adoption, push, conflict detection, and resolution."""

from __future__ import annotations

import json

import pytest

from hermes_pubky.outbox import Operation, Outbox
from hermes_pubky.schema import Profile
from hermes_pubky.store import Store
from hermes_pubky.sync import Syncer

from fakes import FakeRemote


def make_syncer(store: Store, outbox: Outbox, remote: FakeRemote) -> Syncer:
    return Syncer(store, outbox, "default", lambda: remote)


def remote_with(revision: int = 1, **kw) -> FakeRemote:
    return FakeRemote(Profile(profile_id="default", revision=revision, **kw))


class TestAdoptRemote:
    def test_with_nothing_queued_the_remote_wins(self, store, outbox):
        remote = remote_with(revision=5, memory=["from another machine"])
        result = make_syncer(store, outbox, remote).sync()
        assert result.status == "up-to-date"
        cached = store.load_profile()
        assert cached is not None and cached.memory == ["from another machine"]
        assert store.load_state().last_revision == 5

    def test_an_absent_remote_profile_is_not_an_error(self, store, outbox):
        result = make_syncer(store, outbox, FakeRemote()).sync()
        assert result.status == "up-to-date"
        assert store.load_profile() is None


class TestPush:
    def test_queued_writes_are_applied_and_pushed(self, store, outbox):
        remote = remote_with(revision=2, memory=["existing"])
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "new fact"))

        result = make_syncer(store, outbox, remote).sync()

        assert result.status == "synced"
        assert result.revision == 3
        pushed = remote.puts[-1]
        assert pushed["memory"] == ["existing", "new fact"]
        assert pushed["revision"] == 3
        assert pushed["updatedAt"]

    def test_the_queue_is_cleared_after_a_successful_push(self, store, outbox):
        remote = remote_with(revision=1)
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "user", "x"))
        make_syncer(store, outbox, remote).sync()
        assert outbox.count() == 0

    def test_a_failed_push_leaves_the_queue_intact(self, store, outbox):
        remote = remote_with(revision=1)
        remote.fail_put = RuntimeError("homeserver down")
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "user", "keep me"))

        with pytest.raises(RuntimeError):
            make_syncer(store, outbox, remote).sync()

        assert outbox.count() == 1
        assert store.load_state().last_revision == 1

    def test_pushing_to_a_profile_that_does_not_exist_yet_creates_it(self, store, outbox):
        remote = FakeRemote()
        outbox.append(Operation("add", "memory", "first ever"))
        result = make_syncer(store, outbox, remote).sync()
        assert result.status == "synced" and result.revision == 1
        assert remote.puts[-1]["memory"] == ["first ever"]

    def test_the_remote_base_context_pin_is_preserved(self, store, outbox):
        ref = {"url": "pubky://k/pub/c.json", "sha256": "ab" * 32}
        raw = json.loads(Profile(profile_id="default", revision=1).to_bytes())
        raw["baseContext"] = ref
        remote = FakeRemote()
        remote.stored["default"] = json.dumps(raw).encode()
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "memory", "x"))

        make_syncer(store, outbox, remote).sync()
        assert remote.puts[-1]["baseContext"] == ref

    def test_replayed_operations_apply_in_order(self, store, outbox):
        remote = remote_with(revision=1)
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "memory", "draft"))
        outbox.append(Operation("replace", "memory", "final", old_text="draft"))

        make_syncer(store, outbox, remote).sync()
        assert remote.puts[-1]["memory"] == ["final"]

    def test_a_queue_that_outlives_a_restart_still_drains(self, store, outbox):
        remote = remote_with(revision=1)
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "memory", "written while offline"))

        # Fresh objects, same paths — as if the process had restarted.
        reopened = Syncer(Store(store.layout), Outbox(outbox.path), "default", lambda: remote)
        result = reopened.sync()

        assert result.status == "synced"
        assert remote.puts[-1]["memory"] == ["written while offline"]


class TestConflict:
    def test_a_moved_remote_with_pending_writes_conflicts(self, store, outbox):
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local write"))
        remote = remote_with(revision=7, memory=["someone else's write"])

        result = make_syncer(store, outbox, remote).sync()

        assert result.status == "conflict"
        assert "revision 7" in result.detail and "saw 2" in result.detail
        assert remote.puts == []          # nothing was pushed
        assert outbox.count() == 1        # nothing was lost
        assert store.load_state().conflict is True

    def test_syncing_again_stays_blocked_until_resolved(self, store, outbox):
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local"))
        remote = remote_with(revision=7)
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        again = syncer.sync()
        assert again.status == "conflict"
        assert "--prefer" in again.detail
        assert remote.puts == []

    def test_a_moved_remote_without_pending_writes_is_just_adopted(self, store, outbox):
        store.save_state(_state(store, last_revision=2))
        remote = remote_with(revision=7, memory=["theirs"])
        result = make_syncer(store, outbox, remote).sync()
        assert result.status == "up-to-date"
        assert store.load_state().conflict is False

    def test_prefer_remote_discards_local_and_backs_it_up(self, store, outbox):
        store.save_profile(Profile(profile_id="default", revision=2, memory=["mine"]))
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local write"))
        remote = remote_with(revision=7, memory=["theirs"])
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        result = syncer.sync(prefer="remote")

        assert result.status == "synced"
        assert outbox.count() == 0
        cached = store.load_profile()
        assert cached is not None and cached.memory == ["theirs"]
        assert store.load_state().conflict is False
        backups = sorted(p.name for p in store.layout.backups.iterdir())
        assert any("local-profile" in n for n in backups)
        assert any("local-outbox" in n for n in backups)

    def test_the_backed_up_outbox_is_readable(self, store, outbox):
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "recover me"))
        remote = remote_with(revision=7)
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()
        syncer.sync(prefer="remote")

        backup = next(p for p in store.layout.backups.iterdir() if "local-outbox" in p.name)
        saved = json.loads(backup.read_text())
        assert saved[0]["content"] == "recover me"

    def test_prefer_local_discards_the_remote_and_backs_it_up(self, store, outbox):
        store.save_profile(Profile(profile_id="default", revision=2, memory=["mine"]))
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local write"))
        remote = remote_with(revision=7, memory=["theirs"])
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        result = syncer.sync(prefer="local")

        assert result.status == "synced"
        # Must advance past the remote, or the homeserver keeps the old copy.
        assert result.revision == 8
        pushed = remote.puts[-1]["memory"]
        assert pushed == ["mine", "local write"]
        assert "theirs" not in pushed          # the remote side really is dropped
        assert outbox.count() == 0
        assert store.load_state().conflict is False
        assert any("remote-profile" in p.name for p in store.layout.backups.iterdir())

    def test_prefer_local_keeps_a_pin_only_the_remote_had(self, store, outbox):
        # Losing a base context the user pinned elsewhere would be a silent
        # regression, so it is inherited when this machine has none.
        ref = {"url": "pubky://k/pub/c.json", "sha256": "ab" * 32}
        raw = json.loads(Profile(profile_id="default", revision=7).to_bytes())
        raw["baseContext"] = ref
        remote = FakeRemote()
        remote.stored["default"] = json.dumps(raw).encode()
        store.save_profile(Profile(profile_id="default", revision=2))
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local write"))
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        syncer.sync(prefer="local")
        assert remote.puts[-1]["baseContext"] == ref

    def test_prefer_local_with_no_cached_profile_still_pushes_the_queue(self, store, outbox):
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "local write"))
        remote = remote_with(revision=7, memory=["theirs"])
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        result = syncer.sync(prefer="local")
        assert result.status == "synced"
        assert remote.puts[-1]["memory"] == ["local write"]

    def test_prefer_rejects_an_unknown_side(self, store, outbox):
        outbox.append(Operation("add", "memory", "x"))
        with pytest.raises(ValueError, match="prefer"):
            make_syncer(store, outbox, remote_with()).sync(prefer="both")


def _state(store: Store, **kw):
    state = store.load_state()
    for key, value in kw.items():
        setattr(state, key, value)
    return state


class TestConcurrentWrites:
    """A write mirrored while a push is in flight must not be dropped.

    Regression: the queue used to be cleared wholesale after a successful
    push, deleting any operation appended during the network round trip. Two
    memory writes in quick succession would reliably lose the second.
    """

    def test_a_write_that_lands_during_the_push_is_kept(self, store, outbox):
        remote = remote_with(revision=1)
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "memory", "first write"))

        # The agent mirrors another write while put_profile is in flight.
        original_put = remote.put_profile

        def put_then_write(profile, timeout_secs=10.0):
            original_put(profile, timeout_secs)
            outbox.append(Operation("add", "user", "written mid-push"))

        remote.put_profile = put_then_write

        result = make_syncer(store, outbox, remote).sync()

        assert result.status == "synced"
        assert remote.puts[-1]["memory"] == ["first write"]
        # The latecomer survives, and only it.
        survivors = [op.content for op in outbox.load()]
        assert survivors == ["written mid-push"]
        assert "still queued" in result.detail

    def test_the_kept_write_is_pushed_on_the_next_pass(self, store, outbox):
        remote = remote_with(revision=1)
        store.save_state(_state(store, last_revision=1))
        outbox.append(Operation("add", "memory", "first write"))

        original_put = remote.put_profile
        fired = []

        def put_then_write(profile, timeout_secs=10.0):
            original_put(profile, timeout_secs)
            if not fired:
                fired.append(True)
                outbox.append(Operation("add", "user", "written mid-push"))

        remote.put_profile = put_then_write
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()
        syncer.sync()

        assert outbox.count() == 0
        assert remote.puts[-1]["user"] == ["written mid-push"]
        assert remote.puts[-1]["memory"] == ["first write"]

    def test_discarding_local_writes_keeps_one_that_arrives_mid_resolve(
        self, store, outbox
    ):
        # Writes queued before `--prefer remote` are what the user chose to
        # discard. One that lands *during* the resolution was never offered
        # up, so it has to survive.
        store.save_state(_state(store, last_revision=2))
        outbox.append(Operation("add", "memory", "conflicted write"))
        remote = remote_with(revision=7)
        syncer = make_syncer(store, outbox, remote)
        syncer.sync()

        original_fetch = remote.fetch_profile

        def fetch_then_write(profile_id, timeout_secs=5.0):
            result = original_fetch(profile_id, timeout_secs)
            outbox.append(Operation("add", "user", "written mid-resolve"))
            return result

        remote.fetch_profile = fetch_then_write
        syncer.sync(prefer="remote")

        assert [op.content for op in outbox.load()] == ["written mid-resolve"]
