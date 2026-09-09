"""The launcher: prepare a working copy, run Hermes, save the result.

The supervisor owns the connection lock, every network call, and the final
capture after the child exits. The in-process plugin only leaves hints in the
journal; a lifecycle callback can fire after another turn has already begun, so
nothing here treats one as proof that the working copy is quiescent.

Reference: implementation plan sections 4, 6.2 and 7.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import hermes_adapter as adapter
from .journal import (
    REQUEST_DONE,
    REQUEST_FAILED,
    ConnectionLock,
    Journal,
    LockUnavailable,
    new_id,
)
from .models import (
    DATABASE_PATH,
    PORTABLE_CONFIG_PATH,
    PortableConfig,
    SchemaError,
    Snapshot,
    SnapshotRef,
)
from .objects import ObjectCache, IntegrityError
from .paths import DeviceIdentity, Layout, read_env_file, write_private
from .projection import NoChange, Projection
from .storage import AgentRemote
from .sync import SyncEngine

logger = logging.getLogger("hermes_pubky.supervisor")

# Startup metadata refresh budget (plan 6.4). A valid local copy may continue
# stale rather than block the user.
STARTUP_BUDGET = 5.0
# After the child exits, how long the final sync may take before the run is
# reported as saved-locally-only. Covers the whole operation, not each object.
FINAL_SYNC_BUDGET = 15.0
# How often to look for local changes while the child runs. A local stat scan,
# never a remote poll.
DIRTY_SCAN_INTERVAL = 2.0

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_SAVED_LOCALLY = 2
EXIT_CONFLICT = 3
EXIT_AUTH = 4
EXIT_QUOTA = 5
EXIT_INTEGRITY = 6


@dataclass
class RunResult:
    """What a managed run did, and how its state ended up."""

    exit_code: int
    child_exit_code: int = 0
    sync_status: str = ""
    detail: str = ""
    checkpoint_id: str = ""


class Supervisor:
    """One managed run, start to finish."""

    def __init__(self, layout: Layout, *, network: str,
                 remote_factory: Optional[Callable[[], AgentRemote]] = None) -> None:
        self.layout = layout
        self.network = network
        self._remote_factory = remote_factory
        self.journal: Optional[Journal] = None
        self.cache = ObjectCache(layout.cached_objects)
        self.device = DeviceIdentity.load_or_create(layout.root)
        self._lock = ConnectionLock(layout.lock_file)
        self._stop = threading.Event()
        self._run_id = new_id()
        self._remote: Optional[AgentRemote] = None
        self._sync_lock = threading.Lock()

    # -- entry point --------------------------------------------------------

    def run(self, *, resume: Optional[str] = None, offline: bool = False,
            query: Optional[str] = None) -> RunResult:
        """Prepare, launch, supervise, then save."""
        self.layout.assert_outside_workspace()
        try:
            self._lock.acquire()
        except LockUnavailable as exc:
            pid = self._lock.holder_pid()
            return RunResult(exit_code=EXIT_USAGE,
                             detail=f"{exc}" + (f" (pid {pid})" if pid else ""))
        try:
            return self._run_locked(resume=resume, offline=offline, query=query)
        finally:
            self._lock.release()

    def _run_locked(self, *, resume: Optional[str], offline: bool,
                    query: Optional[str]) -> RunResult:
        self.layout.ensure()
        self.journal = Journal(self.layout.journal_file)
        try:
            adapter.assert_supported_runtime()
        except SchemaError as exc:
            return RunResult(exit_code=EXIT_INTEGRITY, detail=str(exc))

        # Anything dirty from a crashed run is preserved before it can be
        # replaced by a restore.
        self._preserve_crash_survivors()

        snapshot = self._prepare(offline=offline)
        if snapshot is None and not self._has_working_copy():
            return RunResult(
                exit_code=EXIT_USAGE,
                detail="this agent has no local working copy and the homeserver "
                       "could not be reached; a fresh attach needs the network")

        portable = self._portable_from(snapshot)
        self._render_runtime(portable)

        child = self._launch(resume=resume, query=query,
                             model=portable.model, toolsets=portable.toolsets)
        watcher = threading.Thread(target=self._watch, name="hermes-pubky-watch",
                                   daemon=True)
        watcher.start()
        child_exit = child.wait()
        self._stop.set()
        watcher.join(timeout=5)

        interrupted = self.journal.interrupt_running_requests()
        if interrupted:
            logger.debug("hermes-pubky: %d request(s) were interrupted", interrupted)

        result = self._final_capture_and_sync(offline=offline)
        result.child_exit_code = child_exit
        if child_exit != 0 and result.exit_code == EXIT_OK:
            # Preserve the child's failure, and say separately how state ended.
            result.exit_code = child_exit
        return result

    # -- preparation --------------------------------------------------------

    def _prepare(self, *, offline: bool) -> Optional[Snapshot]:
        """Refresh metadata within budget, then install the working copy."""
        cached = self._cached_snapshot()
        if offline:
            return cached

        fetched: Optional[Snapshot] = None
        done = threading.Event()

        def refresh() -> None:
            nonlocal fetched
            try:
                fetched = self._fetch_head_snapshot()
            except Exception as exc:  # noqa: BLE001 - offline is ordinary
                logger.debug("hermes-pubky: startup refresh failed: %s", exc)
            finally:
                done.set()

        thread = threading.Thread(target=refresh, name="hermes-pubky-refresh",
                                  daemon=True)
        thread.start()
        done.wait(timeout=STARTUP_BUDGET)

        snapshot = fetched or cached
        if snapshot is not None:
            self._install(snapshot)
        return snapshot

    def _fetch_head_snapshot(self) -> Optional[Snapshot]:
        remote = self._connect()
        head = remote.read_head()
        if head is None:
            return None
        snapshot = remote.read_snapshot(
            SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))
        write_private(self.layout.cached_head, head.to_bytes())
        write_private(self.layout.cached_snapshot(snapshot.snapshot_id),
                      snapshot.to_bytes())
        self.journal.set_setting("cached_snapshot_id", snapshot.snapshot_id)
        return snapshot

    def _cached_snapshot(self) -> Optional[Snapshot]:
        snapshot_id = self.journal.get_setting("cached_snapshot_id")
        if not isinstance(snapshot_id, str):
            return None
        raw = self.layout.cached_snapshot(snapshot_id)
        if not raw.is_file():
            return None
        try:
            return Snapshot.parse(raw.read_bytes())
        except SchemaError:
            return None

    def _install(self, snapshot: Snapshot) -> None:
        """Materialize the eager files, fetching objects as needed."""
        projection = self._projection()
        try:
            projection.materialize(snapshot, self._resolve_object(snapshot))
        except (IntegrityError, SchemaError, OSError) as exc:
            logger.warning("hermes-pubky: could not fully restore: %s", exc)

        if DATABASE_PATH in snapshot.files:
            try:
                from .hermes_adapter import restore_database

                restore_database(snapshot.files[DATABASE_PATH],
                                 self._resolve_object(snapshot),
                                 self.layout)
            except Exception as exc:  # noqa: BLE001 - Slice 5 owns this path
                logger.warning("hermes-pubky: conversation restore failed: %s", exc)

    def _resolve_object(self, snapshot: Snapshot) -> Callable[[str], Path]:
        """Resolve an object reference to a verified local file."""
        pieces = {piece.object: piece
                  for record in snapshot.files.values()
                  for piece in record.pieces}

        def resolve(reference: str) -> Path:
            cached = self.cache.path_for(reference)
            if cached.is_file():
                return cached
            piece = pieces.get(reference)
            if piece is None:
                raise IntegrityError(f"{reference} is not part of this snapshot")
            remote = self._connect()
            target = self.cache.path_for(reference)
            target.parent.mkdir(parents=True, exist_ok=True)
            remote.read_object_to_path(piece, target)
            return target

        return resolve

    def _portable_from(self, snapshot: Optional[Snapshot]) -> PortableConfig:
        if snapshot is None or PORTABLE_CONFIG_PATH not in snapshot.files:
            return PortableConfig()
        try:
            resolve = self._resolve_object(snapshot)
            record = snapshot.files[PORTABLE_CONFIG_PATH]
            from .objects import assemble

            target = self.layout.staging / "portable.json"
            assemble(record, resolve, target)
            return PortableConfig.parse(target.read_bytes())
        except Exception as exc:  # noqa: BLE001 - fall back to defaults
            logger.warning("hermes-pubky: portable settings unreadable (%s); "
                           "using local defaults", exc)
            return PortableConfig()

    def _render_runtime(self, portable: PortableConfig) -> None:
        """Write the generated config and the plugin bridge."""
        device = adapter.load_device_config(self.layout)
        config = adapter.render_config(portable, self.layout, device)
        adapter.write_config(self.layout, config)
        from . import __version__

        adapter.write_plugin_shim(self.layout, __version__)
        write_private(self.layout.connection_file, json.dumps({
            "root": str(self.layout.root),
            "network": self.network,
            "owner": self.layout.owner,
            "agentId": self.layout.agent_id,
            "hermesHome": str(self.layout.hermes_home),
            "workspace": str(self.layout.workspace),
            "runId": self._run_id,
            "adapter": adapter.ADAPTER_ID,
        }, indent=2, sort_keys=True).encode("utf-8"))

    def _has_working_copy(self) -> bool:
        return self.layout.soul_file.is_file() or self.layout.agents_md.is_file()

    def _preserve_crash_survivors(self) -> None:
        """Copy dirty state aside before a restore can replace it."""
        projection = self._projection()
        dirty = projection.scan_dirty()
        if not dirty:
            return
        target = self.layout.recovery / f"{new_id()[:8]}-crash"
        for logical in dirty:
            try:
                source = projection.local_path(logical)
            except SchemaError:
                continue
            if not source.is_file():
                continue
            destination = target / logical
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        logger.info("hermes-pubky: preserved %d dirty file(s) under %s",
                    len(dirty), target)

    # -- the child ----------------------------------------------------------

    def _launch(self, *, resume: Optional[str], query: Optional[str],
                model: str, toolsets: List[str]) -> subprocess.Popen:
        argv = adapter.launch_command(resume=resume, query=query,
                                      model=model or None, toolsets=toolsets)
        env = self.layout.child_environment()
        # Model and tool credentials come from this machine, not the snapshot.
        env.update(read_env_file(self.layout.secrets_file))
        return subprocess.Popen(argv, cwd=str(self.layout.workspace), env=env)

    def _watch(self) -> None:
        """Serve journal requests and capture when local state changes."""
        while not self._stop.wait(DIRTY_SCAN_INTERVAL):
            try:
                self._serve_requests()
            except Exception as exc:  # noqa: BLE001 - a run must not die here
                logger.debug("hermes-pubky: request handling failed: %s", exc)

    def _serve_requests(self) -> None:
        for request in self.journal.claim_requests():
            if request.kind == "file_fetch":
                self._serve_fetch(request)
            elif request.kind == "capture":
                self.journal.finish_request(request.id, REQUEST_DONE,
                                            {"queued": True})
            else:
                self.journal.finish_request(
                    request.id, REQUEST_FAILED,
                    {"error": f"unsupported request kind {request.kind!r}"})

    def _serve_fetch(self, request) -> None:
        """Fetch one workspace file into the working copy."""
        logical = str(request.payload.get("logicalPath") or "")
        snapshot = self._cached_snapshot()
        if snapshot is None or logical not in snapshot.files:
            self.journal.finish_request(request.id, REQUEST_FAILED, {
                "error": f"{logical} is not part of the saved snapshot"})
            return
        record = self.journal.get_materialized(logical)
        if record is not None and record.dirty:
            self.journal.finish_request(request.id, REQUEST_FAILED, {
                "error": f"{logical} has local changes; not overwriting them"})
            return
        try:
            projection = self._projection()
            projection.materialize(snapshot, self._resolve_object(snapshot),
                                   extra_paths=[logical])
            local = projection.local_path(logical)
            self.journal.finish_request(request.id, REQUEST_DONE, {
                "path": str(local),
                "size": local.stat().st_size if local.is_file() else 0,
                "sha256": snapshot.files[logical].sha256,
            })
        except Exception as exc:  # noqa: BLE001
            self.journal.finish_request(request.id, REQUEST_FAILED,
                                        {"error": str(exc)})

    # -- saving -------------------------------------------------------------

    def _final_capture_and_sync(self, *, offline: bool) -> RunResult:
        """Capture what the run produced, then publish within the budget."""
        with self._sync_lock:
            base = self._cached_snapshot()
            projection = self._projection()
            try:
                candidate = projection.capture(
                    base,
                    last_session_id=self._last_session_id(),
                    portable=self._current_portable(),
                    database=self._capture_database(),
                )
            except SchemaError as exc:
                return RunResult(exit_code=EXIT_INTEGRITY, detail=str(exc))
            except OSError as exc:
                return RunResult(exit_code=EXIT_SAVED_LOCALLY,
                                 detail=f"could not seal a checkpoint: {exc}")

            if isinstance(candidate, NoChange) and not self.journal.has_pending():
                return RunResult(exit_code=EXIT_OK, sync_status="up-to-date",
                                 detail="nothing changed")

            if offline:
                return RunResult(
                    exit_code=EXIT_SAVED_LOCALLY, sync_status="offline",
                    detail="saved locally; not yet saved to homeserver")

            try:
                engine = SyncEngine(self.journal, self._connect(), self.cache,
                                    recovery_dir=self.layout.recovery)
            except Exception as exc:  # noqa: BLE001
                return RunResult(exit_code=EXIT_SAVED_LOCALLY, sync_status="offline",
                                 detail=f"saved locally; not yet saved to "
                                        f"homeserver ({exc})")

            result = engine.sync_with_retries(
                deadline=time.monotonic() + FINAL_SYNC_BUDGET)
            return self._to_run_result(result)

    @staticmethod
    def _to_run_result(result) -> RunResult:
        mapping = {
            "synced": EXIT_OK,
            "up-to-date": EXIT_OK,
            "conflict": EXIT_CONFLICT,
            "retry": EXIT_SAVED_LOCALLY,
        }
        code = mapping.get(result.status, EXIT_SAVED_LOCALLY)
        if result.status == "blocked":
            lowered = result.detail.lower()
            if "quota" in lowered or "413" in lowered:
                code = EXIT_QUOTA
            elif "auth" in lowered or "grant" in lowered or "capabilit" in lowered:
                code = EXIT_AUTH
            else:
                code = EXIT_INTEGRITY
        detail = result.detail
        if code == EXIT_SAVED_LOCALLY:
            detail = f"saved locally; not yet saved to homeserver ({detail})"
        return RunResult(exit_code=code, sync_status=result.status, detail=detail,
                         checkpoint_id=result.checkpoint_id)

    def _capture_database(self):
        try:
            from .hermes_adapter import capture_database

            return capture_database(self.layout, self.cache)
        except Exception as exc:  # noqa: BLE001 - Slice 5 owns this path
            logger.debug("hermes-pubky: conversation capture skipped: %s", exc)
            return None

    def _current_portable(self) -> Optional[PortableConfig]:
        """Read allowlisted settings back out of the generated configuration."""
        path = self.layout.hermes_config_file
        if not path.is_file():
            return None
        try:
            import yaml

            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(config, dict):
            return None
        return adapter.extract_portable(config)

    def _last_session_id(self) -> Optional[str]:
        value = self.journal.get_setting("last_session_id")
        return value if isinstance(value, str) and value else None

    # -- helpers ------------------------------------------------------------

    def _projection(self) -> Projection:
        return Projection(self.layout, self.journal, self.cache,
                          runtime=adapter.runtime_info(),
                          device_id=self.device.device_id)

    def _connect(self) -> AgentRemote:
        if self._remote is not None:
            return self._remote
        if self._remote_factory is not None:
            self._remote = self._remote_factory()
            return self._remote
        from .paths import GRANT_ENV

        grant = read_env_file(self.layout.credentials_file).get(GRANT_ENV, "")
        if not grant:
            raise RuntimeError(
                "this agent has no stored grant; run "
                f"'hermes-pubky agent login {self.layout.agent_id}'")
        self._remote = AgentRemote.connect(grant, self.layout.owner,
                                           self.layout.agent_id)
        return self._remote

    def close(self) -> None:
        journal, self.journal = self.journal, None
        if journal is not None:
            journal.close()
