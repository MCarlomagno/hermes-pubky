#!/usr/bin/env python3
"""Generate the pinned Hermes conversation-database fixture.

Slice 0 of the 0.2 plan: capture what Hermes 0.19.0 actually writes, so the
adapter's normalization and restore logic can be tested against recorded
values instead of assumptions.

Builds five scenarios in an isolated HERMES_HOME -- plain turns, a tool
call/result pair, rewound (inactive) messages, a compaction lineage, and a
workspace cwd -- then records the schema shape and the durable rows.

    python scripts/generate_hermes_fixture.py --out tests/fixtures/hermes_0_19_0_schema22.json

Refuses to run against the caller's real Hermes home.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Tables whose contents must survive a handoff, versus tables the adapter
# clears because they describe one machine's live state.
DURABLE_TABLES = ("sessions", "messages", "session_model_usage")
RUNTIME_TABLES = (
    "state_meta",
    "gateway_routing",
    "compression_locks",
    "async_delegations",
    "telegram_dm_topic_bindings",
    "telegram_dm_topic_mode",
)


def isolate(root: Path) -> None:
    """Point Hermes at `root`, refusing to touch the caller's real profile.

    `hermes_state.DEFAULT_DB_PATH` is bound at import time, so this has to run
    before any Hermes module is imported.
    """
    real = Path.home() / ".hermes"
    resolved = root.resolve()
    if resolved == real.resolve() or real.resolve() in resolved.parents:
        raise SystemExit(f"refusing to use a fixture home inside {real}")
    os.environ["HERMES_HOME"] = str(resolved)
    for leaked in ("HERMES_PUBKY_GRANT_SECRET", "HERMES_PROFILE"):
        os.environ.pop(leaked, None)
    for module in list(sys.modules):
        if module.startswith(("hermes_", "agent", "tools")):
            raise SystemExit(f"Hermes already imported ({module}); cannot isolate")


def build(db, workspace: Path) -> dict:
    """Write the five scenarios through the public API.

    Returns the ids the second phase needs. Flag adjustments happen in
    `adjust()` over plain sqlite3 rather than through `SessionDB._conn`, so the
    fixture does not depend on Hermes internals.
    """
    ids: dict = {"rows": {}}

    # 1. A plain conversation.
    plain = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    db.create_session(plain, "cli", cwd=str(workspace))
    db.append_message(plain, "user", content="what is in the workspace?")
    db.append_message(plain, "assistant", content="A report and two references.",
                      token_count=11, finish_reason="stop")
    ids["plain"] = plain

    # 2. A tool call and its result, which must stay paired after restore.
    tools = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    db.create_session(tools, "cli", cwd=str(workspace))
    db.append_message(tools, "user", content="read notes.md")
    db.append_message(
        tools, "assistant",
        tool_calls=[{"id": "call_1", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"path":"notes.md"}'}}],
        reasoning="The file is in the workspace.",
        api_content="assistant api payload",
    )
    db.append_message(tools, "tool", content="notes body", tool_name="read_file",
                      tool_call_id="call_1")
    ids["tools"] = tools

    # 3. Rewound messages stay in the row set but inactive; a restore that
    #    reactivated them would resurrect undone work.
    rewound = "cccccccccccccccccccccccccccccccc"
    db.create_session(rewound, "cli", cwd=str(workspace))
    db.append_message(rewound, "user", content="kept")
    ids["rows"]["undone"] = db.append_message(rewound, "user",
                                              content="undone by a rewind")
    ids["rewound"] = rewound

    # 4. A compaction lineage: a child session referencing its parent, with the
    #    folded messages flagged.
    parent = "dddddddddddddddddddddddddddddddd"
    child = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    db.create_session(parent, "cli", cwd=str(workspace))
    ids["rows"]["folded"] = db.append_message(
        parent, "user", content="long history that was compacted")
    db.create_session(child, "cli", cwd=str(workspace))
    db.append_message(child, "user", content="continues after compaction")
    ids["compaction_parent"] = parent
    ids["compaction_child"] = child
    return ids


def adjust(db_path: Path, ids: dict, workspace: Path) -> None:
    """Set the flags and machine-specific fields the public API does not expose.

    The machine-specific values exist so the fixture proves normalization
    actually removes something.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE messages SET active = 0 WHERE id = ?",
                     (ids["rows"]["undone"],))
        conn.execute("UPDATE sessions SET rewind_count = 1 WHERE id = ?",
                     (ids["rewound"],))
        conn.execute("UPDATE messages SET compacted = 1, active = 0 WHERE id = ?",
                     (ids["rows"]["folded"],))
        conn.execute("UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                     (ids["compaction_parent"], ids["compaction_child"]))
        conn.execute(
            "UPDATE sessions SET session_key = ?, chat_id = ?, display_name = ?, "
            "origin_json = ?, profile_name = ?, handoff_state = ?, model_config = ?, "
            "billing_base_url = ?, git_repo_root = ?, title = ? WHERE id = ?",
            ("telegram:42", "42", "Marcos", '{"platform":"telegram"}', "coder",
             "pending", '{"api_key_ref":"local"}', "https://local.endpoint/v1",
             str(workspace), "Workspace questions", ids["plain"]),
        )
        conn.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                     ("last_device", "machine-a"))
        conn.commit()
    finally:
        conn.close()


def record(db_path: Path, ids: dict, workspace: Path) -> dict:
    """Read back the schema shape and durable rows as the fixture."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        tables = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
        columns = {
            t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
            for t in DURABLE_TABLES
        }
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions ORDER BY id")]
        messages = [dict(r) for r in conn.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, tool_name, "
            "reasoning, api_content, active, compacted FROM messages ORDER BY id")]
        return {
            "generatedBy": "scripts/generate_hermes_fixture.py",
            "hermesVersion": "0.19.0",
            "schemaVersion": version,
            "adapterId": "hermes-0.19-sqlite22-v1",
            "tables": tables,
            "durableTables": list(DURABLE_TABLES),
            "runtimeTables": [t for t in RUNTIME_TABLES if t in tables],
            "columns": columns,
            "scenarios": ids,
            "workspace": str(workspace),
            "sessions": sessions,
            "messages": messages,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="tests/fixtures/hermes_0_19_0_schema22.json")
    ap.add_argument("--keep", action="store_true", help="keep the temporary home")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="hermes-fixture-"))
    home = tmp / "home"
    workspace = tmp / "workspace"
    (home / "memories").mkdir(parents=True)
    workspace.mkdir()
    isolate(home)

    from hermes_constants import get_hermes_home
    from hermes_state import SCHEMA_VERSION, SessionDB

    resolved = Path(get_hermes_home())
    if resolved != home.resolve():
        raise SystemExit(f"HERMES_HOME not honored: {resolved} != {home.resolve()}")

    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        ids = build(db, workspace)
    finally:
        db.close()
    adjust(db_path, ids, workspace)

    fixture = record(db_path, ids, workspace)
    if fixture["schemaVersion"] != SCHEMA_VERSION:
        raise SystemExit(
            f"recorded schema {fixture['schemaVersion']} != package {SCHEMA_VERSION}"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"  HERMES_HOME     {resolved}")
    print(f"  schema version  {fixture['schemaVersion']}")
    print(f"  tables          {len(fixture['tables'])}")
    print(f"  sessions        {len(fixture['sessions'])}")
    print(f"  messages        {len(fixture['messages'])}")
    print(f"  wrote           {out}")
    if not args.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
