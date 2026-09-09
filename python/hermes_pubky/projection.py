"""What belongs in a checkpoint, and how a snapshot becomes a working copy.

Capture scans an allowlist of sources, never a recursive walk of the management
root. Materialization installs a validated generation beside the live one and
swaps it, so a failed restore leaves the previous working copy intact.

Reference: implementation plan sections 6.2, 8.4 and 11.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .journal import Journal, MaterializedFile, Upload, new_id, utcnow
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
from .objects import ObjectCache, StagedFile, assemble, hash_bytes, stage_file
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
        except FileNotFoundError:
            return Ignore()
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
        return len(self.uploads)


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

    # -- capture ------------------------------------------------------------

    def capture(
        self,
        base: Optional[Snapshot],
        *,
        name: str = "",
        last_session_id: Optional[str] = None,
        template: Optional[TemplateOrigin] = None,
        database: Optional[Tuple[FileRecord, Dict[str, Path]]] = None,
        portable: Optional[PortableConfig] = None,
    ):
        """Seal the current working copy as a candidate, or report no change.

        Unchanged and remote-only files keep the descriptors the base snapshot
        already had, so the absence of a never-fetched file is never read as a
        deletion.
        """
        generation_before = self.journal.generation
        staging = self.layout.staging / new_id()
        staging.mkdir(parents=True, exist_ok=True)

        records: Dict[str, FileRecord] = {}
        staged_objects: Dict[str, Path] = {}
        inventory = self._local_inventory()
        tombstones = {
            record.logical_path for record in self.journal.list_materialized()
            if record.explicit_delete
        }

        # 1. Carry forward everything the base described that we did not touch.
        if base is not None:
            local_paths = {item.logical_path for item in inventory}
            for logical, record in base.files.items():
                if logical in tombstones:
                    continue
                if logical in local_paths:
                    continue
                if logical in (DATABASE_PATH, PORTABLE_CONFIG_PATH):
                    continue
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
            body = portable.to_bytes()
            record, objects = self._stage_bytes(
                PORTABLE_CONFIG_PATH, body, staging)
            records[PORTABLE_CONFIG_PATH] = record
            staged_objects.update(objects)
        elif base is not None and PORTABLE_CONFIG_PATH in base.files:
            records[PORTABLE_CONFIG_PATH] = base.files[PORTABLE_CONFIG_PATH]

        if database is not None:
            record, objects = database
            records[DATABASE_PATH] = record
            staged_objects.update(objects)
        elif base is not None and DATABASE_PATH in base.files:
            records[DATABASE_PATH] = base.files[DATABASE_PATH]

        # 4. A capture is only worth publishing if content actually moved.
        if base is not None and _same_inventory(base, records) \
                and base.template == template \
                and (last_session_id or None) == (base.last_session_id or None):
            shutil.rmtree(staging, ignore_errors=True)
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
        return self._seal(snapshot, staged_objects, staging)

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
            try:
                staged = stage_file(item.source, item.logical_path, staging)
            except (OSError, SchemaError):
                raise
            try:
                after = item.source.stat()
            except OSError:
                return None
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                # Reuse the previous descriptor when the content is identical,
                # so an unchanged file uploads nothing.
                if existing is not None and existing.sha256 == staged.record.sha256:
                    for path in staged.objects.values():
                        path.unlink(missing_ok=True)
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
              staging: Path) -> Candidate:
        """Commit the candidate's bytes to `pending/` before journaling it."""
        checkpoint_id = new_id()
        pending = self.layout.pending_checkpoint(checkpoint_id)
        (pending / "objects").mkdir(parents=True, exist_ok=True)

        uploads: List[Upload] = []
        needed = snapshot.objects()
        for reference, size in sorted(needed.items()):
            digest = reference.split("/", 1)[1].split(".", 1)[0]
            source = staged_objects.get(reference)
            if source is None:
                # Carried forward from the base; the cache holds it already.
                cached = self.cache.path_for(reference)
                if not cached.is_file():
                    continue
                source = cached
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

        checkpoint = self.journal.create_checkpoint(
            snapshot_path=str(snapshot_path), snapshot_hash=hash_bytes(body),
            parent_snapshot_id=snapshot.parent.snapshot_id if snapshot.parent else None,
            parent_hash=snapshot.parent.sha256 if snapshot.parent else None,
            checkpoint_id=checkpoint_id)
        self.journal.record_uploads(checkpoint.id, uploads)
        self.journal.clear_dirty()
        # Record the state this capture observed, so the next scan can tell an
        # edit from an untouched file and a deletion from a never-fetched one.
        for logical, record in snapshot.files.items():
            existing = self.journal.get_materialized(logical)
            try:
                present = self.local_path(logical).is_file()
            except SchemaError:
                present = False
            self.journal.set_materialized(MaterializedFile(
                logical_path=logical, base_hash=record.sha256,
                present=present if existing is None else (existing.present or present),
                dirty=False, explicit_delete=False))
        shutil.rmtree(staging, ignore_errors=True)

        # Objects belong in the cache too, so a later checkpoint reuses them.
        for reference in needed:
            source = staged_objects.get(reference)
            if source is not None and source.exists():
                self.cache.adopt(reference, source)

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
        refreshed, so Hermes never reads stale bytes.
        """
        wanted = self._paths_to_install(snapshot, extra_paths)
        installed: List[str] = []
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
            if record.size == 0:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"")
                os.chmod(destination, 0o700 if record.executable else 0o600)
            else:
                assemble(record, resolve, destination)
            self.journal.set_materialized(MaterializedFile(
                logical_path=logical, base_hash=record.sha256, present=True,
                dirty=False, explicit_delete=False))
            installed.append(logical)

        # Record remote-only files so their absence is never read as deletion.
        for logical, record in snapshot.files.items():
            if self.journal.get_materialized(logical) is None:
                self.journal.set_materialized(MaterializedFile(
                    logical_path=logical, base_hash=record.sha256, present=False,
                    dirty=False, explicit_delete=False))

        self._remove_vanished(snapshot)
        return installed

    def _paths_to_install(self, snapshot: Snapshot,
                          extra_paths: Iterable[str]) -> List[str]:
        wanted: List[str] = []
        for logical in snapshot.files:
            if logical in EAGER_PATHS or logical.startswith(SKILLS_PREFIX):
                wanted.append(logical)
        for logical in extra_paths:
            if logical in snapshot.files and logical not in wanted:
                wanted.append(logical)
        # The database and generated config are installed by their own adapters.
        return [p for p in wanted if p not in (DATABASE_PATH, PORTABLE_CONFIG_PATH)]

    def _remove_vanished(self, snapshot: Snapshot) -> None:
        """Drop tracked local files the snapshot no longer contains.

        Only proven-clean files are removed, and the previous generation is
        preserved under `recovery/` first.
        """
        for record in self.journal.list_materialized():
            if record.logical_path in snapshot.files:
                continue
            if record.dirty or record.explicit_delete:
                continue
            local = self.local_path(record.logical_path)
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
            if record is None:
                dirty.append(item.logical_path)
                continue
            if record.dirty:
                dirty.append(item.logical_path)
                continue
            try:
                from .objects import hash_file

                digest, _size = hash_file(item.source)
            except OSError:
                continue
            if digest != record.base_hash:
                dirty.append(item.logical_path)
        # A tracked file that was deleted locally is a change too.
        for record in self.journal.list_materialized():
            if not record.present or record.explicit_delete:
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
