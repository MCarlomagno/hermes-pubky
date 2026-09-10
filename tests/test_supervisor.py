"""The launcher end to end: prepare, run, save, and the ways that goes wrong.

Every scenario drives the real `Supervisor` against a fake homeserver and a
small stand-in for Hermes that behaves like a turn: it appends to a memory file
and writes a conversation row. The rules under test are the ones a user relies
on: a second run continues the first, offline work chains and drains, a moved
head is never installed over local work, a failed capture or restore is never
reported as saved, and nothing is published that the homeserver could not
reconstruct.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fakes import FakeAgentRemote

from hermes_pubky import hermes_adapter as adapter
from hermes_pubky import supervisor as sv
from hermes_pubky.journal import ConnectionLock, Journal, LockUnavailable, STATE_CONFLICT
from hermes_pubky.models import DATABASE_PATH, PORTABLE_CONFIG_PATH, PortableConfig, SnapshotRef
from hermes_pubky.objects import assemble, hash_bytes
from hermes_pubky.paths import Layout
from hermes_pubky.supervisor import Supervisor

OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"

# A schema-22-shaped conversation database: what the adapter checks for,
# nothing Hermes-specific beyond that.
SCHEMA = """
CREATE TABLE schema_version (version INTEGER);
INSERT INTO schema_version VALUES (22);
CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
CREATE TABLE session_model_usage (id INTEGER PRIMARY KEY, session_id TEXT);
"""

# Stands in for Hermes: one turn appends to MEMORY.md and records a message.
CHILD = textwrap.dedent("""
    import json, os, sqlite3, sys, time
    from pathlib import Path
    home = Path(os.environ["HERMES_HOME"])
    turn = os.environ.get("FAKE_TURN", "turn")
    if os.environ.get("FAKE_NOOP"):
        sys.exit(0)
    with open(home / "memories" / "MEMORY.md", "a") as f:
        f.write(turn + "\\n")
    if (home / "state.db").exists():
        c = sqlite3.connect(home / "state.db")
        c.execute("INSERT INTO messages (session_id, role, content) VALUES ('s1', 'assistant', ?)", (turn,))
        c.commit(); c.close()
    if os.environ.get("FAKE_CAPTURE_REQUEST"):
        sys.path.insert(0, os.environ["FAKE_SRC"])
        from hermes_pubky.journal import Journal
        connection = json.load(open(os.environ["HERMES_PUBKY_CONNECTION"]))
        j = Journal(Path(connection["root"]) / "networks" / connection["network"]
                    / connection["owner"] / "agents" / connection["agentId"] / "journal.sqlite3")
        j.enqueue_request("capture", {"sessionId": "sess-1"}, connection["runId"])
        j.close()
        time.sleep(float(os.environ["FAKE_CAPTURE_REQUEST"]))
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
""")


def messages(db: Path) -> list:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT content FROM messages ORDER BY id")]
    finally:
        conn.close()


class Machine:
    """One computer: a layout and a supervisor bound to the shared fake remote."""

    def __init__(self, root: Path, remote: FakeAgentRemote) -> None:
        self.layout = Layout(root=root, network="testnet", owner=OWNER,
                             agent_id="default").ensure()
        self.remote = remote
        self.supervisor = Supervisor(self.layout, network="testnet",
                                     remote_factory=lambda: remote)

    def seed(self, soul: bytes = b"# agent\n") -> None:
        self.layout.soul_file.write_bytes(soul)
        self.layout.user_memory_file.write_bytes(b"")
        self.layout.agent_memory_file.write_bytes(b"")
        self.layout.agents_md.write_bytes(b"# workspace\n")
        conn = sqlite3.connect(self.layout.state_db)
        conn.executescript(SCHEMA)
        conn.close()

    def init(self, portable: PortableConfig = PortableConfig()) -> None:
        """What `agent init` does: render, seal the seeded copy, publish it."""
        with self.supervisor.session() as s:
            adapter.write_config(self.layout,
                                 adapter.render_config(portable, self.layout, {}))
            result = s.sync()
        assert result.ok, result.detail

    def attach(self) -> None:
        """What `agent attach` does: install the head as the base."""
        head = self.remote.read_head()
        snapshot = self.remote.read_snapshot(SnapshotRef(head.snapshot_id, head.sha256))
        with self.supervisor.session() as s:
            s.install(snapshot)

    def run(self, **kwargs):
        return self.supervisor.run(**kwargs)

    def sync(self, **kwargs):
        with self.supervisor.session() as s:
            return s.sync(**kwargs)

    def journal(self) -> Journal:
        return Journal(self.layout.journal_file)

    def base_id(self) -> str:
        with self.journal() as journal:
            return journal.get_setting("base_snapshot_id")


@pytest.fixture
def remote() -> FakeAgentRemote:
    return FakeAgentRemote()


@pytest.fixture
def machine(tmp_path, remote, monkeypatch, fake_hermes):
    return Machine(tmp_path / "a", remote)


@pytest.fixture
def other(tmp_path, remote, fake_hermes):
    return Machine(tmp_path / "b", remote)


@pytest.fixture
def fake_hermes(monkeypatch):
    """No Hermes installed: the runtime check passes and the child is ours."""
    monkeypatch.setattr(adapter, "assert_supported_runtime", lambda: "0.19.0")
    launches = []

    def launch_command(**kwargs):
        launches.append(kwargs)
        return [sys.executable, "-c", CHILD]

    monkeypatch.setattr(adapter, "launch_command", launch_command)
    monkeypatch.setenv("FAKE_SRC", str(Path(__file__).resolve().parents[1] / "python"))
    return launches


def remote_head_snapshot(remote: FakeAgentRemote):
    head = remote.read_head()
    return remote.read_snapshot(SnapshotRef(head.snapshot_id, head.sha256))


def remote_file(remote: FakeAgentRemote, logical: str, tmp_path: Path) -> bytes:
    """Reassemble a file from what the fake homeserver actually holds."""
    snapshot = remote_head_snapshot(remote)
    scratch = tmp_path / "reassembled"
    scratch.mkdir(exist_ok=True)

    def resolve(reference: str) -> Path:
        path = scratch / reference.replace("/", "_")
        path.write_bytes(remote.objects[reference])
        return path

    out = scratch / logical.replace("/", "_")
    assemble(snapshot.files[logical], resolve, out)
    return out.read_bytes()


# -- the ordinary life of an agent ----------------------------------------------

class TestContinuation:
    def test_a_run_builds_on_the_previous_run(self, machine, monkeypatch, tmp_path):
        machine.seed()
        machine.init()
        first_head = machine.remote.read_head().snapshot_id

        monkeypatch.setenv("FAKE_TURN", "one")
        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        assert machine.remote.read_head().snapshot_id != first_head

        monkeypatch.setenv("FAKE_TURN", "two")
        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        # Both turns are in the working copy and on the homeserver.
        assert messages(machine.layout.state_db) == ["one", "two"]
        machine.layout.state_db.write_bytes(
            remote_file(machine.remote, DATABASE_PATH, tmp_path))
        assert messages(machine.layout.state_db) == ["one", "two"]
        assert remote_head_snapshot(machine.remote).parent is not None

    def test_a_second_machine_attaches_and_continues(self, machine, other,
                                                     monkeypatch, tmp_path):
        machine.seed(b"# from A\n")
        machine.init()

        other.attach()
        assert other.layout.soul_file.read_bytes() == b"# from A\n"
        monkeypatch.setenv("FAKE_TURN", "on B")
        assert other.run(query="hi").exit_code == 0
        monkeypatch.setenv("FAKE_TURN", "on B again")
        assert other.run(query="hi").exit_code == 0
        assert messages(other.layout.state_db) == ["on B", "on B again"]

        # A picks all of it up on its next run, with no conflict.
        monkeypatch.setenv("FAKE_TURN", "back on A")
        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        assert messages(machine.layout.state_db) == ["on B", "on B again", "back on A"]

    def test_a_run_that_changes_nothing_publishes_nothing(self, machine, monkeypatch):
        machine.seed()
        machine.init()
        head = machine.remote.head
        monkeypatch.setenv("FAKE_NOOP", "1")
        result = machine.run(query="hi")
        assert result.exit_code == 0 and result.sync_status == "up-to-date"
        assert machine.remote.head == head
        with machine.journal() as journal:
            assert not journal.has_pending()

    def test_a_database_that_did_not_change_is_not_reuploaded(self, machine):
        machine.seed()
        machine.init()
        before = remote_head_snapshot(machine.remote).files[DATABASE_PATH]
        # Touch the file without changing a row: SQLite rewrites pages.
        conn = sqlite3.connect(machine.layout.state_db)
        conn.execute("INSERT INTO sessions VALUES ('tmp', NULL)")
        conn.execute("DELETE FROM sessions WHERE id = 'tmp'")
        conn.commit()
        conn.close()
        machine.layout.soul_file.write_bytes(b"# edited\n")
        assert machine.sync().ok
        after = remote_head_snapshot(machine.remote).files[DATABASE_PATH]
        assert after == before, "same rows, same descriptor, nothing re-sent"


class TestOffline:
    def test_offline_runs_chain_and_one_sync_drains_them(self, machine, monkeypatch):
        machine.seed()
        machine.init()
        published = machine.remote.read_head().snapshot_id

        monkeypatch.setenv("FAKE_TURN", "first offline")
        assert machine.run(query="hi", offline=True).exit_code == sv.EXIT_SAVED_LOCALLY
        monkeypatch.setenv("FAKE_TURN", "second offline")
        assert machine.run(query="hi", offline=True).exit_code == sv.EXIT_SAVED_LOCALLY

        with machine.journal() as journal:
            pending = journal.active_checkpoints()
        assert len(pending) == 2
        assert pending[0].parent_snapshot_id == published
        assert pending[1].parent_snapshot_id != published, "the second builds on the first"

        result = machine.sync()
        assert result.status == "synced" and result.remaining == 0, result.detail
        assert machine.remote.read_head().sha256 == pending[1].snapshot_hash
        with machine.journal() as journal:
            assert not journal.has_pending()

    def test_the_final_sync_reports_retry_when_its_budget_cannot_drain_the_queue(
            self, machine, monkeypatch):
        machine.seed()
        machine.init()
        monkeypatch.setenv("FAKE_TURN", "offline")
        machine.run(query="hi", offline=True)
        machine.layout.soul_file.write_bytes(b"# more\n")
        with machine.supervisor.session() as s:
            s.seal()
            result = s.sync(deadline=0.0)  # already expired
        assert result.status == "retry"
        assert "pending" in result.detail or "budget" in result.detail
        with machine.journal() as journal:
            assert journal.has_pending(), "nothing was dropped"


# -- what must never be published ------------------------------------------------

class TestIntegrity:
    def test_a_crash_between_journal_writes_leaves_no_half_checkpoint(self, machine,
                                                                     monkeypatch):
        machine.seed()
        machine.init()
        head = machine.remote.head
        machine.layout.soul_file.write_bytes(b"# changed\n")
        with machine.supervisor.session() as s:
            monkeypatch.setattr(s.journal, "record_uploads",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
            with pytest.raises(RuntimeError):
                s.seal()
            monkeypatch.undo()
        with machine.journal() as journal:
            assert journal.active_checkpoints() == [], "nothing publishable survived"
        assert machine.sync().ok
        assert machine.remote.head != head, "the change was sealed cleanly on retry"

    def test_a_checkpoint_naming_unknown_objects_is_refused(self, machine):
        machine.seed()
        machine.init()
        machine.layout.soul_file.write_bytes(b"# changed\n")
        with machine.supervisor.session() as s:
            candidate = s.seal()
            s.journal._execute("DELETE FROM uploads WHERE checkpoint_id = ?",
                               (candidate.checkpoint_id,))
            result = s.sync()
        assert result.status == "blocked" and "never recorded" in result.detail
        assert remote_head_snapshot(machine.remote).files["profile/SOUL.md"].sha256 \
            == hash_bytes(b"# agent\n")

    def test_a_corrupt_database_fails_the_run_and_publishes_nothing(self, machine):
        machine.seed()
        machine.init()
        head = machine.remote.head
        machine.layout.state_db.write_bytes(b"not a database")
        result = machine.run(query="hi")
        assert result.exit_code == sv.EXIT_INTEGRITY
        assert "sealed" in result.detail or "captured" in result.detail
        assert machine.remote.head == head
        assert not machine.layout.agent_memory_file.read_bytes(), "the child never ran"

    def test_a_failed_restore_keeps_the_old_generation_and_base(self, machine, other,
                                                               monkeypatch):
        machine.seed(b"# old\n")
        machine.init()
        other.attach()
        other.layout.soul_file.write_bytes(b"# new\n")
        assert other.sync().ok
        # The new instructions' object is gone from the homeserver.
        new_object = remote_head_snapshot(machine.remote).files["profile/SOUL.md"].pieces[0].object
        del machine.remote.objects[new_object]

        base_before = machine.base_id()
        result = machine.run(query="hi")
        assert result.exit_code == sv.EXIT_INTEGRITY
        assert "left as it was" in result.detail
        assert machine.layout.soul_file.read_bytes() == b"# old\n"
        assert machine.base_id() == base_before

    def test_new_objects_land_in_the_cache_and_pending_is_pruned(self, machine):
        machine.seed()
        machine.init()
        snapshot = remote_head_snapshot(machine.remote)
        for reference in snapshot.objects():
            assert machine.supervisor.cache.has(reference)
        assert not any(machine.layout.pending.iterdir()), "acknowledged work is not kept twice"


# -- two machines ----------------------------------------------------------------

class TestDivergence:
    def test_a_stale_workspace_file_is_refreshed_before_a_run(self, machine, other,
                                                              monkeypatch, tmp_path):
        machine.seed()
        (machine.layout.workspace / "report.md").write_bytes(b"A original\n")
        machine.init()

        other.attach()
        with other.supervisor.session() as s:
            s.fetch_path("workspace/report.md")
        (other.layout.workspace / "report.md").write_bytes(b"B revision\n")
        assert other.sync().ok

        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        assert (machine.layout.workspace / "report.md").read_bytes() == b"B revision\n"
        assert remote_file(machine.remote, "workspace/report.md", tmp_path) == b"B revision\n"

    def test_local_edits_and_a_moved_head_conflict_before_anything_runs(
            self, machine, other, monkeypatch):
        machine.seed(b"# original\n")
        machine.init()
        other.attach()
        other.layout.soul_file.write_bytes(b"# B's instructions\n")
        assert other.sync().ok

        machine.layout.soul_file.write_bytes(b"# A's uncheckpointed edit\n")
        result = machine.run(query="hi")
        assert result.exit_code == sv.EXIT_CONFLICT
        assert "--prefer" in result.detail
        assert machine.layout.soul_file.read_bytes() == b"# A's uncheckpointed edit\n"
        assert not machine.layout.agent_memory_file.read_bytes(), "the child never ran"
        with machine.journal() as journal:
            assert [c.state for c in journal.conflicted_checkpoints()] == [STATE_CONFLICT]

    def test_preferring_remote_installs_it_and_keeps_the_local_side_recoverable(
            self, machine, other):
        machine.seed(b"# original\n")
        machine.init()
        other.attach()
        other.layout.soul_file.write_bytes(b"# B's instructions\n")
        assert other.sync().ok
        machine.layout.soul_file.write_bytes(b"# A's edit\n")
        assert machine.run(query="hi").exit_code == sv.EXIT_CONFLICT

        assert machine.sync(prefer="remote").ok
        assert machine.layout.soul_file.read_bytes() == b"# B's instructions\n"
        assert list(machine.layout.recovery.rglob("local-snapshot.json"))
        assert machine.run(query="hi").exit_code == 0

    def test_preferring_local_republishes_it_on_top_of_the_remote(self, machine, other):
        machine.seed(b"# original\n")
        machine.init()
        other.attach()
        other.layout.soul_file.write_bytes(b"# B's instructions\n")
        assert other.sync().ok
        remote_before = machine.remote.read_head()
        machine.layout.soul_file.write_bytes(b"# A's edit\n")
        assert machine.run(query="hi").exit_code == sv.EXIT_CONFLICT

        result = machine.sync(prefer="local")
        assert result.ok, result.detail
        snapshot = remote_head_snapshot(machine.remote)
        assert snapshot.files["profile/SOUL.md"].sha256 == hash_bytes(b"# A's edit\n")
        assert snapshot.parent.snapshot_id == remote_before.snapshot_id
        assert machine.run(query="hi").exit_code == 0, "the rebased base is consistent"

    def test_deleting_a_materialized_file_propagates(self, machine, other):
        machine.seed()
        skill = machine.layout.skills_dir / "demo" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_bytes(b"# demo\n")
        machine.init()
        assert "profile/skills/demo/SKILL.md" in remote_head_snapshot(machine.remote).files

        skill.unlink()
        assert machine.sync().ok
        assert "profile/skills/demo/SKILL.md" not in remote_head_snapshot(machine.remote).files
        other.attach()
        assert not (other.layout.skills_dir / "demo" / "SKILL.md").exists()


# -- blocked work, the child, and the lock -----------------------------------------

class TestRecovery:
    def test_a_blocked_checkpoint_stays_pending_and_retries_after_remediation(
            self, machine):
        machine.seed()
        machine.init()
        machine.layout.soul_file.write_bytes(b"# changed\n")
        machine.remote.fail_at = "read_head"
        machine.remote.fail_with = lambda: type("PubkyAuthError", (Exception,), {})(
            "homeserver returned 401: grant revoked")
        result = machine.sync()
        assert result.status == "blocked"
        with machine.journal() as journal:
            assert journal.has_pending(), "blocked work is still pending work"
        from hermes_pubky.status import agent_status

        assert agent_status(machine.layout, "testnet", check_remote=False)["state"] \
            == "auth-required"
        # The user logs in again; nothing else changes.
        assert machine.sync().ok

    def test_a_capture_request_during_the_run_seals_a_checkpoint(self, machine,
                                                                 monkeypatch):
        machine.seed()
        machine.init()
        monkeypatch.setattr(sv, "WATCH_INTERVAL", 0.2)
        monkeypatch.setenv("FAKE_CAPTURE_REQUEST", "1.5")
        monkeypatch.setenv("FAKE_TURN", "mid-run")
        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        with machine.journal() as journal:
            requests = journal._rows("SELECT state, result_json FROM requests")
            assert [r["state"] for r in requests] == ["done"]
            assert "checkpointId" in requests[0]["result_json"]
            assert journal.get_setting("last_session_id") == "sess-1"
        assert remote_head_snapshot(machine.remote).last_session_id == "sess-1"

    def test_an_interrupt_still_saves_the_run(self, machine, monkeypatch):
        machine.seed()
        machine.init()
        head = machine.remote.head
        monkeypatch.setenv("FAKE_TURN", "interrupted")
        original = machine.supervisor._launch

        class Interrupting:
            def __init__(self, child):
                self.child = child
                self.raised = False

            def wait(self, timeout=None):
                if not self.raised:
                    self.raised = True
                    self.child.wait()
                    raise KeyboardInterrupt
                return self.child.wait(timeout=timeout)

            def __getattr__(self, name):
                return getattr(self.child, name)

        monkeypatch.setattr(machine.supervisor, "_launch",
                            lambda **kw: Interrupting(original(**kw)))
        result = machine.run(query="hi")
        assert result.exit_code == 0, result.detail
        assert machine.remote.head != head, "the final capture and sync still ran"

    def test_the_device_model_is_launched_but_never_published(self, machine,
                                                              fake_hermes, tmp_path):
        machine.seed()
        machine.init(PortableConfig(model="portable/model"))
        machine.layout.device_config_file.write_text("model: device/model\n")
        assert machine.run(query="hi").exit_code == 0
        assert fake_hermes[-1]["model"] == "device/model"
        published = PortableConfig.parse(
            remote_file(machine.remote, PORTABLE_CONFIG_PATH, tmp_path))
        assert published.model == "portable/model"

    def test_a_running_agent_holds_off_every_management_command(self, machine):
        machine.seed()
        held = ConnectionLock(machine.layout.lock_file)
        held.acquire()
        try:
            assert machine.run(query="hi").exit_code == sv.EXIT_USAGE
            with pytest.raises(LockUnavailable):
                with machine.supervisor.session():
                    pass
        finally:
            held.release()
