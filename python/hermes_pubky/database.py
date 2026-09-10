"""Conversation recovery through verified SQLite snapshots.

Hermes 0.19's session importer skips existing ids and reinserts messages as
active, so exporting and reimporting cannot preserve rewind and compaction
history. Instead this module takes a consistent online backup of the database,
normalizes the state that belongs to one machine, and chunks the result.

Nothing here rewrites message text. A transcript is a record of what happened;
only structured fields are rebased.

Reference: implementation plan section 8 and ADR 0002.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from .models import DATABASE_PATH, FileRecord, SchemaError
from .objects import ObjectCache, assemble, stage_file
from .paths import Layout

logger = logging.getLogger("hermes_pubky.database")

SUPPORTED_SCHEMA = 22
WORKSPACE_MARKER = "pubky-workspace:"

# Durable tables whose rows are the conversation.
DURABLE_TABLES = ("sessions", "messages", "session_model_usage")

# Tables describing one machine's live state. Emptied in the snapshot only.
RUNTIME_TABLES = (
    "gateway_routing", "compression_locks", "async_delegations", "state_meta",
    "telegram_dm_topic_bindings", "telegram_dm_topic_mode",
)

# Session columns that name a live channel or this device.
CLEARED_SESSION_COLUMNS = (
    "session_key", "chat_id", "chat_type", "thread_id", "display_name",
    "origin_json", "profile_name", "handoff_state", "handoff_platform",
    "handoff_error", "compression_failure_error", "model_config",
    "billing_base_url",
)
ZEROED_SESSION_COLUMNS = (
    "expiry_finalized", "compression_fallback_streak",
    "compression_failure_cooldown_until",
)

# Application tables this adapter knows about. Anything else requires an
# explicit decision rather than being uploaded by default.
KNOWN_TABLES = set(DURABLE_TABLES) | set(RUNTIME_TABLES) | {"schema_version"}


class UnsupportedDatabase(SchemaError):
    """The database is not the schema this adapter was validated against."""


@dataclass
class CapturedDatabase:
    """A normalized, chunked copy of the conversation and its logical digest."""

    record: FileRecord
    objects: Dict[str, Path]
    # Over durable rows only, so two captures of the same conversation compare
    # equal even though SQLite rewrote page headers in between.
    digest: str


@dataclass
class StagedDatabase:
    """A verified, rebased database waiting to replace the live one."""

    path: Path
    digest: str


def capture_database(layout: Layout, cache: ObjectCache) -> Optional[CapturedDatabase]:
    """Take a consistent snapshot of the conversation database.

    Returns None only when there is no database at all. A database that exists
    but cannot be captured raises, so a failed capture is never mistaken for a
    conversation that does not exist.
    """
    source = layout.state_db
    if not source.is_file():
        return None

    staging = layout.staging / "database"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    working = staging / "state.sqlite3"

    copy_database(source, working)
    _assert_supported(working)
    _normalize(working, layout)
    _verify_integrity(working)
    digest = logical_digest(working)

    staged = stage_file(working, DATABASE_PATH, staging / "objects",
                        force_chunked=True)
    return CapturedDatabase(record=staged.record, objects=staged.objects,
                            digest=digest)


def copy_database(source: Path, destination: Path) -> None:
    """Copy a database through SQLite's backup API.

    Reading the file directly would miss committed WAL data and could capture a
    torn page. The WAL and SHM sidecars are never copied on their own.
    """
    try:
        origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise UnsupportedDatabase(f"cannot open {source}: {exc}") from exc
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            origin.backup(target)
            # DELETE mode leaves a single self-contained file, so the copy
            # does not depend on sidecars that were never transferred.
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise UnsupportedDatabase(f"backup of {source} failed: {exc}") from exc
    finally:
        origin.close()


def _assert_supported(path: Path) -> None:
    """Refuse a schema this adapter has not been validated against."""
    conn = sqlite3.connect(path)
    try:
        try:
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise UnsupportedDatabase(
                f"{path} has no schema_version table ({exc})") from exc
        if version != SUPPORTED_SCHEMA:
            raise UnsupportedDatabase(
                f"conversation schema {version} is not supported; this adapter "
                f"targets {SUPPORTED_SCHEMA}. Working state was not modified.")
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
        for table in DURABLE_TABLES:
            if table not in present:
                raise UnsupportedDatabase(f"required table {table} is missing")
        unknown = {t for t in present
                   if t not in KNOWN_TABLES and not t.startswith("messages_fts")}
        if unknown:
            raise UnsupportedDatabase(
                f"unrecognized tables {sorted(unknown)}; an adapter decision is "
                "needed before uploading them")
    finally:
        conn.close()


def _normalize(path: Path, layout: Layout) -> None:
    """Strip this machine's live state from the snapshot copy only."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        for table in RUNTIME_TABLES:
            if table in present:
                conn.execute(f"DELETE FROM {table}")

        columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        for column in CLEARED_SESSION_COLUMNS:
            if column in columns:
                conn.execute(f"UPDATE sessions SET {column} = NULL")
        for column in ZEROED_SESSION_COLUMNS:
            if column in columns:
                conn.execute(f"UPDATE sessions SET {column} = 0")

        # Structured workspace paths become portable markers. Paths outside the
        # workspace become null and are reported, not rewritten to a guess.
        workspace = str(layout.workspace)
        for column in ("cwd", "git_repo_root"):
            if column not in columns:
                continue
            for row in conn.execute(
                    f"SELECT id, {column} FROM sessions WHERE {column} IS NOT NULL"):
                session_id, value = row
                if value == workspace:
                    replacement = f"{WORKSPACE_MARKER}/"
                elif value.startswith(workspace + "/"):
                    relative = value[len(workspace) + 1:]
                    replacement = f"{WORKSPACE_MARKER}/{relative}"
                else:
                    replacement = None
                    logger.info(
                        "hermes-pubky: session %s recorded %s outside the "
                        "managed workspace; it will not resolve on another "
                        "computer", session_id, column)
                conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?",
                             (replacement, session_id))
        conn.commit()
    finally:
        conn.close()


def _verify_integrity(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise UnsupportedDatabase(f"integrity check failed: {result}")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise UnsupportedDatabase(
                f"foreign key check failed on {len(broken)} row(s)")
    finally:
        conn.close()


def logical_digest(path: Path) -> str:
    """A deterministic digest over durable rows only.

    SQLite rewrites header counters and page layout on every write, so file
    hashes change when nothing meaningful did. This compares the data instead,
    which is what decides whether a new checkpoint is worth uploading. Run on
    the normalized copy, so the result is the same on every machine.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    digest = hashlib.sha256()
    try:
        for table in DURABLE_TABLES:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            names = ", ".join(f'"{c}"' for c in columns)
            key = "id" if "id" in columns else columns[0]
            digest.update(f"table:{table}:{names}\n".encode("utf-8"))
            for row in conn.execute(f"SELECT {names} FROM {table} ORDER BY {key}"):
                digest.update(repr(row).encode("utf-8"))
                digest.update(b"\n")
    finally:
        conn.close()
    return digest.hexdigest()


def stage_database(record: FileRecord, resolve: Callable[[str], Path],
                   layout: Layout) -> StagedDatabase:
    """Rebuild a snapshot's database beside the live one, fully verified.

    Nothing the child reads is touched: the result waits for `install_database`
    so a restore can verify every file first and then commit them together.
    """
    staging = layout.staging / "restore"
    staging.mkdir(parents=True, exist_ok=True)
    candidate = staging / "state.db"

    # assemble() verifies each chunk and the whole-file hash.
    assemble(record, resolve, candidate)
    _assert_supported(candidate)
    _verify_integrity(candidate)
    # The digest is taken before rebasing, on the same bytes the other machine
    # digested when it captured them.
    digest = logical_digest(candidate)
    _rebase(candidate, layout)
    return StagedDatabase(path=candidate, digest=digest)


def install_database(staged: StagedDatabase, layout: Layout) -> None:
    """Replace the live database with a staged one. Never while a child runs."""
    target = layout.state_db
    previous = staged.path.with_name("previous-state.db")
    if target.is_file():
        shutil.copy2(target, previous)
    # Same-filesystem rename, and the old WAL/SHM sidecars are dropped rather
    # than left to be reused against a different database.
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    staged.path.replace(target)


def restore_database(record: FileRecord, resolve: Callable[[str], Path],
                     layout: Layout) -> str:
    """Stage and install in one step. Returns the logical digest."""
    staged = stage_database(record, resolve, layout)
    install_database(staged, layout)
    return staged.digest


def _rebase(path: Path, layout: Layout) -> None:
    """Turn portable workspace markers back into this machine's paths."""
    conn = sqlite3.connect(path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        workspace = str(layout.workspace)
        for column in ("cwd", "git_repo_root"):
            if column not in columns:
                continue
            for session_id, value in conn.execute(
                    f"SELECT id, {column} FROM sessions WHERE {column} LIKE ?",
                    (f"{WORKSPACE_MARKER}%",)).fetchall():
                relative = value[len(WORKSPACE_MARKER):].lstrip("/")
                resolved = str(Path(workspace) / relative) if relative else workspace
                conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?",
                             (resolved, session_id))
        conn.commit()
    finally:
        conn.close()


def open_or_create(layout: Layout) -> None:
    """Ensure a database exists in the dedicated profile.

    Uses Hermes' own `SessionDB` so the schema is whatever the pinned package
    creates, never a hand-written copy.
    """
    if layout.state_db.is_file():
        return
    try:
        from hermes_state import SessionDB  # type: ignore
    except Exception:  # pragma: no cover - Hermes absent
        return
    db = SessionDB(db_path=layout.state_db)
    db.close()
