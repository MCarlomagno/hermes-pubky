"""Local layout and the child environment.

Isolation is the point: the same agent id under two identities or networks must
never share state, and the child must never resolve to the user's ordinary
Hermes profile or be handed the grant.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from hermes_pubky.paths import (
    CONNECTION_ENV,
    GRANT_ENV,
    HERMES_HOME_ENV,
    MANAGED_ENV,
    NETWORK_MAINNET,
    NETWORK_TESTNET,
    ROOT_ENV,
    TERMINAL_CWD_ENV,
    DeviceIdentity,
    Layout,
    default_root,
    is_managed_child,
    iter_connections,
    layout_for,
    read_env_file,
    template_dir,
    write_env_file,
    write_private,
)

A = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
B = "1zpqzr4nz1c9krzcbrfzo8w4hb8kfxa6fjjwygtsxsjyyfp7zwjo"


class TestPartitioning:
    def test_two_identities_never_share_state(self, tmp_path):
        one = Layout(tmp_path, NETWORK_MAINNET, A, "default")
        two = Layout(tmp_path, NETWORK_MAINNET, B, "default")
        assert one.base != two.base
        assert one.journal_file != two.journal_file
        assert one.credentials_file != two.credentials_file

    def test_two_networks_never_share_state(self, tmp_path):
        main = Layout(tmp_path, NETWORK_MAINNET, A, "default")
        test = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        assert main.base != test.base

    def test_an_unknown_network_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown network"):
            layout_for(A, "default", network="staging", root=tmp_path)

    def test_the_root_honors_its_environment_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ROOT_ENV, str(tmp_path / "elsewhere"))
        assert default_root() == (tmp_path / "elsewhere").resolve()

    def test_template_receipts_live_outside_any_agent_workspace(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_MAINNET, A, "default")
        receipts = template_dir(A, "researcher", root=tmp_path)
        assert layout.workspace not in receipts.parents
        assert receipts != layout.base


class TestPermissions:
    def test_ensure_creates_owner_only_directories(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default").ensure()
        for path in (layout.base, layout.cache, layout.workspace,
                     layout.hermes_home, layout.pending, layout.recovery):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700, path

    def test_private_writes_are_owner_only_and_atomic(self, tmp_path):
        target = tmp_path / "a" / "secret"
        write_private(target, b"data")
        assert target.read_bytes() == b"data"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert list(target.parent.iterdir()) == [target], "no temp file left"

    def test_an_env_file_is_owner_only(self, tmp_path):
        path = tmp_path / "credentials.env"
        write_env_file(path, {GRANT_ENV: "secret-value"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert read_env_file(path) == {GRANT_ENV: "secret-value"}

    def test_reading_a_missing_env_file_is_empty(self, tmp_path):
        assert read_env_file(tmp_path / "absent.env") == {}


class TestHermesLayout:
    def test_memories_live_in_a_subdirectory(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        # 0.1 assumed root-level files; the pinned Hermes uses memories/.
        assert layout.user_memory_file.parent.name == "memories"
        assert layout.agent_memory_file.parent.name == "memories"

    def test_the_database_belongs_to_the_dedicated_profile(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        assert layout.state_db == layout.hermes_home / "state.db"

    def test_the_plugin_shim_goes_where_hermes_scans(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        assert layout.plugin_dir == layout.hermes_home / "plugins" / "pubky"


class TestChildEnvironment:
    def test_the_child_is_pointed_at_the_dedicated_home(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        env = layout.child_environment({})
        assert env[HERMES_HOME_ENV] == str(layout.hermes_home)
        assert env[TERMINAL_CWD_ENV] == str(layout.workspace)
        assert env[MANAGED_ENV] == "1"
        assert env[CONNECTION_ENV] == str(layout.connection_file)

    def test_the_grant_never_reaches_the_child(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        env = layout.child_environment({GRANT_ENV: "secret-value"})
        assert GRANT_ENV not in env, "the supervisor owns all remote I/O"

    def test_a_stale_root_override_is_dropped(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        env = layout.child_environment({ROOT_ENV: "/somewhere/else"})
        assert ROOT_ENV not in env

    def test_the_home_is_never_the_users_own_profile(self, tmp_path):
        layout = Layout(tmp_path, NETWORK_TESTNET, A, "default")
        env = layout.child_environment({})
        assert Path(env[HERMES_HOME_ENV]) != Path.home() / ".hermes"

    def test_managed_detection_follows_the_environment(self, monkeypatch):
        monkeypatch.delenv(MANAGED_ENV, raising=False)
        assert is_managed_child() is False
        monkeypatch.setenv(MANAGED_ENV, "1")
        assert is_managed_child() is True


class TestWorkspaceGuard:
    def test_a_root_inside_another_agents_workspace_is_refused(self, tmp_path):
        # That workspace is scanned for upload, so a journal or credential file
        # inside it would become a publication candidate.
        outer = Layout(tmp_path / "outer", NETWORK_TESTNET, A, "first").ensure()
        outer.connection_file.write_text("{}", encoding="utf-8")

        nested_root = outer.workspace / "nested-root"
        inner = Layout(nested_root, NETWORK_TESTNET, A, "second")
        with pytest.raises(ValueError, match="managed workspace"):
            inner.assert_outside_workspace()

    def test_a_root_equal_to_a_workspace_is_refused(self, tmp_path):
        outer = Layout(tmp_path / "outer", NETWORK_TESTNET, A, "first").ensure()
        outer.connection_file.write_text("{}", encoding="utf-8")
        inner = Layout(outer.workspace, NETWORK_TESTNET, A, "second")
        with pytest.raises(ValueError, match="managed workspace"):
            inner.assert_outside_workspace()

    def test_an_ordinary_root_is_accepted(self, tmp_path):
        Layout(tmp_path, NETWORK_TESTNET, A, "default").assert_outside_workspace()


class TestConnectionDiscovery:
    def test_only_directories_with_a_connection_file_are_listed(self, tmp_path):
        one = Layout(tmp_path, NETWORK_TESTNET, A, "default").ensure()
        Layout(tmp_path, NETWORK_TESTNET, A, "unfinished").ensure()
        one.connection_file.write_text("{}", encoding="utf-8")
        found = list(iter_connections(tmp_path))
        assert [c.agent_id for c in found] == ["default"]

    def test_agents_under_different_owners_are_both_listed(self, tmp_path):
        for owner in (A, B):
            layout = Layout(tmp_path, NETWORK_TESTNET, owner, "default").ensure()
            layout.connection_file.write_text("{}", encoding="utf-8")
        assert sorted(c.owner for c in iter_connections(tmp_path)) == sorted([A, B])

    def test_an_absent_root_lists_nothing(self, tmp_path):
        assert list(iter_connections(tmp_path / "absent")) == []


class TestDeviceIdentity:
    def test_it_is_created_once_and_reused(self, tmp_path):
        first = DeviceIdentity.load_or_create(tmp_path)
        second = DeviceIdentity.load_or_create(tmp_path)
        assert first.device_id == second.device_id
        assert len(first.device_id) == 32

    def test_a_corrupt_device_file_is_replaced(self, tmp_path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "device.json").write_text("{not json", encoding="utf-8")
        assert len(DeviceIdentity.load_or_create(tmp_path).device_id) == 32
