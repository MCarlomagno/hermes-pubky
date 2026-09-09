"""The managed provider bridge.

It must be inert unless the launcher started it, must not duplicate content
Hermes already injects, and must never let a non-primary context schedule a
remote write.
"""

from __future__ import annotations

import json

import pytest

from hermes_pubky.journal import Journal, MaterializedFile
from hermes_pubky.paths import CONNECTION_ENV, MANAGED_ENV, Layout
from hermes_pubky.provider import NONPRIMARY_CONTEXTS, PubkyMemoryProvider

OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


@pytest.fixture
def managed(tmp_path, monkeypatch):
    """A provider wired to a real connection file and journal."""
    layout = Layout(root=tmp_path / "root", network="testnet", owner=OWNER,
                    agent_id="default").ensure()
    connection = {
        "root": str(layout.root), "network": "testnet", "owner": OWNER,
        "agentId": "default", "hermesHome": str(layout.hermes_home),
        "workspace": str(layout.workspace), "runId": "run-1",
    }
    layout.connection_file.write_text(json.dumps(connection), encoding="utf-8")
    monkeypatch.setenv(MANAGED_ENV, "1")
    monkeypatch.setenv(CONNECTION_ENV, str(layout.connection_file))
    provider = PubkyMemoryProvider()
    yield provider, layout
    provider.shutdown()


class TestActivation:
    def test_unmanaged_is_unavailable(self, monkeypatch):
        monkeypatch.delenv(MANAGED_ENV, raising=False)
        monkeypatch.delenv(CONNECTION_ENV, raising=False)
        assert PubkyMemoryProvider().is_available() is False

    def test_a_connection_file_alone_does_not_activate_it(self, tmp_path, monkeypatch):
        # A downloaded config must not be able to point the provider at a path.
        path = tmp_path / "connection.json"
        path.write_text(json.dumps({"network": "testnet", "owner": OWNER,
                                    "agentId": "x", "hermesHome": "/tmp/h",
                                    "workspace": "/tmp/w"}), encoding="utf-8")
        monkeypatch.delenv(MANAGED_ENV, raising=False)
        monkeypatch.setenv(CONNECTION_ENV, str(path))
        assert PubkyMemoryProvider().is_available() is False

    def test_managed_with_a_valid_connection_is_available(self, managed):
        provider, _layout = managed
        assert provider.is_available() is True

    def test_a_malformed_connection_is_refused(self, managed, monkeypatch):
        provider, layout = managed
        layout.connection_file.write_text("{not json", encoding="utf-8")
        assert provider.is_available() is False

    def test_initializing_unmanaged_leaves_it_inert(self, monkeypatch, tmp_path):
        monkeypatch.delenv(CONNECTION_ENV, raising=False)
        provider = PubkyMemoryProvider()
        provider.initialize("session-1", hermes_home=str(tmp_path))
        assert provider.system_prompt_block() == ""
        assert provider.get_tool_schemas() == []

    def test_a_mismatched_home_is_refused(self, managed, tmp_path):
        provider, _layout = managed
        provider.initialize("session-1", hermes_home=str(tmp_path / "elsewhere"))
        assert provider.get_tool_schemas() == []


class TestPromptBlock:
    def test_it_does_not_repeat_content_hermes_already_injects(self, managed):
        provider, layout = managed
        layout.soul_file.write_text("# my soul\n", encoding="utf-8")
        layout.user_memory_file.write_text("likes tea\n", encoding="utf-8")
        provider.initialize("session-1", hermes_home=str(layout.hermes_home))

        block = provider.system_prompt_block()
        assert "my soul" not in block
        assert "likes tea" not in block

    def test_it_names_the_workspace(self, managed):
        provider, layout = managed
        provider.initialize("session-1", hermes_home=str(layout.hermes_home))
        assert str(layout.workspace) in provider.system_prompt_block()

    def test_it_mentions_remote_only_files_and_how_to_get_them(self, managed):
        provider, layout = managed
        journal = Journal(layout.journal_file)
        journal.set_materialized(MaterializedFile(
            logical_path="workspace/ref.pdf", base_hash="a" * 64, present=False,
            dirty=False, explicit_delete=False))
        journal.close()
        provider.initialize("session-1", hermes_home=str(layout.hermes_home))

        block = provider.system_prompt_block()
        assert "pubky_file_fetch" in block
        assert "placeholder" in block

    def test_it_warns_that_historical_paths_may_be_from_another_computer(self, managed):
        provider, layout = managed
        provider.initialize("session-1", hermes_home=str(layout.hermes_home))
        assert "previous computer" in provider.system_prompt_block()

    def test_no_recall_is_performed(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        assert provider.prefetch("anything") == ""
        assert provider.on_pre_compress([]) == ""


class TestChangeNotifications:
    def test_a_memory_write_marks_the_real_file_dirty(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home),
                            agent_context="primary")
        provider.on_memory_write("add", "user", "a fact", {})

        journal = Journal(layout.journal_file)
        try:
            assert "profile/memories/USER.md" in journal.dirty_paths()
        finally:
            journal.close()

    def test_the_memory_target_maps_to_its_own_file(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        provider.on_memory_write("add", "memory", "a note", {})
        journal = Journal(layout.journal_file)
        try:
            assert "profile/memories/MEMORY.md" in journal.dirty_paths()
        finally:
            journal.close()

    @pytest.mark.parametrize("context", sorted(NONPRIMARY_CONTEXTS))
    def test_a_nonprimary_context_never_schedules_a_write(self, managed, context):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home),
                            agent_context=context)
        provider.on_memory_write("add", "user", "should not persist", {})
        provider.sync_turn("hi", "hello")

        journal = Journal(layout.journal_file)
        try:
            assert journal.dirty_paths() == []
            assert journal.claim_requests() == []
        finally:
            journal.close()

    def test_an_unmirrored_action_is_ignored(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        provider.on_memory_write("search", "user", "query", {})
        provider.on_memory_write("add", "skills", "not a memory target", {})
        journal = Journal(layout.journal_file)
        try:
            assert journal.dirty_paths() == []
        finally:
            journal.close()

    def test_a_completed_turn_queues_a_capture(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        provider.sync_turn("question", "answer", session_id="s2")

        journal = Journal(layout.journal_file)
        try:
            kinds = [r.kind for r in journal.claim_requests()]
            assert "capture" in kinds
        finally:
            journal.close()

    def test_a_session_switch_marks_the_conversation_changed(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        provider.on_session_switch("s2", rewound=True)

        journal = Journal(layout.journal_file)
        try:
            assert "conversations/state.sqlite3" in journal.dirty_paths()
        finally:
            journal.close()

    def test_the_flattened_turn_strings_are_not_stored_anywhere(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        provider.sync_turn("SECRET-USER-TEXT", "SECRET-ASSISTANT-TEXT")

        blob = layout.journal_file.read_bytes()
        assert b"SECRET-USER-TEXT" not in blob
        assert b"SECRET-ASSISTANT-TEXT" not in blob


class TestTools:
    def test_exactly_two_tools_are_exposed(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        names = [s["name"] for s in provider.get_tool_schemas()]
        assert names == ["pubky_file_list", "pubky_file_fetch"]

    def test_listing_reports_local_and_remote_only_files(self, managed):
        provider, layout = managed
        journal = Journal(layout.journal_file)
        (layout.workspace / "here.md").write_text("x", encoding="utf-8")
        journal.set_materialized(MaterializedFile("workspace/here.md", "a" * 64,
                                                  True, False, False))
        journal.set_materialized(MaterializedFile("workspace/there.pdf", "b" * 64,
                                                  False, False, False))
        journal.close()
        provider.initialize("s", hermes_home=str(layout.hermes_home))

        result = json.loads(provider.handle_tool_call("pubky_file_list", {}))
        states = {f["path"]: f["materialized"] for f in result["files"]}
        assert states == {"here.md": True, "there.pdf": False}

    def test_listing_caps_the_limit(self, managed):
        provider, layout = managed
        journal = Journal(layout.journal_file)
        for index in range(5):
            journal.set_materialized(MaterializedFile(
                f"workspace/f{index}.md", "a" * 64, False, False, False))
        journal.close()
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        result = json.loads(provider.handle_tool_call(
            "pubky_file_list", {"limit": 100000}))
        assert len(result["files"]) == 5

    def test_fetching_an_unknown_path_is_an_error_not_a_download(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        result = json.loads(provider.handle_tool_call(
            "pubky_file_fetch", {"path": "nope.pdf"}))
        assert "not one of this agent's saved files" in result["error"]

    def test_fetching_queues_an_idempotent_request(self, managed):
        provider, layout = managed
        journal = Journal(layout.journal_file)
        journal.set_materialized(MaterializedFile("workspace/ref.pdf", "b" * 64,
                                                  False, False, False))
        journal.close()
        provider.initialize("s", hermes_home=str(layout.hermes_home))

        first = json.loads(provider.handle_tool_call(
            "pubky_file_fetch", {"path": "ref.pdf"}))
        second = json.loads(provider.handle_tool_call(
            "pubky_file_fetch", {"path": "ref.pdf"}))
        assert first["status"] == "pending"
        assert first["request_id"] == second["request_id"], \
            "a repeated fetch must not queue a second download"

    def test_fetching_will_not_overwrite_dirty_local_work(self, managed):
        provider, layout = managed
        journal = Journal(layout.journal_file)
        journal.set_materialized(MaterializedFile("workspace/ref.pdf", "b" * 64,
                                                  True, True, False))
        journal.close()
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        result = json.loads(provider.handle_tool_call(
            "pubky_file_fetch", {"path": "ref.pdf"}))
        assert "local changes" in result["error"]

    def test_an_unknown_tool_returns_an_error(self, managed):
        provider, layout = managed
        provider.initialize("s", hermes_home=str(layout.hermes_home))
        assert "unknown tool" in json.loads(
            provider.handle_tool_call("nope", {}))["error"]

    def test_tools_are_unavailable_when_unmanaged(self, monkeypatch):
        monkeypatch.delenv(CONNECTION_ENV, raising=False)
        provider = PubkyMemoryProvider()
        result = json.loads(provider.handle_tool_call("pubky_file_list", {}))
        assert "hermes-pubky run" in result["error"]
