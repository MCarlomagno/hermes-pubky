"""Slice 0: the upstream Hermes contract this integration is pinned to.

Two kinds of check live here. The fixture checks run everywhere and assert that
the recorded contract still describes what the adapter needs. The live checks
run only where `hermes-agent` is installed and assert the package still behaves
that way -- they are the ones that catch an upstream change.

Regenerate the fixture with:

    python scripts/generate_hermes_fixture.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "hermes_0_19_0_schema22.json"

PINNED_HERMES_VERSION = "0.19.0"
PINNED_SCHEMA_VERSION = 22
ADAPTER_ID = "hermes-0.19-sqlite22-v1"

# How the launcher's options translate to the pinned Hermes CLI. Verified
# against the installed parser rather than assumed, because a rename upstream
# would otherwise surface as a runtime failure inside a managed run.
LAUNCH_OPTION_MAP = {
    "--resume SESSION_ID": "--resume",
    "--query TEXT": "--oneshot",
    "portable model": "--model",
    "portable toolsets": "--toolsets",
    "cwd rebased by the adapter": "--no-restore-cwd",
}
# Options and subcommands the launcher must never expose or let portable
# configuration reach: they redirect the managed profile/workspace or disable
# an approval gate.
FORBIDDEN_LAUNCH_OPTIONS = ("--worktree", "--yolo", "--accept-hooks")
FORBIDDEN_SUBCOMMANDS = ("gateway", "cron", "proxy")

# Message fields whose loss would silently corrupt a restored conversation.
REQUIRED_MESSAGE_COLUMNS = {
    "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
    "tool_name", "timestamp", "reasoning", "reasoning_content", "api_content",
    "active", "compacted",
}
# Session fields the adapter either preserves or deliberately clears.
REQUIRED_SESSION_COLUMNS = {
    "id", "source", "model", "model_config", "parent_session_id", "cwd",
    "git_repo_root", "title", "archived", "rewind_count",
    "session_key", "chat_id", "chat_type", "thread_id", "display_name",
    "origin_json", "profile_name", "expiry_finalized",
    "handoff_state", "handoff_platform", "handoff_error",
    "compression_failure_cooldown_until", "compression_failure_error",
    "compression_fallback_streak", "billing_base_url",
}
# Tables the adapter empties, because they describe one machine's live state.
RUNTIME_TABLES = {
    "state_meta", "gateway_routing", "compression_locks", "async_delegations",
}

# Live checks need the real package; probe by import name so a partially
# installed environment is treated as absent rather than erroring mid-test.
HERMES_INSTALLED = importlib.util.find_spec("hermes_constants") is not None

live = pytest.mark.skipif(not HERMES_INSTALLED, reason="hermes-agent not installed")


@pytest.fixture(scope="module")
def help_text(tmp_path_factory) -> str:
    """`hermes --help` from the pinned package, captured once."""
    if not HERMES_INSTALLED:
        pytest.skip("hermes-agent not installed")
    home = tmp_path_factory.mktemp("home")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    out = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "--help"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture(scope="module")
def fixture() -> dict:
    if not FIXTURE.exists():
        pytest.fail(
            f"{FIXTURE} is missing; run scripts/generate_hermes_fixture.py"
        )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestRecordedContract:
    def test_pins_the_supported_versions(self, fixture):
        assert fixture["hermesVersion"] == PINNED_HERMES_VERSION
        assert fixture["schemaVersion"] == PINNED_SCHEMA_VERSION
        assert fixture["adapterId"] == ADAPTER_ID

    def test_durable_tables_are_the_ones_that_carry_a_conversation(self, fixture):
        assert fixture["durableTables"] == ["sessions", "messages", "session_model_usage"]
        for table in fixture["durableTables"]:
            assert table in fixture["tables"]

    def test_message_columns_cover_everything_a_restore_must_preserve(self, fixture):
        missing = REQUIRED_MESSAGE_COLUMNS - set(fixture["columns"]["messages"])
        assert not missing, f"messages is missing {sorted(missing)}"

    def test_session_columns_cover_preserved_and_cleared_fields(self, fixture):
        missing = REQUIRED_SESSION_COLUMNS - set(fixture["columns"]["sessions"])
        assert not missing, f"sessions is missing {sorted(missing)}"

    def test_runtime_tables_exist_so_normalization_has_a_target(self, fixture):
        assert RUNTIME_TABLES <= set(fixture["runtimeTables"])

    def test_full_text_search_structures_are_present(self, fixture):
        # Restore has to keep these or search silently stops working.
        assert any(t.startswith("messages_fts") for t in fixture["tables"])


class TestRecordedScenarios:
    """The fixture must actually contain the states the adapter is built for."""

    def test_a_tool_call_is_paired_with_its_result(self, fixture):
        session = fixture["scenarios"]["tools"]
        rows = [m for m in fixture["messages"] if m["session_id"] == session]
        call = next(m for m in rows if m["tool_calls"])
        result = next(m for m in rows if m["role"] == "tool")
        assert json.loads(call["tool_calls"])[0]["id"] == result["tool_call_id"]
        assert call["api_content"], "api_content must be captured"
        assert call["reasoning"], "reasoning must be captured"

    def test_a_rewound_message_is_inactive_but_still_stored(self, fixture):
        session = fixture["scenarios"]["rewound"]
        rows = [m for m in fixture["messages"] if m["session_id"] == session]
        assert [m["active"] for m in rows] == [1, 0], "rewind must leave an inactive row"

    def test_a_compacted_message_is_flagged_and_inactive(self, fixture):
        session = fixture["scenarios"]["compaction_parent"]
        folded = [m for m in fixture["messages"] if m["session_id"] == session]
        assert all(m["compacted"] == 1 and m["active"] == 0 for m in folded)

    def test_compaction_lineage_is_recorded(self, fixture):
        parent = fixture["scenarios"]["compaction_parent"]
        child = fixture["scenarios"]["compaction_child"]
        row = next(s for s in fixture["sessions"] if s["id"] == child)
        assert row["parent_session_id"] == parent

    def test_message_order_is_recoverable_from_the_row_ids(self, fixture):
        ids = [m["id"] for m in fixture["messages"]]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)

    def test_machine_specific_state_is_present_to_be_stripped(self, fixture):
        row = next(s for s in fixture["sessions"] if s["id"] == fixture["scenarios"]["plain"])
        for field in ("session_key", "chat_id", "display_name", "origin_json",
                      "profile_name", "handoff_state", "model_config", "billing_base_url"):
            assert row[field], f"{field} should be set in the fixture"

    def test_a_workspace_path_is_present_to_be_rebased(self, fixture):
        row = next(s for s in fixture["sessions"] if s["id"] == fixture["scenarios"]["plain"])
        assert row["cwd"] == fixture["workspace"]
        assert row["git_repo_root"] == fixture["workspace"]


@live
class TestLivePackage:
    """These fail if the installed package drifts from the recorded contract."""

    def test_schema_version_still_matches(self):
        import hermes_state

        assert hermes_state.SCHEMA_VERSION == PINNED_SCHEMA_VERSION

    def test_memory_entry_delimiter_still_matches(self):
        from tools.memory_tool import ENTRY_DELIMITER

        assert ENTRY_DELIMITER == "\n§\n"

    def test_memories_live_in_a_subdirectory_not_the_home_root(self):
        # 0.1 assumed root-level USER.md / MEMORY.md, which was wrong.
        from tools.memory_tool import get_memory_dir

        assert Path(get_memory_dir()).name == "memories"

    def test_default_config_still_supplies_the_portable_allowlist(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["model"] == ""
        assert DEFAULT_CONFIG["toolsets"] == ["hermes-cli"]
        assert DEFAULT_CONFIG["agent"]["max_turns"] == 90
        memory = DEFAULT_CONFIG["memory"]
        assert memory["memory_enabled"] is True
        assert memory["user_profile_enabled"] is True
        assert memory["memory_char_limit"] == 2200
        assert memory["user_char_limit"] == 1375
        assert DEFAULT_CONFIG["terminal"]["backend"] == "local"

    def test_the_installed_version_is_the_pinned_one(self):
        from importlib.metadata import version

        assert version("hermes-agent") == PINNED_HERMES_VERSION


@live
class TestLaunchOptions:
    """The launcher translates its own options; the pinned parser must accept them."""

    @pytest.mark.parametrize("intent,flag", sorted(LAUNCH_OPTION_MAP.items()))
    def test_each_mapped_option_exists(self, help_text, intent, flag):
        assert flag in help_text, f"{intent!r} maps to {flag}, absent from the parser"

    @pytest.mark.parametrize("flag", FORBIDDEN_LAUNCH_OPTIONS)
    def test_options_the_launcher_must_never_pass_still_exist_upstream(self, help_text, flag):
        # If one of these is renamed, the blocklist needs updating, so pin them.
        assert flag in help_text

    @pytest.mark.parametrize("subcommand", FORBIDDEN_SUBCOMMANDS)
    def test_modes_the_launcher_must_never_start_still_exist_upstream(self, help_text, subcommand):
        assert subcommand in help_text

    def test_oneshot_is_the_flag_for_a_single_prompt(self, help_text):
        # The plan's `--query TEXT` is not a Hermes option name; -z/--oneshot is.
        assert "--oneshot" in help_text
        assert "--query" not in help_text


@live
class TestIsolation:
    """A test run must never resolve to the developer's real Hermes profile.

    These run in subprocesses because `hermes_state.DEFAULT_DB_PATH` is bound at
    import time, so the environment has to be set before Hermes is imported.
    """

    @staticmethod
    def _resolve_home(env_home: str | None) -> str:
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        if env_home is not None:
            env["HERMES_HOME"] = env_home
        out = subprocess.run(
            [sys.executable, "-c",
             "from hermes_constants import get_hermes_home; print(get_hermes_home())"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def test_hermes_home_is_honored(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        assert Path(self._resolve_home(str(home))) == home.resolve()

    def test_the_db_path_follows_the_overridden_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        out = subprocess.run(
            [sys.executable, "-c",
             "import hermes_state; print(hermes_state.DEFAULT_DB_PATH)"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        assert Path(out.stdout.strip()) == home.resolve() / "state.db"

    def test_without_an_override_it_falls_back_to_the_real_home(self):
        # Proves the override above is doing the work, and documents the
        # fallback the supervisor must always prevent for a child process.
        assert Path(self._resolve_home(None)) == Path.home() / ".hermes"

    def test_the_fixture_was_not_generated_from_a_real_profile(self, fixture):
        real = str(Path.home() / ".hermes")
        assert not fixture["workspace"].startswith(real)
        for row in fixture["sessions"]:
            assert not (row["cwd"] or "").startswith(real)
