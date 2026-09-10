"""Conversation capture and restore against a real Hermes database.

Uses the pinned fixture scenarios: a tool call/result pair, a rewound message,
a compaction lineage and a workspace path. What must survive is the durable
history; what must not is one machine's live channel state.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_pubky.database import (
    WORKSPACE_MARKER,
    UnsupportedDatabase,
    capture_database,
    logical_digest,
    restore_database,
)
from hermes_pubky.objects import ObjectCache
from hermes_pubky.paths import Layout

OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
FIXTURE_SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_hermes_fixture.py"

try:
    import importlib.util

    HERMES_INSTALLED = importlib.util.find_spec("hermes_state") is not None
except Exception:
    HERMES_INSTALLED = False

live = pytest.mark.skipif(not HERMES_INSTALLED, reason="hermes-agent not installed")


def make_layout(tmp_path: Path) -> Layout:
    return Layout(root=tmp_path / "root", network="testnet", owner=OWNER,
                  agent_id="default").ensure()


def build_real_database(tmp_path: Path) -> Path:
    """Generate a database with the pinned fixture scenarios."""
    out = tmp_path / "fixture.json"
    result = subprocess.run(
        [sys.executable, str(FIXTURE_SCRIPT), "--out", str(out), "--keep"],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    home = None
    for line in result.stdout.splitlines():
        if "HERMES_HOME" in line:
            home = Path(line.split()[-1])
    assert home is not None, result.stdout
    return home / "state.db"


@live
class TestCapture:
    def test_captures_a_real_database_into_chunked_objects(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)

        captured = capture_database(layout, ObjectCache(layout.cached_objects))
        assert captured is not None
        record, objects = captured.record, captured.objects
        assert record.size > 0
        assert all(p.object.endswith(".chunk") for p in record.pieces), \
            "the database is always chunked"
        assert sum(p.size for p in record.pieces) == record.size
        assert objects

    def test_no_database_yields_none(self, tmp_path):
        layout = make_layout(tmp_path)
        assert capture_database(layout, ObjectCache(layout.cached_objects)) is None

    def test_durable_history_survives_normalization(self, tmp_path):
        layout = make_layout(tmp_path)
        source = build_real_database(tmp_path)
        shutil.copy2(source, layout.state_db)
        before = _durable_rows(layout.state_db)

        capture_database(layout, ObjectCache(layout.cached_objects))
        working = layout.staging / "database" / "state.sqlite3"
        after = _durable_rows(working)

        assert before["messages"] == after["messages"], \
            "message rows, order and flags must be untouched"
        assert len(before["sessions"]) == len(after["sessions"])

    def test_machine_specific_session_state_is_cleared(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        capture_database(layout, ObjectCache(layout.cached_objects))
        working = layout.staging / "database" / "state.sqlite3"

        conn = sqlite3.connect(working)
        try:
            row = conn.execute(
                "SELECT session_key, chat_id, display_name, origin_json, "
                "profile_name, handoff_state, model_config, billing_base_url "
                "FROM sessions WHERE session_key IS NOT NULL "
                "OR chat_id IS NOT NULL").fetchone()
            assert row is None, f"live channel state survived: {row}"
            assert conn.execute("SELECT COUNT(*) FROM state_meta").fetchone()[0] == 0
        finally:
            conn.close()

    def test_a_workspace_path_becomes_a_portable_marker(self, tmp_path):
        layout = make_layout(tmp_path)
        source = build_real_database(tmp_path)
        shutil.copy2(source, layout.state_db)

        # Point a session at this layout's workspace so it can be rebased.
        conn = sqlite3.connect(layout.state_db)
        conn.execute("UPDATE sessions SET cwd = ?, git_repo_root = ?",
                     (str(layout.workspace), str(layout.workspace)))
        conn.commit()
        conn.close()

        capture_database(layout, ObjectCache(layout.cached_objects))
        working = layout.staging / "database" / "state.sqlite3"
        conn = sqlite3.connect(working)
        try:
            values = {r[0] for r in conn.execute(
                "SELECT cwd FROM sessions WHERE cwd IS NOT NULL")}
            assert values == {f"{WORKSPACE_MARKER}/"}
        finally:
            conn.close()

    def test_a_path_outside_the_workspace_becomes_null_not_a_guess(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        capture_database(layout, ObjectCache(layout.cached_objects))
        working = layout.staging / "database" / "state.sqlite3"
        conn = sqlite3.connect(working)
        try:
            outside = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE cwd IS NOT NULL "
                "AND cwd NOT LIKE ?", (f"{WORKSPACE_MARKER}%",)).fetchone()[0]
            assert outside == 0
        finally:
            conn.close()

    def test_an_unsupported_schema_is_refused(self, tmp_path):
        layout = make_layout(tmp_path)
        conn = sqlite3.connect(layout.state_db)
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER); "
            "INSERT INTO schema_version VALUES (99);")
        conn.commit()
        conn.close()
        with pytest.raises(UnsupportedDatabase, match="not supported"):
            capture_database(layout, ObjectCache(layout.cached_objects))

    def test_an_unrecognized_table_is_refused(self, tmp_path):
        layout = make_layout(tmp_path)
        source = build_real_database(tmp_path)
        shutil.copy2(source, layout.state_db)
        conn = sqlite3.connect(layout.state_db)
        conn.execute("CREATE TABLE surprise_data (a TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(UnsupportedDatabase, match="unrecognized tables"):
            capture_database(layout, ObjectCache(layout.cached_objects))


@live
class TestLogicalDigest:
    def test_is_stable_across_identical_content(self, tmp_path):
        source = build_real_database(tmp_path)
        one = tmp_path / "a.db"
        two = tmp_path / "b.db"
        shutil.copy2(source, one)
        shutil.copy2(source, two)
        # Touch page layout without changing durable data.
        conn = sqlite3.connect(two)
        conn.execute("VACUUM")
        conn.close()
        assert logical_digest(one) == logical_digest(two), \
            "a layout change must not look like new content"

    def test_changes_when_a_message_is_added(self, tmp_path):
        source = build_real_database(tmp_path)
        path = tmp_path / "a.db"
        shutil.copy2(source, path)
        before = logical_digest(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ((SELECT id FROM sessions LIMIT 1), 'user', 'new', 1.0)")
        conn.commit()
        conn.close()
        assert logical_digest(path) != before


@live
class TestRestore:
    def test_a_round_trip_preserves_the_conversation(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        before = _durable_rows(layout.state_db)

        cache = ObjectCache(layout.cached_objects)
        captured = capture_database(layout, cache)
        record, objects = captured.record, captured.objects

        # Wipe the working database, then restore from the staged objects.
        layout.state_db.unlink()
        restore_database(record, lambda ref: objects[ref], layout)

        after = _durable_rows(layout.state_db)
        assert after["messages"] == before["messages"]
        assert len(after["sessions"]) == len(before["sessions"])

    def test_flags_and_tool_pairing_survive(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        cache = ObjectCache(layout.cached_objects)
        captured = capture_database(layout, cache)
        record, objects = captured.record, captured.objects
        layout.state_db.unlink()
        restore_database(record, lambda ref: objects[ref], layout)

        conn = sqlite3.connect(layout.state_db)
        try:
            inactive = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE active = 0").fetchone()[0]
            compacted = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE compacted = 1").fetchone()[0]
            paired = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE tool_call_id IS NOT NULL"
            ).fetchone()[0]
            lineage = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE parent_session_id IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert inactive >= 2, "rewound and compacted messages must stay inactive"
        assert compacted >= 1
        assert paired >= 1, "a tool result must keep its call id"
        assert lineage >= 1, "compaction lineage must survive"

    def test_the_marker_is_rebased_to_this_workspace(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        conn = sqlite3.connect(layout.state_db)
        conn.execute("UPDATE sessions SET cwd = ?", (str(layout.workspace),))
        conn.commit()
        conn.close()

        cache = ObjectCache(layout.cached_objects)
        captured = capture_database(layout, cache)
        record, objects = captured.record, captured.objects

        # Restore into a different workspace, as a second machine would.
        other = Layout(root=tmp_path / "root2", network="testnet", owner=OWNER,
                       agent_id="default").ensure()
        restore_database(record, lambda ref: objects[ref], other)

        conn = sqlite3.connect(other.state_db)
        try:
            values = {r[0] for r in conn.execute(
                "SELECT cwd FROM sessions WHERE cwd IS NOT NULL")}
        finally:
            conn.close()
        assert values == {str(other.workspace)}

    def test_stale_sidecars_are_not_reused(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        cache = ObjectCache(layout.cached_objects)
        captured = capture_database(layout, cache)
        record, objects = captured.record, captured.objects

        # A leftover WAL from a different database must not survive the swap.
        wal = layout.state_db.with_name(layout.state_db.name + "-wal")
        wal.write_bytes(b"stale wal")
        restore_database(record, lambda ref: objects[ref], layout)
        assert not wal.exists()

    def test_message_text_is_never_rewritten(self, tmp_path):
        layout = make_layout(tmp_path)
        shutil.copy2(build_real_database(tmp_path), layout.state_db)
        conn = sqlite3.connect(layout.state_db)
        conn.execute(
            "UPDATE messages SET content = ? WHERE id = (SELECT MIN(id) FROM messages)",
            (f"I saved it to {layout.workspace}/report.md",))
        conn.commit()
        conn.close()

        cache = ObjectCache(layout.cached_objects)
        captured = capture_database(layout, cache)
        record, objects = captured.record, captured.objects
        other = Layout(root=tmp_path / "root2", network="testnet", owner=OWNER,
                       agent_id="default").ensure()
        restore_database(record, lambda ref: objects[ref], other)

        conn = sqlite3.connect(other.state_db)
        try:
            text = conn.execute(
                "SELECT content FROM messages ORDER BY id LIMIT 1").fetchone()[0]
        finally:
            conn.close()
        assert str(layout.workspace) in text, \
            "historical text is a record of what happened, not a path to rewrite"


class TestCopy:
    def test_committed_wal_rows_are_copied(self, tmp_path):
        from hermes_pubky.database import copy_database

        source = tmp_path / "source.db"
        origin = sqlite3.connect(source)
        origin.execute("PRAGMA journal_mode=WAL")
        origin.execute("CREATE TABLE proof (value TEXT)")
        origin.commit()
        origin.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        origin.execute("INSERT INTO proof VALUES ('committed, still in the WAL')")
        origin.commit()  # not checkpointed: a plain file copy would miss it
        try:
            copy_database(source, tmp_path / "copy.db")
        finally:
            origin.close()
        copied = sqlite3.connect(tmp_path / "copy.db")
        try:
            assert copied.execute("SELECT COUNT(*) FROM proof").fetchone()[0] == 1
        finally:
            copied.close()


def _durable_rows(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        messages = conn.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
            "reasoning, api_content, active, compacted FROM messages ORDER BY id"
        ).fetchall()
        sessions = conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()
        return {"messages": messages, "sessions": sessions}
    finally:
        conn.close()
