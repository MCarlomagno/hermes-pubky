"""The launcher: prepare a working copy, run Hermes, save the result.

The supervisor owns the connection lock, every network call, and every
checkpoint. The in-process plugin only leaves hints in the journal; a lifecycle
callback can fire after another turn has already begun, so nothing here treats
one as proof that the working copy is quiescent.

Local state has three parts. The `base` is the snapshot the working copy
corresponds to; it advances when a checkpoint is sealed and when a snapshot is
installed. Pending checkpoints chain from the base backwards to the last
acknowledged head. The acknowledged head is what the homeserver last confirmed.
Publication moves the acknowledged head; it never moves the base.

Reference: implementation plan sections 4, 6.2 and 7.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import hermes_adapter as adapter
from .database import (
    CapturedDatabase,
    StagedDatabase,
    capture_database,
    install_database,
    stage_database,
)
from .journal import (
    LAST_SESSION,
    REQUEST_DONE,
    REQUEST_FAILED,
    STATE_CONFLICT,
    ConnectionLock,
    Journal,
    LockUnavailable,
    new_id,
)
from .models import (
    DATABASE_PATH,
    PORTABLE_CONFIG_PATH,
    Head,
    PortableConfig,
    SchemaError,
    Snapshot,
    SnapshotRef,
)
from .objects import IntegrityError, ObjectCache, assemble, hash_bytes
from .paths import DeviceIdentity, Layout, read_env_file, write_private
from .projection import Candidate, NoChange, Projection
from .storage import AgentRemote
from .sync import SyncEngine, SyncResult

logger = logging.getLogger("hermes_pubky.supervisor")

# Startup metadata refresh budget (plan 6.4). A valid local copy may continue
# stale rather than block the user. Object transfers for a restore are bounded
# per object, not by this budget: a cold attach legitimately takes as long as
# the conversation database is large.
STARTUP_BUDGET = 5.0
# After the child exits, how long the final sync may take before the run is
# reported as saved-locally-only. Covers the whole operation, every transfer
# included.
FINAL_SYNC_BUDGET = 15.0
# How often the watcher services the plugin's requests.
WATCH_INTERVAL = 2.0
# How long an interrupted child gets to exit on its own before it is stopped.
CHILD_GRACE = 10.0

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


@dataclass
class Prepared:
    """The outcome of reconciling with the homeserver before a run."""

    snapshot: Optional[Snapshot]
    status: str  # current | fast-forwarded | offline | conflict | error
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("conflict", "error")


class Supervisor:
    """One managed run, or one management command, start to finish."""

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
        # Serializes captures: the watcher and the final capture must never
        # seal against the same base at once.
        self._seal_lock = threading.Lock()
        # One session per process: the homeserver keeps a single session per
        # grant, so a second restore would silently revoke the first.
        self._connect_lock = threading.Lock()

    # -- sessions -------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator["Supervisor"]:
        """Hold the connection lock and an open journal for one command.

        Every command that touches the journal or the working copy goes through
        here, so a management command cannot interleave with a running agent.
        """
        self.layout.assert_outside_workspace()
        self._lock.acquire()
        try:
            self.layout.ensure()
            self.journal = Journal(self.layout.journal_file)
            try:
                yield self
            finally:
                self.close()
        finally:
            self._lock.release()

    def close(self) -> None:
        journal, self.journal = self.journal, None
        if journal is not None:
            journal.close()

    # -- entry point --------------------------------------------------------

    def run(self, *, resume: Optional[str] = None, offline: bool = False,
            query: Optional[str] = None) -> RunResult:
        """Prepare, launch, supervise, then save."""
        try:
            with self.session():
                return self._run_locked(resume=resume, offline=offline, query=query)
        except LockUnavailable as exc:
            pid = self._lock.holder_pid()
            return RunResult(exit_code=EXIT_USAGE,
                             detail=f"{exc}" + (f" (pid {pid})" if pid else ""))

    def _run_locked(self, *, resume: Optional[str], offline: bool,
                    query: Optional[str]) -> RunResult:
        try:
            adapter.assert_supported_runtime()
        except SchemaError as exc:
            return RunResult(exit_code=EXIT_INTEGRITY, detail=str(exc))
        self.journal.interrupt_running_requests()

        # 1. Work a crashed run left behind becomes a checkpoint against the
        #    base it was made on, before anything can replace it.
        try:
            self.seal()
        except SchemaError as exc:
            return RunResult(exit_code=EXIT_INTEGRITY,
                             detail=f"local changes could not be sealed: {exc}")

        # 2. Reconcile with the homeserver within the startup budget.
        prepared = self.prepare(offline=offline)
        if prepared.failed:
            return RunResult(exit_code=EXIT_CONFLICT if prepared.status == "conflict"
                             else EXIT_INTEGRITY, detail=prepared.detail)
        if prepared.snapshot is None and not self._has_working_copy():
            return RunResult(
                exit_code=EXIT_USAGE,
                detail="this agent has no local working copy and the homeserver "
                       "could not be reached; a fresh attach needs the network")

        portable = self._portable_from(prepared.snapshot)
        config = self._render_runtime(portable)

        child = self._launch(resume=resume, query=query, config=config)
        watcher = threading.Thread(target=self._watch, name="hermes-pubky-watch",
                                   daemon=True)
        watcher.start()
        try:
            child_exit = child.wait()
        except KeyboardInterrupt:
            child_exit = self._stop_child(child)
        finally:
            self._stop.set()
            watcher.join(timeout=5)
            # Hints the plugin left on its way out are served now, so the
            # session it named lands in the final checkpoint.
            try:
                self._serve_requests()
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes-pubky: final request handling failed: %s", exc)
            interrupted = self.journal.interrupt_running_requests()
            if interrupted:
                logger.debug("hermes-pubky: %d request(s) were interrupted", interrupted)

        result = self._final_capture_and_sync(offline=offline)
        result.child_exit_code = child_exit
        if child_exit != 0 and result.exit_code == EXIT_OK:
            # Preserve the child's failure, and say separately how state ended.
            result.exit_code = child_exit
        self.cache.evict_to_budget(self.journal.protected_objects())
        return result

    # -- preparation --------------------------------------------------------

    def prepare(self, *, offline: bool) -> Prepared:
        """Bring the working copy up to date with the homeserver, or refuse.

        With nothing pending, a newer remote head is installed (fast-forward).
        With pending work, the head must be where that work was built from;
        anything else is a divergence the user has to resolve, and nothing is
        installed over local work.
        """
        base = self._base()
        if offline:
            return self._continue_on(base, "offline")

        head = self._refresh_head()
        if head is None:
            return self._continue_on(base, "offline")

        pending = self.journal.active_checkpoints()
        if pending:
            oldest = pending[0]
            if (head.snapshot_id, head.sha256) == (oldest.parent_snapshot_id,
                                                   oldest.parent_hash):
                return self._continue_on(base, "current")
            if any(c.snapshot_hash == head.sha256 for c in pending):
                # A previous publication lost only its response; the final
                # sync recognizes it as our own.
                return self._continue_on(base, "current")
            self.journal.set_checkpoint_state(
                oldest.id, STATE_CONFLICT,
                f"the homeserver has {head.snapshot_id} but this machine has "
                f"unsaved work built on {oldest.parent_snapshot_id or 'nothing'}")
            return Prepared(
                None, "conflict",
                f"the homeserver has checkpoint {head.snapshot_id[:8]} but this "
                "machine has unsaved work built on an older one. Nothing was "
                "overwritten. Choose with 'hermes-pubky agent sync "
                f"{self.layout.agent_id} --prefer local|remote'.")

        if base is not None and head.snapshot_id == base.snapshot_id \
                and head.sha256 == hash_bytes(base.to_bytes()):
            return self._continue_on(base, "current")

        try:
            snapshot = self._connect().read_snapshot(
                SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))
            self.install(snapshot)
        except Exception as exc:  # noqa: BLE001 - reported, never masked
            return Prepared(
                None, "error",
                f"could not restore checkpoint {head.snapshot_id[:8]}: {exc}. "
                "The working copy was left as it was.")
        return Prepared(snapshot, "fast-forwarded")

    def _continue_on(self, base: Optional[Snapshot], status: str) -> Prepared:
        """Run on the base, making sure its eager files are actually present."""
        if base is None:
            return Prepared(None, status)
        try:
            self.install(base)
        except Exception as exc:  # noqa: BLE001
            if self._has_working_copy():
                logger.warning("hermes-pubky: could not refresh the working copy "
                               "(%s); continuing with what is present", exc)
                return Prepared(base, status)
            return Prepared(None, "error",
                            f"the working copy could not be restored: {exc}")
        return Prepared(base, status)

    def _refresh_head(self) -> Optional[Head]:
        """Read the remote head within the startup budget. None when unreachable."""
        outcome: Dict[str, Any] = {}
        done = threading.Event()

        def refresh() -> None:
            try:
                head = self._connect().read_head()
                outcome["head"] = head
                if head is not None:
                    write_private(self.layout.cached_head, head.to_bytes())
            except Exception as exc:  # noqa: BLE001 - offline is ordinary
                logger.debug("hermes-pubky: startup refresh failed: %s", exc)
            finally:
                done.set()

        threading.Thread(target=refresh, name="hermes-pubky-refresh",
                         daemon=True).start()
        done.wait(timeout=STARTUP_BUDGET)
        return outcome.get("head")

    def install(self, snapshot: Snapshot) -> None:
        """Make the working copy match `snapshot`, verifying everything first.

        The database is assembled and checked, then every eager file, and only
        then are they swapped in and the base advanced. A failure anywhere
        before the swap leaves the previous generation and its base intact.
        """
        projection = self._projection()
        resolve = self._resolve_object(snapshot)
        staged: Optional[StagedDatabase] = None
        if DATABASE_PATH in snapshot.files:
            staged = stage_database(snapshot.files[DATABASE_PATH], resolve, self.layout)
        projection.materialize(snapshot, resolve)
        if staged is not None:
            install_database(staged, self.layout)
        projection.set_base(snapshot, db_digest=staged.digest if staged else None)
        # The generated configuration describes the base; keep it in step so
        # the next capture reads the installed settings back, not stale ones.
        self._render_runtime(self._portable_from(snapshot))

    def fetch_path(self, logical: str) -> Path:
        """Bring one saved file into the working copy and return its path."""
        base = self._base()
        if base is None or logical not in base.files:
            raise SchemaError(f"{logical} is not part of the saved snapshot")
        projection = self._projection()
        projection.materialize(base, self._resolve_object(base), extra_paths=[logical])
        return projection.local_path(logical)

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
            cached.parent.mkdir(parents=True, exist_ok=True)
            self._connect().read_object_to_path(piece, cached)
            return cached

        return resolve

    def _portable_from(self, snapshot: Optional[Snapshot]) -> PortableConfig:
        if snapshot is None or PORTABLE_CONFIG_PATH not in snapshot.files:
            return PortableConfig()
        try:
            target = self.layout.staging / "portable.json"
            assemble(snapshot.files[PORTABLE_CONFIG_PATH],
                     self._resolve_object(snapshot), target)
            return PortableConfig.parse(target.read_bytes())
        except Exception as exc:  # noqa: BLE001 - fall back to defaults
            logger.warning("hermes-pubky: portable settings unreadable (%s); "
                           "using local defaults", exc)
            return PortableConfig()

    def _render_runtime(self, portable: PortableConfig) -> Dict[str, Any]:
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
        return config

    def _has_working_copy(self) -> bool:
        return self.layout.soul_file.is_file() or self.layout.agents_md.is_file()

    # -- the child ----------------------------------------------------------

    def _launch(self, *, resume: Optional[str], query: Optional[str],
                config: Dict[str, Any]) -> subprocess.Popen:
        # The rendered configuration is what the child should run with; the
        # command line repeats it so a device override is never shadowed by
        # the portable model.
        toolsets = config.get("toolsets")
        argv = adapter.launch_command(
            resume=resume, query=query,
            model=config.get("model") or None,
            toolsets=list(toolsets) if isinstance(toolsets, list) else None)
        env = self.layout.child_environment()
        # Model and tool credentials come from this machine, not the snapshot.
        env.update(read_env_file(self.layout.secrets_file))
        return subprocess.Popen(argv, cwd=str(self.layout.workspace), env=env)

    def _stop_child(self, child: subprocess.Popen) -> int:
        """After an interrupt, let the child finish, then make it."""
        for send in (None, child.terminate, child.kill):
            if send is not None:
                try:
                    send()
                except OSError:
                    pass
            try:
                return child.wait(timeout=CHILD_GRACE)
            except subprocess.TimeoutExpired:
                continue
        return child.returncode if child.returncode is not None else -signal.SIGKILL

    def _watch(self) -> None:
        """Service the plugin's requests while the child runs."""
        while not self._stop.wait(WATCH_INTERVAL):
            try:
                self._serve_requests()
            except Exception as exc:  # noqa: BLE001 - a run must not die here
                logger.warning("hermes-pubky: request handling failed: %s", exc)

    def _serve_requests(self) -> None:
        captures = []
        for request in self.journal.claim_requests(self._run_id):
            if request.kind == "file_fetch":
                self._serve_fetch(request)
            elif request.kind == "capture":
                captures.append(request)
            else:
                self.journal.finish_request(
                    request.id, REQUEST_FAILED,
                    {"error": f"unsupported request kind {request.kind!r}"})
        if not captures:
            return
        # Several hints since the last tick mean one capture, not several.
        session_ids = [str(r.payload.get("sessionId") or "") for r in captures]
        session_id = next((s for s in reversed(session_ids) if s), None)
        try:
            candidate = self.seal(last_session_id=session_id)
        except (SchemaError, OSError) as exc:
            for request in captures:
                self.journal.finish_request(request.id, REQUEST_FAILED,
                                            {"error": str(exc)})
            return
        result = ({"checkpointId": candidate.checkpoint_id} if candidate
                  else {"unchanged": True})
        for request in captures:
            self.journal.finish_request(request.id, REQUEST_DONE, result)

    def _serve_fetch(self, request) -> None:
        """Fetch one workspace file into the working copy."""
        logical = str(request.payload.get("logicalPath") or "")
        record = self.journal.get_materialized(logical)
        if record is not None and record.dirty:
            self.journal.finish_request(request.id, REQUEST_FAILED, {
                "error": f"{logical} has local changes; not overwriting them"})
            return
        try:
            local = self.fetch_path(logical)
            self.journal.finish_request(request.id, REQUEST_DONE, {
                "path": str(local),
                "size": local.stat().st_size if local.is_file() else 0,
                "sha256": self._base().files[logical].sha256,
            })
        except Exception as exc:  # noqa: BLE001
            self.journal.finish_request(request.id, REQUEST_FAILED,
                                        {"error": str(exc)})

    # -- saving -------------------------------------------------------------

    def seal(self, *, last_session_id: Optional[str] = None,
             portable: Optional[PortableConfig] = None) -> Optional[Candidate]:
        """Seal local changes as a checkpoint against the base, if any changed.

        Raises `SchemaError` when the working copy cannot be captured, for
        instance a corrupt conversation database. That is never reported as
        "nothing changed".
        """
        with self._seal_lock:
            projection = self._projection()
            base = projection.base()
            if base is None and not self._has_working_copy():
                return None
            if last_session_id:
                self.journal.set_setting(LAST_SESSION, last_session_id)
            database: Optional[CapturedDatabase] = None
            if base is None or DATABASE_PATH not in base.files \
                    or not projection.database_unchanged_by_stat():
                database = capture_database(self.layout, self.cache)
            candidate = projection.capture(
                base,
                last_session_id=self._last_session_id(),
                portable=portable if portable is not None else self._current_portable(),
                database=database,
            )
            return None if isinstance(candidate, NoChange) else candidate

    def sync(self, *, prefer: Optional[str] = None,
             deadline: Optional[float] = None) -> SyncResult:
        """Seal local changes, then publish everything pending.

        With `prefer`, resolve an outstanding conflict first. Keeping the
        remote side also installs it, so the working copy stops describing the
        discarded checkpoint.
        """
        try:
            self.seal()
        except SchemaError as exc:
            return SyncResult(status="blocked", detail=f"local changes could not "
                                                        f"be sealed: {exc}")
        engine = self._engine()
        if prefer is not None:
            result = engine.resolve(prefer)
            if prefer == "remote" and result.ok:
                head = engine.remote.read_head()
                if head is not None:
                    snapshot = engine.remote.read_snapshot(
                        SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))
                    self.install(snapshot)
            return result
        if not self.journal.has_pending():
            return SyncResult(status="up-to-date", detail="nothing to save")
        return engine.sync_with_retries(deadline=deadline)

    def _final_capture_and_sync(self, *, offline: bool) -> RunResult:
        """Capture what the run produced, then publish within the budget."""
        try:
            self.seal()
        except SchemaError as exc:
            return RunResult(exit_code=EXIT_INTEGRITY,
                             detail=f"the run's state could not be captured and "
                                    f"is NOT saved: {exc}")
        except OSError as exc:
            return RunResult(exit_code=EXIT_SAVED_LOCALLY,
                             detail=f"could not seal a checkpoint: {exc}")

        if not self.journal.has_pending():
            return RunResult(exit_code=EXIT_OK, sync_status="up-to-date",
                             detail="nothing changed")
        if offline:
            return RunResult(
                exit_code=EXIT_SAVED_LOCALLY, sync_status="offline",
                detail="saved locally; not yet saved to homeserver")
        try:
            engine = self._engine()
        except Exception as exc:  # noqa: BLE001
            return RunResult(exit_code=EXIT_SAVED_LOCALLY, sync_status="offline",
                             detail=f"saved locally; not yet saved to "
                                    f"homeserver ({exc})")
        result = engine.sync_with_retries(
            deadline=time.monotonic() + FINAL_SYNC_BUDGET)
        return self._to_run_result(result)

    @staticmethod
    def _to_run_result(result: SyncResult) -> RunResult:
        mapping = {
            "synced": EXIT_OK,
            "up-to-date": EXIT_OK,
            "conflict": EXIT_CONFLICT,
            "retry": EXIT_SAVED_LOCALLY,
        }
        code = mapping.get(result.status, EXIT_SAVED_LOCALLY)
        if result.status == "blocked":
            code = blocked_exit_code(result.detail)
        detail = result.detail
        if code == EXIT_SAVED_LOCALLY:
            detail = f"saved locally; not yet saved to homeserver ({detail})"
        return RunResult(exit_code=code, sync_status=result.status, detail=detail,
                         checkpoint_id=result.checkpoint_id)

    def _current_portable(self) -> Optional[PortableConfig]:
        """Read allowlisted settings back out of the generated configuration.

        The generated file has this device's overrides merged in, so a model
        the device chose is not read back as the agent's portable model.
        """
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
        current = self._portable_from(self._base())
        portable = adapter.extract_portable(config, current)
        try:
            device = adapter.load_device_config(self.layout)
        except SchemaError:
            device = {}
        if "model" in device:
            portable.model = current.model
        return portable

    def _last_session_id(self) -> Optional[str]:
        value = self.journal.get_setting(LAST_SESSION)
        return value if isinstance(value, str) and value else None

    # -- helpers ------------------------------------------------------------

    def _base(self) -> Optional[Snapshot]:
        return self._projection().base()

    def _projection(self) -> Projection:
        return Projection(self.layout, self.journal, self.cache,
                          runtime=adapter.runtime_info(),
                          device_id=self.device.device_id)

    def _engine(self) -> SyncEngine:
        return SyncEngine(self.journal, self._connect(), self.cache,
                          recovery_dir=self.layout.recovery,
                          snapshot_dir=self.layout.cached_snapshots)

    def _connect(self) -> AgentRemote:
        with self._connect_lock:
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


def blocked_exit_code(detail: str) -> int:
    """Which exit code a blocked checkpoint's error maps to."""
    lowered = detail.lower()
    if "quota" in lowered or "413" in lowered:
        return EXIT_QUOTA
    if "auth" in lowered or "grant" in lowered or "capabilit" in lowered \
            or "401" in lowered or "403" in lowered:
        return EXIT_AUTH
    return EXIT_INTEGRITY
