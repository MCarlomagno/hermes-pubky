"""What belongs in a checkpoint, and how a snapshot becomes a working copy.

Capture scans an allowlist of sources, never a recursive walk of the management
root. Materialization assembles and verifies a whole generation in staging
before a single file is swapped in, so a failed restore leaves the previous
working copy intact and the journal still describing it.

The journal's `base` is the snapshot the working copy corresponds to. Sealing a
checkpoint advances it, so consecutive captures chain, and installing a snapshot
advances it, so the next capture builds on what was installed.

Reference: implementation plan sections 6.2, 8.4 and 11.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .database import CapturedDatabase
from .journal import (
    BASE_DB_DIGEST,
    BASE_DB_STAT,
    BASE_SNAPSHOT,
    Journal,
    MaterializedFile,
    Upload,
    new_id,
    utcnow,
)
from .models import (
    DATABASE_PATH,
    PORTABLE_CONFIG_PATH,
    FileRecord,
    PortableConfig,
    RuntimeInfo,
    SchemaError,
    Snapshot,
    SnapshotRef,
    TemplateOrigin,
    normalize_new_path,
)
from .objects import ObjectCache, StagedFile, assemble, hash_bytes, hash_file, stage_file
from .paths import Layout, executable_bit, write_private

# Logical prefixes and their local homes.
PROFILE_PREFIX = "profile/"
WORKSPACE_PREFIX = "workspace/"
CONFIG_PREFIX = "config/"
CONVERSATIONS_PREFIX = "conversations/"

# Core files, restored eagerly because Hermes reads them before any hook runs.
SOUL_PATH = "profile/SOUL.md"
USER_MEMORY_PATH = "profile/memories/USER.md"
AGENT_MEMORY_PATH = "profile/memories/MEMORY.md"
AGENTS_MD_PATH = "workspace/AGENTS.md"
SKILLS_PREFIX = "profile/skills/"

EAGER_PATHS = (SOUL_PATH, USER_MEMORY_PATH, AGENT_MEMORY_PATH, AGENTS_MD_PATH,
               PORTABLE_CONFIG_PATH, DATABASE_PATH)

# Exclusions (plan 11). An explicit default policy, not a claim to detect every
# possible secret.
EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", "target", "dist", "build",
})
EXCLUDED_NAMES = frozenset({
    ".DS_Store", ".env", "credentials.env", "secrets.env", "auth.json",
    "credentials.json", "id_rsa", "id_ed25519",
})
EXCLUDED_SUFFIXES = (".swp", ".swo", ".tmp", "~")
EXCLUDED_PREFIXES = (".env.",)

IGNORE_FILE = ".pubkyignore"

PEM_MARKERS = (b"-----BEGIN RSA PRIVATE KEY", b"-----BEGIN OPENSSH PRIVATE KEY",
               b"-----BEGIN PRIVATE KEY", b"-----BEGIN EC PRIVATE KEY",
               b"-----BEGIN DSA PRIVATE KEY", b"-----BEGIN PGP PRIVATE KEY")


class ExcludedByPolicy(Exception):
    """A file is outside this release's portability boundary."""


@dataclass
class Ignore:
    """`.pubkyignore`: exact workspace-relative paths, or directory prefixes.

    Deliberately not gitignore. No wildcards, no negation, no traversal, so a
    rule can never quietly widen or narrow what is uploaded.
    """

    paths: Set[str] = field(default_factory=set)
    prefixes: List[str] = field(default_factory=list)

    @staticmethod
    def parse(text: str) -> "Ignore":
        ignore = Ignore()
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if any(ch in line for ch in "*?[]!") or line.startswith("/") \
                    or "\\" in line or ".." in line.split("/"):
                raise SchemaError(
                    f"{IGNORE_FILE} line {number}: wildcards, negation, absolute "
                    f"paths and traversal are not supported (got {line!r})")
            if line.endswith("/"):
                ignore.prefixes.append(line)
            else:
                ignore.paths.add(line)
        return ignore

    @staticmethod
    def load(workspace: Path) -> "Ignore":
        path = workspace / IGNORE_FILE
        try:
            return Ignore.parse(path.read_text(encoding="utf-8"))
        except OSError:
            return Ignore()

    def excludes(self, relative: str) -> bool:
        if relative in self.paths:
            return True
        return any(relative.startswith(prefix) for prefix in self.prefixes)


def excluded_reason(path: Path, relative: str, ignore: Ignore) -> Optional[str]:
    """Why a file is not included, or None when it is."""
    parts = relative.split("/")
    for segment in parts[:-1]:
        if segment in EXCLUDED_DIRS:
            return f"inside {segment}/"
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return "a credential or noise file by name"
    if name.startswith(EXCLUDED_PREFIXES):
        return "an environment file"
    if name.endswith(EXCLUDED_SUFFIXES):
        return "an editor or temporary file"
    if ignore.excludes(relative):
        return f"listed in {IGNORE_FILE}"
    if not path.is_file() or path.is_symlink():
        return "not a regular file"
    return None


def looks_like_private_key(path: Path) -> bool:
    """Detect a PEM private key header in a file being explicitly imported."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(200)
    except OSError:
        return False
    return any(marker in head for marker in PEM_MARKERS)


@dataclass
class Inventory:
    """One logical path and the local file backing it."""

    logical_path: str
    source: Path
    executable: bool = False


@dataclass
class Candidate:
    """A sealed checkpoint: a snapshot document plus staged objects."""

    checkpoint_id: str
    snapshot: Snapshot
    snapshot_path: Path
    snapshot_hash: str
    uploads: List[Upload]

    @property
    def object_count(self) -> int:
        """Objects this checkpoint has to upload."""
        return sum(1 for u in self.uploads if not u.acknowledged)


class NoChange:
    """Returned when nothing worth a new checkpoint has changed."""

    def __bool__(self) -> bool:
        return False


def scan_workspace(workspace: Path, ignore: Optional[Ignore] = None
                   ) -> Tuple[List[Inventory], List[Tuple[str, str]]]:
    """List includable workspace files and the exclusions to report.

    Links are never followed: a symlink out of the workspace would collect
    content from elsewhere on the machine.
    """
    ignore = ignore or Ignore.load(workspace)
    included: List[Inventory] = []
    excluded: List[Tuple[str, str]] = []
    if not workspace.is_dir():
        return included, excluded

    for path in sorted(workspace.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            continue
        reason = excluded_reason(path, relative, ignore)
        if reason:
            excluded.append((relative, reason))
            continue
        included.append(Inventory(
            logical_path=f"{WORKSPACE_PREFIX}{normalize_new_path(relative)}",
            source=path, executable=executable_bit(path)))
    return included, excluded


def scan_skills(skills_dir: Path) -> Tuple[List[Inventory], List[Tuple[str, str]]]:
    """List managed skill files. Skills use the fixed exclusion list only."""
    included: List[Inventory] = []
    excluded: List[Tuple[str, str]] = []
    if not skills_dir.is_dir():
        return included, excluded
    empty = Ignore()
    for path in sorted(skills_dir.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(skills_dir).as_posix()
        reason = excluded_reason(path, relative, empty)
        if reason:
            excluded.append((relative, reason))
            continue
        included.append(Inventory(
            logical_path=f"{SKILLS_PREFIX}{normalize_new_path(relative)}",
            source=path, executable=executable_bit(path)))
    return included, excluded


def core_inventory(layout: Layout) -> List[Inventory]:
    """The always-considered core files, when they exist locally."""
    pairs = [
        (SOUL_PATH, layout.soul_file),
        (USER_MEMORY_PATH, layout.user_memory_file),
        (AGENT_MEMORY_PATH, layout.agent_memory_file),
    ]
    return [Inventory(logical_path=logical, source=source)
            for logical, source in pairs if source.is_file()]


class Projection:
    """Captures a working copy into a candidate, and installs one back."""

    def __init__(self, layout: Layout, journal: Journal, cache: ObjectCache,
                 *, runtime: RuntimeInfo, device_id: str) -> None:
        self.layout = layout
        self.journal = journal
        self.cache = cache
        self.runtime = runtime
        self.device_id = device_id

    # -- the base -------------------------------------------------------------

    def base(self) -> Optional[Snapshot]:
        """The snapshot the working copy corresponds to, if any."""
        snapshot_id = self.journal.get_setting(BASE_SNAPSHOT)
        if not isinstance(snapshot_id, str):
            return None
        raw = self.layout.cached_snapshot(snapshot_id)
        if not raw.is_file():
            return None
        try:
            return Snapshot.parse(raw.read_bytes())
        except SchemaError:
            return None

    def set_base(self, snapshot: Snapshot, *, db_digest: Optional[str]) -> None:
        """Record that the working copy now corresponds to `snapshot`."""
        write_private(self.layout.cached_snapshot(snapshot.snapshot_id),
                      snapshot.to_bytes())
        with self.journal.transaction():
            self.journal.set_setting(BASE_SNAPSHOT, snapshot.snapshot_id)
            self.journal.set_setting(BASE_DB_DIGEST, db_digest)
            self.journal.set_setting(BASE_DB_STAT, self._db_stat())

    def _db_stat(self) -> Optional[List[int]]:
        try:
            stat = self.layout.state_db.stat()
        except OSError:
            return None
        return [stat.st_size, stat.st_mtime_ns]

    def database_unchanged_by_stat(self) -> bool:
        """Cheap pre-check: the database file has not been touched since base."""
        return self._db_stat() == self.journal.get_setting(BASE_DB_STAT)

    # -- capture ------------------------------------------------------------

    def capture(
        self,
        base: Optional[Snapshot],
        *,
        name: str = "",
        last_session_id: Optional[str] = None,
        template: Optional[TemplateOrigin] = None,
        database: Optional[CapturedDatabase] = None,
        portable: Optional[PortableConfig] = None,
    ):
        """Seal the current working copy as a candidate, or report no change.

        `base` should be `self.base()`: the sealed candidate becomes the new
        base, so consecutive captures form a chain rather than siblings.

        Unchanged and never-fetched files keep the descriptors the base already
        had. A file that was materialized here and is now gone is a deletion.
        """
        generation_before = self.journal.generation
        staging = self.layout.staging / new_id()
        staging.mkdir(parents=True, exist_ok=True)

        records: Dict[str, FileRecord] = {}
        staged_objects: Dict[str, Path] = {}
        inventory = self._local_inventory()
        known = {r.logical_path: r for r in self.journal.list_materialized()}
        tombstones = {p for p, r in known.items() if r.explicit_delete}

        # 1. Carry forward what the base described and we did not touch.
        if base is not None:
            local_paths = {item.logical_path for item in inventory}
            for logical, record in base.files.items():
                if logical in tombstones or logical in local_paths:
                    continue
                if logical in (DATABASE_PATH, PORTABLE_CONFIG_PATH):
                    continue
                previous = known.get(logical)
                if previous is not None and previous.present:
                    continue  # was here, now gone: an ordinary deletion
                records[logical] = record

        # 2. Stage the local files.
        for item in inventory:
            if item.logical_path in tombstones:
                continue
            existing = base.files.get(item.logical_path) if base else None
            staged = self._stage_stable(item, staging, existing)
            if staged is None:
                continue
            records[item.logical_path] = staged.record
            staged_objects.update(staged.objects)

        # 3. Generated documents supplied by the caller.
        if portable is not None:
            record, objects = self._stage_bytes(
                PORTABLE_CONFIG_PATH, portable.to_bytes(), staging)
            records[PORTABLE_CONFIG_PATH] = record
            staged_objects.update(objects)
        elif base is not None and PORTABLE_CONFIG_PATH in base.files:
            records[PORTABLE_CONFIG_PATH] = base.files[PORTABLE_CONFIG_PATH]

        db_digest = self.journal.get_setting(BASE_DB_DIGEST)
        if database is not None:
            if (base is not None and DATABASE_PATH in base.files
                    and database.digest == db_digest):
                # SQLite rewrote pages but no conversation row changed.
                records[DATABASE_PATH] = base.files[DATABASE_PATH]
            else:
                records[DATABASE_PATH] = database.record
                staged_objects.update(database.objects)
            db_digest = database.digest
        elif base is not None and DATABASE_PATH in base.files:
            records[DATABASE_PATH] = base.files[DATABASE_PATH]

        # 4. A capture is only worth publishing if content actually moved.
        if base is not None and _same_inventory(base, records) \
                and base.template == template \
                and (last_session_id or None) == (base.last_session_id or None):
            shutil.rmtree(staging, ignore_errors=True)
            self.journal.set_setting(BASE_DB_STAT, self._db_stat())
            return NoChange()

        # 5. A turn that landed mid-capture invalidates the observation.
        if self.journal.generation != generation_before:
            shutil.rmtree(staging, ignore_errors=True)
            return NoChange()

        snapshot = Snapshot(
            agent_id=self.layout.agent_id,
            snapshot_id=new_id(),
            created_at=utcnow(),
            device_id=self.device_id,
            runtime=self.runtime,
            files=records,
            name=name or (base.name if base else ""),
            parent=SnapshotRef(snapshot_id=base.snapshot_id,
                               sha256=hash_bytes(base.to_bytes())) if base else None,
            last_session_id=last_session_id,
            template=template if template is not None else (base.template if base else None),
        )
        return self._seal(snapshot, staged_objects, staging, base, db_digest)

    def _local_inventory(self) -> List[Inventory]:
        items = core_inventory(self.layout)
        skills, _ = scan_skills(self.layout.skills_dir)
        workspace, _ = scan_workspace(self.layout.workspace)
        items.extend(skills)
        items.extend(workspace)
        return items

    def _stage_stable(self, item: Inventory, staging: Path,
                      existing: Optional[FileRecord]) -> Optional[StagedFile]:
        """Stage a file, retrying if it changed while being read.

        Comparing size and mtime around the read catches a file Hermes was
        writing; hashing the staged bytes is what the manifest describes.
        """
        for _attempt in range(3):
            try:
                before = item.source.stat()
            except OSError:
                return None
            if not item.source.is_file() or item.source.is_symlink():
                return None
            staged = stage_file(item.source, item.logical_path, staging)
            try:
                after = item.source.stat()
            except OSError:
                return None
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                # Reuse the previous descriptor when the content is identical,
                # so an unchanged file uploads nothing. The staged objects are
                # left alone: another file in this capture may share them, and
                # the staging directory is removed whole at the end.
                if existing is not None and existing.sha256 == staged.record.sha256:
                    return StagedFile(logical_path=item.logical_path,
                                      record=existing, objects={})
                return staged
        return None

    def _stage_bytes(self, logical: str, body: bytes, staging: Path
                     ) -> Tuple[FileRecord, Dict[str, Path]]:
        temp = staging / "generated" / logical.replace("/", "_")
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(body)
        staged = stage_file(temp, logical, staging)
        return staged.record, staged.objects

    def _seal(self, snapshot: Snapshot, staged_objects: Dict[str, Path],
              staging: Path, base: Optional[Snapshot],
              db_digest: Optional[str]) -> Candidate:
        """Commit the candidate's bytes to `pending/`, then journal it whole.

        Every object the snapshot references gets an upload row: new objects
        unacknowledged with their pending copy, carried-forward ones already
        acknowledged, because the base that referenced them was itself
        published (or is queued ahead of this one). The checkpoint, its upload
        rows, the inventory and the base advance in one transaction, so a crash
        can never leave a publishable checkpoint that names objects the journal
        does not know about.
        """
        checkpoint_id = new_id()
        pending = self.layout.pending_checkpoint(checkpoint_id)
        (pending / "objects").mkdir(parents=True, exist_ok=True)
        base_objects = set(base.objects()) if base is not None else set()

        uploads: List[Upload] = []
        for reference, size in sorted(snapshot.objects().items()):
            digest = reference.split("/", 1)[1].split(".", 1)[0]
            source = staged_objects.get(reference)
            if source is None and reference in base_objects:
                uploads.append(Upload(
                    checkpoint_id=checkpoint_id, object_path=reference,
                    sha256=digest, size=size,
                    local_path=str(self.cache.path_for(reference)),
                    acknowledged=True))
                continue
            if source is None:
                source = self.cache.path_for(reference)
                if not source.is_file():
                    raise SchemaError(
                        f"{reference} is referenced by the capture but no local "
                        "copy exists; the checkpoint was not sealed")
            target = pending / "objects" / Path(reference).name
            if not target.exists():
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
            uploads.append(Upload(
                checkpoint_id=checkpoint_id, object_path=reference,
                sha256=digest, size=size, local_path=str(target),
                acknowledged=False))

        body = snapshot.to_bytes()
        snapshot_path = pending / "snapshot.json"
        write_private(snapshot_path, body)
        # fsync the directory so the references the journal records cannot
        # outlive the files they name.
        _fsync_dir(pending)
        write_private(self.layout.cached_snapshot(snapshot.snapshot_id), body)

        with self.journal.transaction():
            checkpoint = self.journal.create_checkpoint(
                snapshot_path=str(snapshot_path), snapshot_hash=hash_bytes(body),
                parent_snapshot_id=snapshot.parent.snapshot_id if snapshot.parent else None,
                parent_hash=snapshot.parent.sha256 if snapshot.parent else None,
                checkpoint_id=checkpoint_id)
            self.journal.record_uploads(checkpoint.id, uploads)
            self.journal.clear_dirty()
            # The inventory now describes this snapshot: what is present, and
            # what is described but only held remotely.
            for record in self.journal.list_materialized():
                if record.logical_path not in snapshot.files:
                    self.journal.forget_materialized(record.logical_path)
            for logical, record in snapshot.files.items():
                try:
                    present = self.local_path(logical).is_file()
                except SchemaError:
                    present = False
                self.journal.set_materialized(MaterializedFile(
                    logical_path=logical, base_hash=record.sha256,
                    present=present, dirty=False, explicit_delete=False))
            self.journal.set_setting(BASE_SNAPSHOT, snapshot.snapshot_id)
            self.journal.set_setting(BASE_DB_DIGEST, db_digest)
            self.journal.set_setting(BASE_DB_STAT, self._db_stat())

        # New objects belong in the cache too, so a later restore or capture
        # reuses them. Adopt from staging before it is removed.
        for reference, source in staged_objects.items():
            if source.exists():
                self.cache.adopt(reference, source)
        shutil.rmtree(staging, ignore_errors=True)

        return Candidate(checkpoint_id=checkpoint.id, snapshot=snapshot,
                         snapshot_path=snapshot_path,
                         snapshot_hash=checkpoint.snapshot_hash, uploads=uploads)

    # -- materialization ----------------------------------------------------

    def materialize(
        self,
        snapshot: Snapshot,
        resolve: Callable[[str], Path],
        *,
        extra_paths: Iterable[str] = (),
    ) -> List[str]:
        """Install the eager core files, plus any extra paths requested.

        Workspace documents stay remote-only until fetched. A file that was
        previously materialized and whose remote descriptor changed is
        refreshed, so Hermes never reads stale bytes. Every file is assembled
        and verified in staging first; only then are they swapped in, together
        with the inventory that describes them.
        """
        wanted = self._paths_to_install(snapshot, extra_paths)
        generation = self.layout.staging / f"generation-{new_id()[:8]}"
        plan: List[Tuple[str, FileRecord, Path, Path]] = []
        try:
            for logical in wanted:
                record = snapshot.files[logical]
                destination = self.local_path(logical)
                existing = self.journal.get_materialized(logical)
                if existing is not None and existing.dirty:
                    # Never overwrite work the user has not checkpointed.
                    continue
                if (existing is not None and existing.present
                        and existing.base_hash == record.sha256
                        and destination.is_file()):
                    continue
                staged = generation / logical
                staged.parent.mkdir(parents=True, exist_ok=True)
                if record.size == 0:
                    staged.write_bytes(b"")
                    os.chmod(staged, 0o700 if record.executable else 0o600)
                else:
                    assemble(record, resolve, staged)
                plan.append((logical, record, destination, staged))

            installed: List[str] = []
            with self.journal.transaction():
                for logical, record, destination, staged in plan:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, destination)
                    self.journal.set_materialized(MaterializedFile(
                        logical_path=logical, base_hash=record.sha256,
                        present=True, dirty=False, explicit_delete=False))
                    installed.append(logical)
                # Remote-only files are recorded so their absence is never
                # read as a deletion.
                for logical, record in snapshot.files.items():
                    if self.journal.get_materialized(logical) is None:
                        self.journal.set_materialized(MaterializedFile(
                            logical_path=logical, base_hash=record.sha256,
                            present=False, dirty=False, explicit_delete=False))
                self._remove_vanished(snapshot)
            return installed
        finally:
            shutil.rmtree(generation, ignore_errors=True)

    def _paths_to_install(self, snapshot: Snapshot,
                          extra_paths: Iterable[str]) -> List[str]:
        wanted: List[str] = []
        for logical in snapshot.files:
            if logical in EAGER_PATHS or logical.startswith(SKILLS_PREFIX):
                wanted.append(logical)
        # Anything materialized earlier is refreshed too; a stale copy would be
        # read by Hermes and republished over the newer remote bytes.
        for record in self.journal.list_materialized():
            if record.present and record.logical_path in snapshot.files \
                    and record.logical_path not in wanted:
                wanted.append(record.logical_path)
        for logical in extra_paths:
            if logical in snapshot.files and logical not in wanted:
                wanted.append(logical)
        # The database and generated config are installed by their own adapters.
        return [p for p in wanted if p not in (DATABASE_PATH, PORTABLE_CONFIG_PATH)]

    def _remove_vanished(self, snapshot: Snapshot) -> None:
        """Drop tracked local files the snapshot no longer contains.

        Only proven-clean files are removed, and the previous bytes are kept
        under `recovery/` first.
        """
        for record in self.journal.list_materialized():
            if record.logical_path in snapshot.files:
                continue
            if record.dirty or record.explicit_delete:
                continue
            try:
                local = self.local_path(record.logical_path)
            except SchemaError:
                self.journal.forget_materialized(record.logical_path)
                continue
            if record.present and local.is_file():
                backup = (self.layout.recovery / f"{utcnow().replace(':', '')}-removed"
                          / record.logical_path)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, backup)
                local.unlink(missing_ok=True)
            self.journal.forget_materialized(record.logical_path)

    def local_path(self, logical: str) -> Path:
        """Where a logical path lives in the working copy."""
        if logical.startswith(PROFILE_PREFIX):
            return self.layout.hermes_home / logical[len(PROFILE_PREFIX):]
        if logical.startswith(WORKSPACE_PREFIX):
            return self.layout.workspace / logical[len(WORKSPACE_PREFIX):]
        if logical == DATABASE_PATH:
            return self.layout.state_db
        if logical == PORTABLE_CONFIG_PATH:
            return self.layout.hermes_config_file
        raise SchemaError(f"no local destination for {logical!r}")

    # -- dirty detection ----------------------------------------------------

    def scan_dirty(self) -> List[str]:
        """Paths whose local bytes differ from what the journal recorded."""
        dirty: List[str] = []
        for item in self._local_inventory():
            record = self.journal.get_materialized(item.logical_path)
            if record is None or record.dirty:
                dirty.append(item.logical_path)
                continue
            try:
                digest, _size = hash_file(item.source)
            except OSError:
                continue
            if digest != record.base_hash:
                dirty.append(item.logical_path)
        # A tracked file that was deleted locally is a change too.
        for record in self.journal.list_materialized():
            if not record.present or record.explicit_delete:
                continue
            if record.logical_path in (DATABASE_PATH, PORTABLE_CONFIG_PATH):
                continue
            try:
                local = self.local_path(record.logical_path)
            except SchemaError:
                continue
            if not local.exists() and record.logical_path not in dirty:
                dirty.append(record.logical_path)
        return sorted(set(dirty))


def _same_inventory(base: Snapshot, records: Dict[str, FileRecord]) -> bool:
    """True when nothing about the file set changed."""
    if set(base.files) != set(records):
        return False
    for logical, record in records.items():
        other = base.files[logical]
        if (record.sha256, record.size, record.executable) != \
                (other.sha256, other.size, other.executable):
            return False
    return True


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
