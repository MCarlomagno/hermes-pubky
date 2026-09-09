"""Local cache, sync state, and conflict backups."""

from __future__ import annotations

import stat
from pathlib import Path

from hermes_pubky.paths import Layout, write_private_file
from hermes_pubky.schema import BaseContextRef, Profile
from hermes_pubky.store import Store, SyncState


class TestProfileCache:
    def test_round_trips(self, store: Store):
        profile = Profile(profile_id="default", revision=3, user=["a"], memory=["b"])
        store.save_profile(profile)
        loaded = store.load_profile()
        assert loaded is not None
        assert (loaded.revision, loaded.user, loaded.memory) == (3, ["a"], ["b"])

    def test_a_missing_cache_reads_as_none(self, store: Store):
        assert store.load_profile() is None

    def test_a_corrupt_cache_reads_as_none(self, store: Store):
        store.layout.profile_cache.write_text("not json", encoding="utf-8")
        assert store.load_profile() is None

    def test_a_schema_violation_reads_as_none(self, store: Store):
        store.layout.profile_cache.write_text('{"schemaVersion": 99}', encoding="utf-8")
        assert store.load_profile() is None

    def test_the_cache_is_owner_only(self, store: Store):
        store.save_profile(Profile(profile_id="default"))
        mode = stat.S_IMODE(store.layout.profile_cache.stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_a_pinned_base_context_survives_the_round_trip(self, store: Store):
        ref = BaseContextRef(url="pubky://k/pub/c.json", sha256="ab" * 32)
        store.save_profile(Profile(profile_id="default", base_context=ref))
        loaded = store.load_profile()
        assert loaded is not None and loaded.base_context == ref


class TestContextCache:
    def test_round_trips_raw_bytes_and_metadata(self, store: Store):
        raw = b'{"schemaVersion":1,"id":"x","instructions":"do things"}'
        meta = store.save_context(raw, "pubky://k/pub/c.json", "ab" * 32)
        assert meta.url == "pubky://k/pub/c.json"

        context, loaded_meta = store.load_context()
        assert context is not None and context.id == "x"
        assert loaded_meta is not None and loaded_meta.sha256 == "ab" * 32
        assert store.load_context_bytes() == raw  # byte-for-byte, for rehashing

    def test_nothing_cached_reads_as_none(self, store: Store):
        assert store.load_context() == (None, None)

    def test_clearing_removes_both_files(self, store: Store):
        store.save_context(b'{"schemaVersion":1,"id":"x","instructions":"y"}',
                           "pubky://k/pub/c.json", "ab" * 32)
        store.clear_context()
        assert store.load_context() == (None, None)
        store.clear_context()  # idempotent

    def test_a_corrupt_body_still_reports_its_metadata(self, store: Store):
        store.save_context(b"garbage", "pubky://k/pub/c.json", "ab" * 32)
        context, meta = store.load_context()
        assert context is None
        assert meta is not None and meta.url == "pubky://k/pub/c.json"


class TestSyncState:
    def test_defaults_when_absent(self, store: Store):
        state = store.load_state()
        assert state.last_revision == 0 and state.conflict is False

    def test_round_trips(self, store: Store):
        state = SyncState(last_revision=9, last_synced_at="2026-01-01T00:00:00Z",
                          conflict=True, conflict_detail="clash",
                          conflict_remote_revision=11)
        store.save_state(state)
        loaded = store.load_state()
        assert loaded == state

    def test_a_corrupt_state_file_resets_to_defaults(self, store: Store):
        store.layout.state.write_text("{{{", encoding="utf-8")
        assert store.load_state() == SyncState()

    def test_wrong_types_reset_to_defaults(self, store: Store):
        store.layout.state.write_text('{"last_revision": "many"}', encoding="utf-8")
        assert store.load_state() == SyncState()


class TestBackups:
    def test_writes_a_timestamped_file(self, store: Store):
        path = store.backup("local-profile", b'{"a":1}')
        assert path.exists() and path.read_bytes() == b'{"a":1}'
        assert "local-profile" in path.name and path.suffix == ".json"

    def test_repeated_backups_do_not_overwrite_each_other(self, store: Store):
        import time

        store.backup("x", b"1")
        time.sleep(1.05)  # the name is second-resolution
        store.backup("x", b"2")
        assert len(list(store.layout.backups.iterdir())) == 2


class TestLayout:
    def test_everything_lives_under_hermes_home(self, home: Path):
        layout = Layout(home, "work")
        for path in (layout.profile_cache, layout.context_cache, layout.outbox,
                     layout.state, layout.backups, layout.env_file):
            assert home in path.parents or path.parent == home, path

    def test_profiles_are_isolated_from_each_other(self, home: Path):
        assert Layout(home, "a").root != Layout(home, "b").root

    def test_ensure_creates_an_owner_only_directory(self, home: Path):
        layout = Layout(home, "work")
        layout.ensure()
        assert layout.root.is_dir()
        assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700


class TestAtomicWrite:
    def test_leaves_no_temporary_file_behind(self, tmp_path: Path):
        target = tmp_path / "f.json"
        write_private_file(target, b"data")
        assert target.read_bytes() == b"data"
        assert list(tmp_path.iterdir()) == [target]

    def test_overwrites_cleanly(self, tmp_path: Path):
        target = tmp_path / "f.json"
        write_private_file(target, b"first")
        write_private_file(target, b"second")
        assert target.read_bytes() == b"second"

    def test_creates_missing_parents(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "f.json"
        write_private_file(target, b"x")
        assert target.exists()


class TestStatusSnapshot:
    """Status must describe the pin from the profile, not just the local cache.

    Regression: a machine that adopts an existing profile has the pin but has
    not fetched the context body yet; status used to report "(none)", implying
    nothing was pinned at all.
    """

    def _snapshot(self, home, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(home))
        from hermes_pubky.status import status_snapshot

        return status_snapshot({"profile_id": "default"})

    def test_reports_a_pin_whose_body_is_not_cached_yet(self, store, home, monkeypatch):
        from hermes_pubky.schema import BaseContextRef, Profile

        ref = BaseContextRef(url="pubky://k/pub/c.json", sha256="ab" * 32)
        store.save_profile(Profile(profile_id="default", revision=2, base_context=ref))

        snapshot = self._snapshot(home, monkeypatch)
        assert snapshot["base_context"] == "pubky://k/pub/c.json"
        assert snapshot["base_context_cached"] == "not yet fetched"

    def test_reports_a_pin_whose_body_is_cached(self, store, home, monkeypatch):
        from hermes_pubky.remote import sha256_hex
        from hermes_pubky.schema import BaseContextRef, Profile

        raw = b'{"schemaVersion":1,"id":"x","instructions":"y"}'
        digest = sha256_hex(raw)
        store.save_profile(
            Profile(
                profile_id="default",
                base_context=BaseContextRef(url="pubky://k/pub/c.json", sha256=digest),
            )
        )
        store.save_context(raw, "pubky://k/pub/c.json", digest)

        assert self._snapshot(home, monkeypatch)["base_context_cached"] == "yes"

    def test_reports_no_pin_when_there_is_none(self, store, home, monkeypatch):
        from hermes_pubky.schema import Profile

        store.save_profile(Profile(profile_id="default"))
        snapshot = self._snapshot(home, monkeypatch)
        assert snapshot["base_context"] == "(none)"
        assert snapshot["base_context_cached"] == "n/a"

    def test_never_contains_the_grant_secret(self, store, home, monkeypatch):
        import json

        monkeypatch.setenv("HERMES_PUBKY_GRANT_SECRET", "pubky-grant-credential-v1:a:b:c")
        rendered = json.dumps(self._snapshot(home, monkeypatch))
        assert "pubky-grant-credential-v1:a:b:c" not in rendered


class TestStatusFailureClassification:
    """A revoked grant is an auth problem, not a network outage.

    Reporting "unreachable" for a 401 sends the user looking at their network
    instead of running `hermes pubky login`.
    """

    def test_an_auth_failure_is_labelled_as_such(self, home, monkeypatch):
        from hermes_pubky import status as status_mod
        from hermes_pubky.remote import native, native_available

        if not native_available():
            import pytest

            pytest.skip("native extension not built")

        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_PUBKY_GRANT_SECRET", "some-secret")
        err = native().PubkyAuthError("homeserver returned 401: Grant has been revoked")
        monkeypatch.setattr(
            status_mod.Remote, "connect",
            staticmethod(lambda secret, timeout: (_ for _ in ()).throw(err)),
        )

        snapshot = status_mod.full_status()
        assert snapshot["homeserver"].startswith("not authorized")

    def test_a_network_failure_is_still_unreachable(self, home, monkeypatch):
        from hermes_pubky import status as status_mod
        from hermes_pubky.remote import native_available

        if not native_available():
            import pytest

            pytest.skip("native extension not built")

        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_PUBKY_GRANT_SECRET", "some-secret")
        monkeypatch.setattr(
            status_mod.Remote, "connect",
            staticmethod(
                lambda secret, timeout: (_ for _ in ()).throw(ConnectionError("no route"))
            ),
        )

        assert status_mod.full_status()["homeserver"].startswith("unreachable")
