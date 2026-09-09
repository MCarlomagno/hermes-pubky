"""Content-addressed objects: chunking, hashing, caching and assembly.

Everything here streams. A conversation database can be hundreds of megabytes,
so no function in this module ever holds a whole file in memory, and no
partially written file is ever visible under its final name.

Reference: implementation plan section 5.4.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Tuple

from .models import (
    EMPTY_SHA256,
    OBJECT_REF_RE,
    OBJECT_CHUNK_BYTES,
    FileRecord,
    Piece,
    SchemaError,
    max_bytes_for,
    object_extension,
    object_ref,
)

# Read buffer for hashing and copying. Independent of the object size so a
# 1 MiB chunk is not held whole while being written.
READ_BUFFER = 256 * 1024

# Target size of the reusable object cache (plan 5.4). Pending and recovery
# data live elsewhere and are never counted against or evicted for this.
CACHE_BUDGET_BYTES = 256 * 1024 * 1024


class IntegrityError(RuntimeError):
    """Stored bytes did not match the hash that was expected of them."""


def hash_file(path: Path) -> Tuple[str, int]:
    """Return `(sha256, size)` for a file, reading it once in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(READ_BUFFER)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class StagedFile:
    """A file measured and split into immutable object files on disk."""

    logical_path: str
    record: FileRecord
    # Object reference -> the staged local file holding exactly those bytes.
    objects: Dict[str, Path] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.record.size == 0


def stage_file(
    source: Path,
    logical_path: str,
    out_dir: Path,
    *,
    force_chunked: bool = False,
) -> StagedFile:
    """Measure `source` and write its objects into `out_dir`.

    A file at or below one object's worth is stored as a single object that
    keeps a readable extension, so Markdown on the homeserver is still
    Markdown. Anything larger, and the conversation database always, is split
    into fixed-size chunks.

    Raises `SchemaError` if the file exceeds the cap for its logical path, so an
    oversized file fails before any bytes are uploaded.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    whole, size = hash_file(source)

    cap = max_bytes_for(logical_path)
    if size > cap:
        raise SchemaError(
            f"{logical_path} is {size} bytes, over its {cap} byte cap; "
            "nothing was staged"
        )

    executable = bool(os.stat(source).st_mode & 0o100)

    if size == 0:
        return StagedFile(
            logical_path=logical_path,
            record=FileRecord(sha256=EMPTY_SHA256, size=0, executable=executable),
        )

    chunked = force_chunked or size > OBJECT_CHUNK_BYTES
    extension = object_extension(logical_path, chunked=chunked)

    pieces: List[Piece] = []
    objects: Dict[str, Path] = {}
    with open(source, "rb") as handle:
        for piece_digest, piece_size, staged in _split(handle, out_dir, extension):
            ref = object_ref(piece_digest, extension)
            pieces.append(Piece(object=ref, sha256=piece_digest, size=piece_size))
            # The same content can repeat inside one file; keep one copy.
            if ref in objects:
                staged.unlink(missing_ok=True)
            else:
                objects[ref] = staged

    record = FileRecord(sha256=whole, size=size, executable=executable, pieces=pieces)
    # Re-validate through the parser so a staging bug cannot produce a manifest
    # the reader would reject.
    FileRecord.parse(record.to_dict(), logical_path)
    return StagedFile(logical_path=logical_path, record=record, objects=objects)


def _split(handle, out_dir: Path, extension: str) -> Iterator[Tuple[str, int, Path]]:
    """Write successive chunks of `handle` to `out_dir`, yielding their hashes."""
    index = 0
    while True:
        remaining = OBJECT_CHUNK_BYTES
        digest = hashlib.sha256()
        written = 0
        temp = out_dir / f".staging-{os.getpid()}-{index}"
        with open(temp, "wb") as out:
            while remaining:
                block = handle.read(min(READ_BUFFER, remaining))
                if not block:
                    break
                out.write(block)
                digest.update(block)
                written += len(block)
                remaining -= len(block)
            out.flush()
            os.fsync(out.fileno())
        if written == 0:
            temp.unlink(missing_ok=True)
            return
        final = out_dir / f"{digest.hexdigest()}.{extension}"
        os.replace(temp, final)
        yield digest.hexdigest(), written, final
        index += 1
        if written < OBJECT_CHUNK_BYTES:
            return


def assemble(
    record: FileRecord,
    resolve: Callable[[str], Path],
    destination: Path,
) -> None:
    """Rebuild a logical file from its pieces, verifying as it goes.

    Every piece is hashed while being copied, and the whole file is hashed at
    the end. The destination only appears under its final name once both
    checks pass, so a partial download is never mistaken for real content.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".partial")

    whole = hashlib.sha256()
    total = 0
    try:
        with open(temp, "wb") as out:
            for piece in record.pieces:
                source = resolve(piece.object)
                piece_digest = hashlib.sha256()
                copied = 0
                with open(source, "rb") as src:
                    while True:
                        block = src.read(READ_BUFFER)
                        if not block:
                            break
                        out.write(block)
                        piece_digest.update(block)
                        whole.update(block)
                        copied += len(block)
                if copied != piece.size:
                    raise IntegrityError(
                        f"{piece.object} is {copied} bytes, expected {piece.size}"
                    )
                if piece_digest.hexdigest() != piece.sha256:
                    raise IntegrityError(f"{piece.object} does not match its hash")
                total += copied
            out.flush()
            os.fsync(out.fileno())

        if total != record.size:
            raise IntegrityError(
                f"assembled {total} bytes, manifest says {record.size}"
            )
        if whole.hexdigest() != record.sha256:
            raise IntegrityError("assembled file does not match its whole-file hash")

        os.chmod(temp, 0o700 if record.executable else 0o600)
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


class ObjectCache:
    """Immutable objects kept on disk, reusable across checkpoints.

    Objects are named by digest, so a cached object is either correct or
    corrupt, never stale. Eviction is opt-in and only ever touches objects the
    caller has confirmed are reconstructible.
    """

    def __init__(self, root: Path, budget_bytes: int = CACHE_BUDGET_BYTES) -> None:
        self.root = Path(root)
        self.budget_bytes = budget_bytes

    def path_for(self, ref: str) -> Path:
        """Local path for an object reference, refusing anything but a digest name."""
        if not OBJECT_REF_RE.match(ref):
            raise SchemaError(f"not an object reference: {ref!r}")
        return self.root / ref.split("/", 1)[1]

    def has(self, ref: str) -> bool:
        return self.path_for(ref).is_file()

    def size_of(self, ref: str) -> int:
        return self.path_for(ref).stat().st_size

    def verify(self, ref: str) -> bool:
        """Rehash a cached object. A name alone is not proof of its content."""
        path = self.path_for(ref)
        if not path.is_file():
            return False
        expected = ref.split("/", 1)[1].split(".", 1)[0]
        digest, _size = hash_file(path)
        return digest == expected

    def adopt(self, ref: str, staged: Path) -> Path:
        """Move a staged object into the cache under its digest name."""
        target = self.path_for(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            staged.unlink(missing_ok=True)
            return target
        try:
            os.replace(staged, target)
        except OSError:
            # Different filesystem; copy then drop the staged copy.
            shutil.copy2(staged, target)
            staged.unlink(missing_ok=True)
        os.chmod(target, 0o600)
        return target

    def total_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())

    def evict_to_budget(self, protected: Iterable[str]) -> List[str]:
        """Drop least-recently-used objects until under budget.

        `protected` names objects that must never be evicted: anything a
        pending checkpoint still needs, or that is not reconstructible from
        elsewhere. Returns the references that were removed.
        """
        if not self.root.is_dir():
            return []
        keep = {self.path_for(ref).name for ref in protected}
        entries = [
            (p.stat().st_atime, p)
            for p in self.root.iterdir()
            if p.is_file() and p.name not in keep
        ]
        total = self.total_bytes()
        removed: List[str] = []
        for _atime, path in sorted(entries):
            if total <= self.budget_bytes:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed.append(f"objects/{path.name}")
        return removed
