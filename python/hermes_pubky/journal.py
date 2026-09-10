"""Durable local bookkeeping: checkpoints, uploads, materialization, requests.

Pending local changes are the only copy of work a user has already done, so
this store is written with `synchronous=FULL` and never treats a read failure
as an empty queue. A corrupt journal raises; it does not silently look like a
fresh agent.

One POSIX advisory lock guards a connection for the length of a managed run or
a management command, because `threading.Lock` does not span processes. Inside
a process the supervisor's watcher and refresh threads share one connection,
serialized by a re-entrant lock, so every statement and every transaction is
whole.

Reference: implementation plan section 6.1.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = 1

# Checkpoint lifecycle. A blocked checkpoint is still pending work: it needs a
# human to fix something (a grant, a quota), after which it is retried. Only
# `conflict` needs an explicit resolution.
STATE_STAGED = "staged"
STATE_UPLOADING = "uploading"
STATE_SNAPSHOT_WRITTEN = "snapshot_written"
STATE_HEAD_WRITTEN = "head_written"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_BLOCKED = "blocked"
STATE_CONFLICT = "conflict"

ACTIVE_STATES = (
    STATE_STAGED, STATE_UPLOADING, STATE_SNAPSHOT_WRITTEN, STATE_HEAD_WRITTEN,
    STATE_BLOCKED,
)
ALL_STATES = ACTIVE_STATES + (STATE_ACKNOWLEDGED, STATE_CONFLICT)

REQUEST_PENDING = "pending"
REQUEST_RUNNING = "running"
REQUEST_DONE = "done"
REQUEST_FAILED = "failed"
REQUEST_INTERRUPTED = "interrupted"

# Settings keys. `base` is the snapshot the working copy corresponds to; it
# advances when a checkpoint is sealed and when a snapshot is installed, and
# never as a side effect of publication.
BASE_SNAPSHOT = "base_snapshot_id"
BASE_DB_DIGEST = "base_db_digest"
BASE_DB_STAT = "base_db_stat"
ACKNOWLEDGED_HEAD = "acknowledged_head"
ACKNOWLEDGED_AT = "acknowledged_at"
LAST_SESSION = "last_session_id"


class JournalError(RuntimeError):
    """The journal could not be read or written."""


class LockUnavailable(RuntimeError):
    """Another process already holds this connection."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    """A checkpoint, device or request id: UUID4 hex, 32 lowercase characters."""
    return uuid.uuid4().hex


@dataclass
class Checkpoint:
    """A sealed candidate and how far its publication got."""

    id: str
    parent_snapshot_id: Optional[str]
    parent_hash: Optional[str]
    snapshot_path: str
    snapshot_hash: str
    state: str
    created_at: str
    last_error: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


@dataclass
class Upload:
    """One object a checkpoint references.

    `acknowledged` means the homeserver is known to hold it: either this
    checkpoint uploaded it, or an earlier published snapshot referenced it.
    """

    checkpoint_id: str
    object_path: str
    sha256: str
    size: int
    local_path: str
    acknowledged: bool


@dataclass
class MaterializedFile:
    """What the working copy holds for one logical path."""

    logical_path: str
    base_hash: str
    present: bool
    dirty: bool
    explicit_delete: bool


@dataclass
class Request:
    """Work the in-process plugin asked the supervisor to do."""

    id: str
    run_id: str
    kind: str
    payload: Dict[str, Any]
    state: str
    result: Dict[str, Any]
    created_at: str


class ConnectionLock:
    """A POSIX advisory lock over one agent connection.

    Held for a whole run or management command. A second local process fails
    rather than interleaving writes to the same journal and working copy.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockUnavailable(
                    f"another hermes-pubky process holds {self.path}; "
                    "only one command per agent at a time"
                ) from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n{utcnow()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ConnectionLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def holder_pid(self) -> Optional[int]:
        """The pid recorded in the lock file, for a useful error message."""
        try:
            first = self.path.read_text(encoding="utf-8").splitlines()[0]
            return int(first)
        except (OSError, IndexError, ValueError):
            return None


class Journal:
    """The durable local store for one agent connection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by every thread of the process, serialized
        # below; SQLite's own per-thread check would otherwise reject the
        # watcher and refresh threads.
        self._lock = threading.RLock()
        self._depth = 0
        self._failed = False
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None,
                                         check_same_thread=False)
        except sqlite3.Error as exc:
            raise JournalError(f"could not open {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _configure(self) -> None:
        # WAL for concurrent readers; FULL because a lost checkpoint reference
        # can mean lost user work. A corrupt file fails here, on the first
        # statement, so this needs the same guard as the migration.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError as exc:
            raise JournalError(
                f"{self.path} is not a usable journal ({exc}); "
                "pending local work may still be present, so it was not replaced"
            ) from exc

    def _migrate(self) -> None:
        try:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    parent_snapshot_id TEXT,
                    parent_hash TEXT,
                    snapshot_path TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
                    object_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    local_path TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (checkpoint_id, object_path)
                );
                CREATE TABLE IF NOT EXISTS materialized (
                    logical_path TEXT PRIMARY KEY,
                    base_hash TEXT NOT NULL DEFAULT '',
                    present INTEGER NOT NULL DEFAULT 0,
                    dirty INTEGER NOT NULL DEFAULT 0,
                    explicit_delete INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_state ON checkpoints(state);
                CREATE INDEX IF NOT EXISTS requests_state ON requests(state);
                """
            )
        except sqlite3.DatabaseError as exc:
            # A corrupt journal must never read as "nothing pending".
            raise JournalError(
                f"{self.path} is not a usable journal ({exc}); "
                "pending local work may still be present, so it was not replaced"
            ) from exc
        self.set_setting("schema", SCHEMA)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- serialized access --------------------------------------------------

    def _execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Any) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def _rows(self, sql: str, params: Any = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _row(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes so a partial update is never visible.

        Re-entrant: nested blocks join the outermost transaction, and a failure
        anywhere inside rolls the whole thing back.
        """
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                self._failed = False
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._failed = True
                raise
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK" if self._failed else "COMMIT")

    # -- settings ----------------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, json.dumps(value)),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._row("SELECT value_json FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    # -- checkpoints -------------------------------------------------------

    def create_checkpoint(
        self,
        *,
        snapshot_path: str,
        snapshot_hash: str,
        parent_snapshot_id: Optional[str],
        parent_hash: Optional[str],
        checkpoint_id: Optional[str] = None,
    ) -> Checkpoint:
        """Record a sealed candidate. Its bytes must already be on disk."""
        checkpoint = Checkpoint(
            id=checkpoint_id or new_id(),
            parent_snapshot_id=parent_snapshot_id,
            parent_hash=parent_hash,
            snapshot_path=snapshot_path,
            snapshot_hash=snapshot_hash,
            state=STATE_STAGED,
            created_at=utcnow(),
        )
        self._execute(
            "INSERT INTO checkpoints (id, parent_snapshot_id, parent_hash, "
            "snapshot_path, snapshot_hash, state, created_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '')",
            (checkpoint.id, checkpoint.parent_snapshot_id, checkpoint.parent_hash,
             checkpoint.snapshot_path, checkpoint.snapshot_hash, checkpoint.state,
             checkpoint.created_at),
        )
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        row = self._row("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return _checkpoint(row) if row else None

    def set_checkpoint_state(self, checkpoint_id: str, state: str,
                             error: str = "") -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown checkpoint state {state!r}")
        self._execute(
            "UPDATE checkpoints SET state = ?, last_error = ? WHERE id = ?",
            (state, error, checkpoint_id),
        )

    def active_checkpoints(self) -> List[Checkpoint]:
        """Candidates not yet on the homeserver, oldest first. Includes blocked."""
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = self._rows(
            f"SELECT * FROM checkpoints WHERE state IN ({placeholders}) "
            "ORDER BY rowid", ACTIVE_STATES)
        return [_checkpoint(r) for r in rows]

    def next_checkpoint(self) -> Optional[Checkpoint]:
        active = self.active_checkpoints()
        return active[0] if active else None

    def conflicted_checkpoints(self) -> List[Checkpoint]:
        rows = self._rows(
            "SELECT * FROM checkpoints WHERE state = ? ORDER BY rowid",
            (STATE_CONFLICT,))
        return [_checkpoint(r) for r in rows]

    def acknowledge_checkpoint(self, checkpoint_id: str) -> None:
        """Mark one checkpoint durable remotely. Later work stays pending."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE checkpoints SET state = ?, last_error = '' WHERE id = ?",
                (STATE_ACKNOWLEDGED, checkpoint_id))
            conn.execute(
                "UPDATE uploads SET acknowledged = 1 WHERE checkpoint_id = ?",
                (checkpoint_id,))

    def has_pending(self) -> bool:
        return bool(self.active_checkpoints() or self.conflicted_checkpoints())

    # -- uploads -----------------------------------------------------------

    def record_uploads(self, checkpoint_id: str, uploads: List[Upload]) -> None:
        self._executemany(
            "INSERT INTO uploads (checkpoint_id, object_path, sha256, size, "
            "local_path, acknowledged) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(checkpoint_id, object_path) DO UPDATE SET "
            "sha256 = excluded.sha256, size = excluded.size, "
            "local_path = excluded.local_path, acknowledged = excluded.acknowledged",
            [(u.checkpoint_id, u.object_path, u.sha256, u.size, u.local_path,
              int(u.acknowledged)) for u in uploads],
        )

    def pending_uploads(self, checkpoint_id: str) -> List[Upload]:
        rows = self._rows(
            "SELECT * FROM uploads WHERE checkpoint_id = ? AND acknowledged = 0 "
            "ORDER BY object_path", (checkpoint_id,))
        return [_upload(r) for r in rows]

    def all_uploads(self, checkpoint_id: str) -> List[Upload]:
        rows = self._rows(
            "SELECT * FROM uploads WHERE checkpoint_id = ? ORDER BY object_path",
            (checkpoint_id,))
        return [_upload(r) for r in rows]

    def mark_uploaded(self, checkpoint_id: str, object_path: str) -> None:
        self._execute(
            "UPDATE uploads SET acknowledged = 1 WHERE checkpoint_id = ? "
            "AND object_path = ?", (checkpoint_id, object_path))

    def protected_objects(self) -> List[str]:
        """Objects any unacknowledged or conflicted checkpoint references.

        The cache must never evict these: for a checkpoint that has not reached
        the homeserver, the local object may be the only copy.
        """
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = self._rows(
            "SELECT DISTINCT object_path FROM uploads WHERE checkpoint_id IN "
            f"(SELECT id FROM checkpoints WHERE state IN ({placeholders}) OR state = ?)",
            (*ACTIVE_STATES, STATE_CONFLICT))
        return [r["object_path"] for r in rows]

    # -- materialization inventory ----------------------------------------

    def set_materialized(self, record: MaterializedFile) -> None:
        self._execute(
            "INSERT INTO materialized (logical_path, base_hash, present, dirty, "
            "explicit_delete) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(logical_path) DO UPDATE SET base_hash = excluded.base_hash, "
            "present = excluded.present, dirty = excluded.dirty, "
            "explicit_delete = excluded.explicit_delete",
            (record.logical_path, record.base_hash, int(record.present),
             int(record.dirty), int(record.explicit_delete)),
        )

    def get_materialized(self, logical_path: str) -> Optional[MaterializedFile]:
        row = self._row("SELECT * FROM materialized WHERE logical_path = ?",
                        (logical_path,))
        return _materialized(row) if row else None

    def list_materialized(self) -> List[MaterializedFile]:
        rows = self._rows("SELECT * FROM materialized ORDER BY logical_path")
        return [_materialized(r) for r in rows]

    def mark_dirty(self, logical_path: str, dirty: bool = True) -> None:
        existing = self.get_materialized(logical_path)
        if existing is None:
            self.set_materialized(MaterializedFile(
                logical_path=logical_path, base_hash="", present=True,
                dirty=dirty, explicit_delete=False))
            return
        self._execute(
            "UPDATE materialized SET dirty = ? WHERE logical_path = ?",
            (int(dirty), logical_path))

    def mark_deleted(self, logical_path: str) -> None:
        """Tombstone a logical path, even one that was never fetched."""
        self._execute(
            "INSERT INTO materialized (logical_path, base_hash, present, dirty, "
            "explicit_delete) VALUES (?, '', 0, 1, 1) "
            "ON CONFLICT(logical_path) DO UPDATE SET present = 0, dirty = 1, "
            "explicit_delete = 1", (logical_path,))

    def forget_materialized(self, logical_path: str) -> None:
        self._execute("DELETE FROM materialized WHERE logical_path = ?",
                      (logical_path,))

    def dirty_paths(self) -> List[str]:
        rows = self._rows(
            "SELECT logical_path FROM materialized WHERE dirty = 1 "
            "ORDER BY logical_path")
        return [r["logical_path"] for r in rows]

    def clear_dirty(self) -> None:
        """After a successful capture, nothing observed is outstanding."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM materialized WHERE explicit_delete = 1")
            conn.execute("UPDATE materialized SET dirty = 0")

    # -- requests ----------------------------------------------------------

    def enqueue_request(self, kind: str, payload: Dict[str, Any], run_id: str,
                        request_id: Optional[str] = None) -> Request:
        """Queue work for the supervisor. Idempotent on `request_id`."""
        request = Request(
            id=request_id or new_id(), run_id=run_id, kind=kind, payload=payload,
            state=REQUEST_PENDING, result={}, created_at=utcnow())
        self._execute(
            "INSERT INTO requests (id, run_id, kind, payload_json, state, "
            "result_json, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?) "
            "ON CONFLICT(id) DO NOTHING",
            (request.id, request.run_id, request.kind, json.dumps(request.payload),
             request.state, request.created_at),
        )
        stored = self.get_request(request.id)
        return stored or request

    def get_request(self, request_id: str) -> Optional[Request]:
        row = self._row("SELECT * FROM requests WHERE id = ?", (request_id,))
        return _request(row) if row else None

    def claim_requests(self, run_id: str = "") -> List[Request]:
        """Take the pending requests, marking them running."""
        with self.transaction():
            if run_id:
                rows = self._rows(
                    "SELECT * FROM requests WHERE state = ? AND run_id = ? "
                    "ORDER BY rowid", (REQUEST_PENDING, run_id))
            else:
                rows = self._rows(
                    "SELECT * FROM requests WHERE state = ? ORDER BY rowid",
                    (REQUEST_PENDING,))
            claimed = [_request(r) for r in rows]
            if claimed:
                self._executemany(
                    "UPDATE requests SET state = ? WHERE id = ?",
                    [(REQUEST_RUNNING, r.id) for r in claimed])
        return claimed

    def finish_request(self, request_id: str, state: str,
                       result: Optional[Dict[str, Any]] = None) -> None:
        if state not in (REQUEST_DONE, REQUEST_FAILED, REQUEST_INTERRUPTED,
                         REQUEST_PENDING):
            raise ValueError(f"unknown request state {state!r}")
        self._execute(
            "UPDATE requests SET state = ?, result_json = ? WHERE id = ?",
            (state, json.dumps(result or {}), request_id))

    def interrupt_running_requests(self) -> int:
        """On supervisor exit, outstanding work becomes visibly interrupted."""
        cursor = self._execute(
            "UPDATE requests SET state = ?, result_json = ? WHERE state IN (?, ?)",
            (REQUEST_INTERRUPTED,
             json.dumps({"error": "the supervisor exited before finishing this request"}),
             REQUEST_RUNNING, REQUEST_PENDING))
        return cursor.rowcount or 0

    # -- generation counter -----------------------------------------------

    def bump_generation(self) -> int:
        """Advance the local write counter.

        Capture compares this before and after reading the working copy; a
        change means a turn intervened and the candidate is discarded.
        """
        with self.transaction():
            current = int(self.get_setting("generation", 0) or 0) + 1
            self.set_setting("generation", current)
        return current

    @property
    def generation(self) -> int:
        return int(self.get_setting("generation", 0) or 0)


def _checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        id=row["id"], parent_snapshot_id=row["parent_snapshot_id"],
        parent_hash=row["parent_hash"], snapshot_path=row["snapshot_path"],
        snapshot_hash=row["snapshot_hash"], state=row["state"],
        created_at=row["created_at"], last_error=row["last_error"] or "")


def _upload(row: sqlite3.Row) -> Upload:
    return Upload(
        checkpoint_id=row["checkpoint_id"], object_path=row["object_path"],
        sha256=row["sha256"], size=row["size"], local_path=row["local_path"],
        acknowledged=bool(row["acknowledged"]))


def _materialized(row: sqlite3.Row) -> MaterializedFile:
    return MaterializedFile(
        logical_path=row["logical_path"], base_hash=row["base_hash"] or "",
        present=bool(row["present"]), dirty=bool(row["dirty"]),
        explicit_delete=bool(row["explicit_delete"]))


def _request(row: sqlite3.Row) -> Request:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    try:
        result = json.loads(row["result_json"])
    except json.JSONDecodeError:
        result = {}
    return Request(
        id=row["id"], run_id=row["run_id"], kind=row["kind"], payload=payload,
        state=row["state"], result=result, created_at=row["created_at"])
