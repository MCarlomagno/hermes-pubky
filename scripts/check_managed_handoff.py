#!/usr/bin/env python3
"""The A-to-B acceptance scenario, against a real Pubky testnet.

Machine A builds an agent, saves it, and stops. Machine B starts with nothing
but the same grant, attaches, and recovers the instructions, memories, skills,
settings, conversation database and workspace catalogue. Then B changes
something, saves, and A recovers that.

Needs a running testnet fixture:

    TEST_PUBKY_CONNECTION_STRING=postgres://... \
      cargo run --example testnet_fixture > /tmp/fixture.json &
    HERMES_PUBKY_TESTNET=1 python scripts/check_managed_handoff.py \
      --fixture /tmp/fixture.json

Creates its own scratch roots and refuses to touch the caller's real profile.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

CHECKS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"         {detail}")
        sys.exit(1)
    CHECKS.append(label)


def load_fixture(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"{path} does not contain fixture JSON yet")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True,
                        help="JSON emitted by the testnet fixture")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if os.environ.get("HERMES_PUBKY_TESTNET") != "1":
        raise SystemExit("set HERMES_PUBKY_TESTNET=1 so the SDK uses the testnet")

    fixture = load_fixture(Path(args.fixture))
    owner = fixture["owner"]
    agent_id = fixture["agentId"]
    grant = fixture["agentGrant"]

    from hermes_pubky import hermes_adapter as adapter
    from hermes_pubky.database import capture_database, restore_database
    from hermes_pubky.journal import Journal
    from hermes_pubky.models import PortableConfig, SnapshotRef
    from hermes_pubky.objects import ObjectCache
    from hermes_pubky.paths import DeviceIdentity, Layout, write_private
    from hermes_pubky.projection import NoChange, Projection
    from hermes_pubky.storage import AgentRemote
    from hermes_pubky.sync import SyncEngine

    real_home = (Path.home() / ".hermes").resolve()
    scratch = Path(tempfile.mkdtemp(prefix="hermes-pubky-handoff-"))
    if real_home == scratch or real_home in scratch.parents:
        raise SystemExit(f"refusing to run inside {real_home}")

    print(f"\nA-to-B handoff against the testnet\n  scratch {scratch}\n" + "-" * 60)

    def machine(name: str) -> Layout:
        return Layout(root=scratch / name, network="testnet", owner=owner,
                      agent_id=agent_id).ensure()

    def parts(layout: Layout):
        journal = Journal(layout.journal_file)
        cache = ObjectCache(layout.cached_objects)
        projection = Projection(
            layout, journal, cache, runtime=adapter.runtime_info(),
            device_id=DeviceIdentity.load_or_create(layout.root).device_id)
        remote = AgentRemote.connect(grant, owner, agent_id)
        return journal, cache, projection, remote

    # -- machine A: build and save -------------------------------------------
    a = machine("machine-a")
    a.assert_outside_workspace()
    write_private(a.soul_file, b"# Research assistant\n\nCite sources.\n")
    write_private(a.user_memory_file, b"prefers concise answers\n")
    write_private(a.agent_memory_file, b"the deploy script lives in scripts/\n")
    write_private(a.agents_md, b"# Workspace\n\nReports live here.\n")
    (a.skills_dir / "research").mkdir(parents=True, exist_ok=True)
    write_private(a.skills_dir / "research" / "SKILL.md", b"# research skill\n")
    reference = b"%PDF-1.7\n" + os.urandom(200_000)
    (a.workspace / "reference.pdf").write_bytes(reference)
    (a.workspace / "report.md").write_bytes(b"# Report\n\nFindings.\n")

    from hermes_pubky.database import open_or_create

    open_or_create(a)
    conn = sqlite3.connect(a.state_db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, cwd) VALUES (?,?,?,?)",
            ("a" * 32, "cli", 1.0, str(a.workspace)))
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)", ("a" * 32, "user", "what did we find?", 1.0))
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active, compacted) "
            "VALUES (?,?,?,?,0,1)", ("a" * 32, "user", "folded history", 0.5))
        conn.commit()
    finally:
        conn.close()

    journal_a, cache_a, projection_a, remote_a = parts(a)
    adapter.write_config(a, adapter.render_config(PortableConfig(model=""), a, {}))

    # Re-runnable against the same fixture: build on whatever head exists, so a
    # second run is ordinary forward progress rather than a false conflict.
    existing = remote_a.read_head()
    base_a = None
    if existing is not None:
        base_a = remote_a.read_snapshot(
            SnapshotRef(snapshot_id=existing.snapshot_id, sha256=existing.sha256))
        write_private(a.cached_snapshot(base_a.snapshot_id), base_a.to_bytes())
        journal_a.set_setting("cached_snapshot_id", base_a.snapshot_id)

    candidate = projection_a.capture(
        base_a, name="Research assistant", last_session_id="a" * 32,
        portable=PortableConfig(model="", toolsets=["hermes-cli"]),
        database=capture_database(a, cache_a))
    check("machine A sealed a checkpoint", not isinstance(candidate, NoChange))

    engine_a = SyncEngine(journal_a, remote_a, cache_a, recovery_dir=a.recovery)
    result = engine_a.sync_with_retries()
    check("machine A published it", result.ok, result.detail)
    checkpoint_id = result.snapshot_id
    print(f"         checkpoint {checkpoint_id}")
    journal_a.close()

    # -- machine B: recover from nothing but the grant ------------------------
    b = machine("machine-b")
    check("machine B starts with no working copy", not b.soul_file.exists())

    journal_b, cache_b, projection_b, remote_b = parts(b)
    head = remote_b.read_head()
    check("machine B reads the head", head is not None
          and head.snapshot_id == checkpoint_id)
    snapshot = remote_b.read_snapshot(
        SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))
    write_private(b.cached_snapshot(snapshot.snapshot_id), snapshot.to_bytes())
    journal_b.set_setting("cached_snapshot_id", snapshot.snapshot_id)

    pieces = {p.object: p for r in snapshot.files.values() for p in r.pieces}

    def resolve_b(reference_name: str) -> Path:
        cached = cache_b.path_for(reference_name)
        if cached.is_file():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        remote_b.read_object_to_path(pieces[reference_name], cached)
        return cached

    projection_b.materialize(snapshot, resolve_b)
    check("instructions recovered byte for byte",
          b.soul_file.read_bytes() == a.soul_file.read_bytes())
    check("both memory files recovered byte for byte",
          b.user_memory_file.read_bytes() == a.user_memory_file.read_bytes()
          and b.agent_memory_file.read_bytes() == a.agent_memory_file.read_bytes())
    check("the skill recovered",
          (b.skills_dir / "research" / "SKILL.md").read_bytes()
          == (a.skills_dir / "research" / "SKILL.md").read_bytes())
    check("workspace instructions recovered",
          b.agents_md.read_bytes() == a.agents_md.read_bytes())

    catalogue = {r.logical_path: r for r in journal_b.list_materialized()}
    check("the workspace catalogue lists files not yet fetched",
          catalogue["workspace/reference.pdf"].present is False,
          "a cold attach must not download every document")
    check("no workspace document was downloaded eagerly",
          not (b.workspace / "reference.pdf").exists())

    restore_database(snapshot.files["conversations/state.sqlite3"], resolve_b, b)
    conn = sqlite3.connect(f"file:{b.state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT role, content, active, compacted FROM messages ORDER BY id"
        ).fetchall()
        rebased = conn.execute(
            "SELECT cwd FROM sessions WHERE id = ?", ("a" * 32,)).fetchone()[0]
    finally:
        conn.close()
    check("the conversation recovered with its history", len(rows) == 2, str(rows))
    compacted = [r for r in rows if r[3] == 1]
    active = [r for r in rows if r[2] == 1]
    check("a compacted message stayed inactive",
          len(compacted) == 1 and compacted[0][2] == 0, str(rows))
    check("the live message stayed active", len(active) == 1, str(rows))
    check("the recorded working directory was rebased to machine B",
          rebased == str(b.workspace), rebased)

    # Fetch one document on demand and verify its hash.
    projection_b.materialize(snapshot, resolve_b,
                             extra_paths=["workspace/reference.pdf"])
    fetched = (b.workspace / "reference.pdf").read_bytes()
    check("a workspace document fetches on demand with the right bytes",
          fetched == reference, f"{len(fetched)} vs {len(reference)} bytes")

    # -- machine B makes a change and saves ----------------------------------
    (b.workspace / "report.md").write_bytes(b"# Report\n\nFindings, revised on B.\n")
    journal_b.mark_dirty("workspace/report.md")
    journal_b.bump_generation()
    candidate_b = projection_b.capture(snapshot, database=capture_database(b, cache_b))
    check("machine B sealed a checkpoint", not isinstance(candidate_b, NoChange))
    engine_b = SyncEngine(journal_b, remote_b, cache_b, recovery_dir=b.recovery)
    result_b = engine_b.sync_with_retries()
    check("machine B published it", result_b.ok, result_b.detail)
    journal_b.close()

    # -- machine A recovers B's change ---------------------------------------
    journal_a2, cache_a2, projection_a2, remote_a2 = parts(a)
    head2 = remote_a2.read_head()
    check("machine A sees the new checkpoint",
          head2 is not None and head2.snapshot_id == result_b.snapshot_id)
    snapshot2 = remote_a2.read_snapshot(
        SnapshotRef(snapshot_id=head2.snapshot_id, sha256=head2.sha256))
    pieces2 = {p.object: p for r in snapshot2.files.values() for p in r.pieces}

    def resolve_a(reference_name: str) -> Path:
        cached = cache_a2.path_for(reference_name)
        if cached.is_file():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        remote_a2.read_object_to_path(pieces2[reference_name], cached)
        return cached

    # A already has report.md at the old content and it is clean, so a refresh
    # must replace it rather than leave stale bytes for Hermes to read.
    journal_a2.set_materialized(type(catalogue["workspace/report.md"])(
        logical_path="workspace/report.md",
        base_hash=snapshot.files["workspace/report.md"].sha256,
        present=True, dirty=False, explicit_delete=False))
    projection_a2.materialize(snapshot2, resolve_a,
                              extra_paths=["workspace/report.md"])
    check("machine A recovers the change made on B",
          (a.workspace / "report.md").read_bytes()
          == b"# Report\n\nFindings, revised on B.\n",
          (a.workspace / "report.md").read_bytes().decode())
    journal_a2.close()

    print("-" * 60)
    print(f"  {len(CHECKS)} checks passed\n")
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
