"""Typed manifests for the /v2 storage protocol, and their strict parsers.

Every document read from a homeserver crosses a trust boundary: the operator
serves the bytes, and a template's author is a stranger. Parsing here is total
and refuses rather than repairs. Nothing in this module imports Hermes or the
native extension, so the rules are testable on their own.

Reference: implementation plan sections 5.1-5.4, 9 and 12.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 2

# -- identifiers -------------------------------------------------------------

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_REF_RE = re.compile(r"^objects/([0-9a-f]{64})\.(md|json|bin|chunk)$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

# -- limits (plan 5.4) -------------------------------------------------------

OBJECT_CHUNK_BYTES = 1024 * 1024          # one raw object, and the chunk size
MAX_HEAD_BYTES = 4 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_CORE_MARKDOWN_BYTES = 64 * 1024
MAX_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_LOGICAL_FILES = 5_000
MAX_LOGICAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOGICAL_PATH_BYTES = 1024

# Core Markdown carries the tighter cap; everything else is a workspace file.
CORE_MARKDOWN_PATHS = frozenset({
    "profile/SOUL.md",
    "profile/memories/USER.md",
    "profile/memories/MEMORY.md",
    "workspace/AGENTS.md",
})
DATABASE_PATH = "conversations/state.sqlite3"
PORTABLE_CONFIG_PATH = "config/portable.json"

# Logical paths a public template may contain (plan 12).
TEMPLATE_PATH_PREFIXES = ("profile/skills/",)
TEMPLATE_EXACT_PATHS = frozenset({
    "profile/SOUL.md", "workspace/AGENTS.md", PORTABLE_CONFIG_PATH,
})


class SchemaError(ValueError):
    """A document did not match the /v2 schema."""


# -- primitive validators ----------------------------------------------------

def require_agent_id(value: Any, what: str = "agentId") -> str:
    if not isinstance(value, str) or not AGENT_ID_RE.match(value):
        raise SchemaError(
            f"{what} must match {AGENT_ID_RE.pattern} (got {value!r})"
        )
    return value


def require_hex32(value: Any, what: str) -> str:
    if not isinstance(value, str) or not HEX32_RE.match(value):
        raise SchemaError(f"{what} must be 32 lowercase hex characters (got {value!r})")
    return value


def require_sha256(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise SchemaError(f"{what} must be 64 lowercase hex characters (got {value!r})")
    return value


def require_int(value: Any, what: str, *, low: int, high: int) -> int:
    # bool is an int subclass; a flag where a number belongs is a schema error.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{what} must be an integer (got {type(value).__name__})")
    if not low <= value <= high:
        raise SchemaError(f"{what} must be between {low} and {high} (got {value})")
    return value


def require_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{what} must be a boolean (got {type(value).__name__})")
    return value


def require_str(value: Any, what: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{what} must be a string (got {type(value).__name__})")
    if len(value) > max_chars:
        raise SchemaError(f"{what} is {len(value)} chars, over the {max_chars} limit")
    return value


def require_timestamp(value: Any, what: str) -> str:
    """Validate shape and then real calendar values.

    The shape check alone would accept month 13, so the parse follows it.
    """
    if not isinstance(value, str) or not RFC3339_RE.match(value):
        raise SchemaError(f"{what} must be an RFC3339 timestamp (got {value!r})")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"{what} is not a real timestamp ({exc})") from exc
    return value


def require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{what} must be a JSON object (got {type(value).__name__})")
    return value


# -- logical paths (plan 5.1) ------------------------------------------------

_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})


def require_logical_path(value: Any, what: str = "logical path") -> str:
    """Validate a relative POSIX path used as a manifest key.

    Rejects anything that could escape a root or collide once written to a
    case-insensitive or normalizing filesystem.
    """
    if not isinstance(value, str):
        raise SchemaError(f"{what} must be a string (got {type(value).__name__})")
    if not value:
        raise SchemaError(f"{what} cannot be empty")
    if len(value.encode("utf-8")) > MAX_LOGICAL_PATH_BYTES:
        raise SchemaError(f"{what} exceeds {MAX_LOGICAL_PATH_BYTES} UTF-8 bytes")
    if value.startswith("/"):
        raise SchemaError(f"{what} must be relative (got {value!r})")
    if "\\" in value:
        raise SchemaError(f"{what} must not contain a backslash (got {value!r})")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SchemaError(f"{what} must not contain control characters")
    if value.endswith("/"):
        raise SchemaError(f"{what} must name a file, not a directory (got {value!r})")
    for segment in value.split("/"):
        if segment in _FORBIDDEN_SEGMENTS:
            raise SchemaError(f"{what} has an empty or relative segment (got {value!r})")
        if segment != segment.strip():
            raise SchemaError(f"{what} has a padded segment (got {value!r})")
    if unicodedata.normalize("NFC", value) != value:
        # Remote names must already be canonical; new names are normalized by
        # `normalize_new_path` before they get here.
        raise SchemaError(f"{what} is not NFC-normalized (got {value!r})")
    return value


def normalize_new_path(value: str) -> str:
    """Normalize a locally created name to the canonical form, then validate."""
    return require_logical_path(unicodedata.normalize("NFC", value))


def collision_key(path: str) -> str:
    """The key two paths share when a filesystem would treat them as one."""
    return unicodedata.normalize("NFC", path).casefold()


def reject_colliding_paths(paths: Iterable[str]) -> None:
    seen: Dict[str, str] = {}
    for path in paths:
        key = collision_key(path)
        if key in seen and seen[key] != path:
            raise SchemaError(
                f"logical paths {seen[key]!r} and {path!r} collide after "
                "normalization; never silently rename"
            )
        seen[key] = path


def max_bytes_for(path: str) -> int:
    """The size cap that applies to one logical path."""
    if path in CORE_MARKDOWN_PATHS:
        return MAX_CORE_MARKDOWN_BYTES
    if path == DATABASE_PATH:
        return MAX_DATABASE_BYTES
    if path == PORTABLE_CONFIG_PATH:
        return MAX_CORE_MARKDOWN_BYTES
    return MAX_WORKSPACE_FILE_BYTES


# -- canonical JSON (plan 5.1) ----------------------------------------------

def dumps(payload: Mapping[str, Any]) -> bytes:
    """Serialize canonically: sorted keys, compact, one trailing newline."""
    text = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SchemaError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def loads(data: bytes, what: str, *, max_bytes: int) -> Any:
    """Parse a manifest, enforcing its cap before doing any work."""
    if not isinstance(data, (bytes, bytearray)):
        raise SchemaError(f"{what} must be bytes")
    if len(data) == 0:
        raise SchemaError(f"{what} is empty")
    if len(data) > max_bytes:
        raise SchemaError(f"{what} is {len(data)} bytes, over the {max_bytes} cap")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{what} is not valid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{what} is not valid JSON: {exc}") from exc
    _reject_nonfinite(parsed, what)
    return parsed


def _reject_nonfinite(node: Any, what: str) -> None:
    if isinstance(node, float) and not math.isfinite(node):
        raise SchemaError(f"{what} contains a non-finite number")
    if isinstance(node, dict):
        for value in node.values():
            _reject_nonfinite(value, what)
    elif isinstance(node, list):
        for value in node:
            _reject_nonfinite(value, what)


def _require_schema_version(obj: Mapping[str, Any], what: str) -> None:
    version = obj.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaError(f"{what} has a missing or non-integer schemaVersion")
    if version != SCHEMA_VERSION:
        raise SchemaError(
            f"{what} declares schemaVersion {version}; this release only "
            f"supports {SCHEMA_VERSION}. There is no v1 reader."
        )


def _require_kind(obj: Mapping[str, Any], expected: str, what: str) -> None:
    if obj.get("kind") != expected:
        raise SchemaError(f"{what} must have kind {expected!r} (got {obj.get('kind')!r})")


# -- pieces and file records -------------------------------------------------

import hashlib  # noqa: E402  (kept local to the hashing constant below)

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_EXTENSION_FOR_PATH = {".md": "md", ".json": "json"}


def object_extension(path: str, *, chunked: bool) -> str:
    """The object filename extension a logical path's bytes are stored under.

    Markdown and JSON keep their identity on the homeserver so the stored files
    are readable there; everything else is `bin`, and any chunk is `chunk`.
    """
    if chunked:
        return "chunk"
    for suffix, ext in _EXTENSION_FOR_PATH.items():
        if path.endswith(suffix):
            return ext
    return "bin"


def object_ref(digest: str, extension: str) -> str:
    require_sha256(digest, "object digest")
    if extension not in ("md", "json", "bin", "chunk"):
        raise SchemaError(f"unsupported object extension {extension!r}")
    return f"objects/{digest}.{extension}"


@dataclass(frozen=True)
class Piece:
    """One stored object making up part (or all) of a logical file."""

    object: str
    sha256: str
    size: int

    @staticmethod
    def parse(raw: Any, what: str) -> "Piece":
        obj = require_mapping(raw, what)
        unknown = set(obj) - {"object", "sha256", "size"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        ref = require_str(obj.get("object"), f"{what}.object", max_chars=128)
        match = OBJECT_REF_RE.match(ref)
        if not match:
            raise SchemaError(
                f"{what}.object must be objects/<sha256>.<md|json|bin|chunk> (got {ref!r})"
            )
        digest = require_sha256(obj.get("sha256"), f"{what}.sha256")
        if match.group(1) != digest:
            raise SchemaError(
                f"{what}.object names digest {match.group(1)} but sha256 is {digest}"
            )
        size = require_int(obj.get("size"), f"{what}.size", low=0, high=OBJECT_CHUNK_BYTES)
        return Piece(object=ref, sha256=digest, size=size)

    def to_dict(self) -> Dict[str, Any]:
        return {"object": self.object, "sha256": self.sha256, "size": self.size}


@dataclass
class FileRecord:
    """A logical file: its whole-file hash plus the ordered objects holding it."""

    sha256: str
    size: int
    executable: bool = False
    pieces: List[Piece] = field(default_factory=list)

    @staticmethod
    def parse(raw: Any, path: str) -> "FileRecord":
        what = f"files[{path!r}]"
        obj = require_mapping(raw, what)
        unknown = set(obj) - {"sha256", "size", "executable", "pieces"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")

        cap = max_bytes_for(path)
        digest = require_sha256(obj.get("sha256"), f"{what}.sha256")
        size = require_int(obj.get("size"), f"{what}.size", low=0, high=cap)
        executable = require_bool(obj.get("executable", False), f"{what}.executable")

        raw_pieces = obj.get("pieces")
        if not isinstance(raw_pieces, list):
            raise SchemaError(f"{what}.pieces must be a list")
        # Bound the array before allocating anything per element.
        max_pieces = cap // OBJECT_CHUNK_BYTES + 2
        if len(raw_pieces) > max_pieces:
            raise SchemaError(
                f"{what}.pieces has {len(raw_pieces)} entries, over {max_pieces} for its cap"
            )
        pieces = [Piece.parse(p, f"{what}.pieces[{i}]") for i, p in enumerate(raw_pieces)]

        if size == 0:
            if pieces:
                raise SchemaError(f"{what} is empty but lists pieces")
            if digest != EMPTY_SHA256:
                raise SchemaError(f"{what} is empty but its sha256 is not the empty hash")
        else:
            if not pieces:
                raise SchemaError(f"{what} has size {size} but no pieces")
            total = sum(p.size for p in pieces)
            if total != size:
                raise SchemaError(f"{what} pieces sum to {total}, expected {size}")
            if len(pieces) == 1 and pieces[0].sha256 != digest:
                raise SchemaError(f"{what} single piece hash must equal the file hash")
            if any(p.size == 0 for p in pieces):
                raise SchemaError(f"{what} has a zero-length piece")
        return FileRecord(sha256=digest, size=size, executable=executable, pieces=pieces)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "executable": self.executable,
            "pieces": [p.to_dict() for p in self.pieces],
        }


def _parse_files(raw: Any, *, allowed: Optional[str] = None) -> Dict[str, FileRecord]:
    """Parse and cross-check a snapshot's file inventory."""
    obj = require_mapping(raw, "files")
    if len(obj) > MAX_LOGICAL_FILES:
        raise SchemaError(f"files has {len(obj)} entries, over {MAX_LOGICAL_FILES}")

    files: Dict[str, FileRecord] = {}
    for path, record in obj.items():
        require_logical_path(path)
        if allowed == "template" and not _is_template_path(path):
            raise SchemaError(f"a template may not contain {path!r}")
        files[path] = FileRecord.parse(record, path)

    reject_colliding_paths(files)

    total = sum(r.size for r in files.values())
    if total > MAX_LOGICAL_BYTES:
        raise SchemaError(f"files total {total} bytes, over {MAX_LOGICAL_BYTES}")

    # The same object may back several files; its hash and size must agree
    # everywhere, or one reference is lying about what it points at.
    seen: Dict[str, int] = {}
    for path, record in files.items():
        for piece in record.pieces:
            if piece.object in seen and seen[piece.object] != piece.size:
                raise SchemaError(
                    f"object {piece.object} is referenced with conflicting sizes"
                )
            seen[piece.object] = piece.size
    return files


def _is_template_path(path: str) -> bool:
    return path in TEMPLATE_EXACT_PATHS or path.startswith(TEMPLATE_PATH_PREFIXES)


# -- head --------------------------------------------------------------------

@dataclass(frozen=True)
class Head:
    """The single mutable document: which snapshot is current."""

    kind: str
    id: str
    snapshot_id: str
    sha256: str

    AGENT = "agent-head"
    TEMPLATE = "template-head"

    @property
    def is_template(self) -> bool:
        return self.kind == Head.TEMPLATE

    @staticmethod
    def parse(data: bytes, *, template: bool = False) -> "Head":
        what = "head"
        obj = require_mapping(loads(data, what, max_bytes=MAX_HEAD_BYTES), what)
        _require_schema_version(obj, what)
        kind = Head.TEMPLATE if template else Head.AGENT
        _require_kind(obj, kind, what)
        id_field = "templateId" if template else "agentId"
        unknown = set(obj) - {"schemaVersion", "kind", id_field, "snapshotId", "sha256"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        return Head(
            kind=kind,
            id=require_agent_id(obj.get(id_field), id_field),
            snapshot_id=require_hex32(obj.get("snapshotId"), f"{what}.snapshotId"),
            sha256=require_sha256(obj.get("sha256"), f"{what}.sha256"),
        )

    def to_dict(self) -> Dict[str, Any]:
        id_field = "templateId" if self.is_template else "agentId"
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": self.kind,
            id_field: self.id,
            "snapshotId": self.snapshot_id,
            "sha256": self.sha256,
        }

    def to_bytes(self) -> bytes:
        return dumps(self.to_dict())


@dataclass(frozen=True)
class SnapshotRef:
    """A verified pointer to a snapshot document."""

    snapshot_id: str
    sha256: str

    @staticmethod
    def parse(raw: Any, what: str) -> Optional["SnapshotRef"]:
        if raw is None:
            return None
        obj = require_mapping(raw, what)
        unknown = set(obj) - {"snapshotId", "sha256"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        return SnapshotRef(
            snapshot_id=require_hex32(obj.get("snapshotId"), f"{what}.snapshotId"),
            sha256=require_sha256(obj.get("sha256"), f"{what}.sha256"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"snapshotId": self.snapshot_id, "sha256": self.sha256}


@dataclass(frozen=True)
class RuntimeInfo:
    """Which harness and adapter produced a snapshot."""

    name: str
    version: str
    adapter: str

    @staticmethod
    def parse(raw: Any, what: str = "runtime") -> "RuntimeInfo":
        obj = require_mapping(raw, what)
        unknown = set(obj) - {"name", "version", "adapter"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        return RuntimeInfo(
            name=require_str(obj.get("name"), f"{what}.name", max_chars=64),
            version=require_str(obj.get("version"), f"{what}.version", max_chars=64),
            adapter=require_str(obj.get("adapter"), f"{what}.adapter", max_chars=128),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version, "adapter": self.adapter}


@dataclass(frozen=True)
class TemplateOrigin:
    """Where an adopted template came from, and what was adopted from it."""

    url: str
    snapshot_id: str
    sha256: str
    adopted_at: str
    managed_paths: Dict[str, str]

    @staticmethod
    def parse(raw: Any, what: str = "template") -> Optional["TemplateOrigin"]:
        if raw is None:
            return None
        obj = require_mapping(raw, what)
        unknown = set(obj) - {"url", "snapshotId", "sha256", "adoptedAt", "managedPaths"}
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        raw_managed = require_mapping(obj.get("managedPaths", {}), f"{what}.managedPaths")
        if len(raw_managed) > MAX_LOGICAL_FILES:
            raise SchemaError(f"{what}.managedPaths has too many entries")
        managed = {}
        for path, digest in raw_managed.items():
            require_logical_path(path, f"{what}.managedPaths key")
            managed[path] = require_sha256(digest, f"{what}.managedPaths[{path!r}]")
        return TemplateOrigin(
            url=require_str(obj.get("url"), f"{what}.url", max_chars=2048),
            snapshot_id=require_hex32(obj.get("snapshotId"), f"{what}.snapshotId"),
            sha256=require_sha256(obj.get("sha256"), f"{what}.sha256"),
            adopted_at=require_timestamp(obj.get("adoptedAt"), f"{what}.adoptedAt"),
            managed_paths=managed,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "snapshotId": self.snapshot_id,
            "sha256": self.sha256,
            "adoptedAt": self.adopted_at,
            "managedPaths": dict(self.managed_paths),
        }


# -- snapshots ---------------------------------------------------------------

@dataclass
class Snapshot:
    """An immutable, complete description of one saved agent state."""

    agent_id: str
    snapshot_id: str
    created_at: str
    device_id: str
    runtime: RuntimeInfo
    files: Dict[str, FileRecord] = field(default_factory=dict)
    name: str = ""
    parent: Optional[SnapshotRef] = None
    last_session_id: Optional[str] = None
    template: Optional[TemplateOrigin] = None

    KIND = "agent-snapshot"
    _KEYS = frozenset({
        "schemaVersion", "kind", "agentId", "name", "snapshotId", "parent",
        "createdAt", "deviceId", "runtime", "lastSessionId", "template", "files",
    })

    @staticmethod
    def parse(data: bytes) -> "Snapshot":
        what = "snapshot"
        obj = require_mapping(loads(data, what, max_bytes=MAX_SNAPSHOT_BYTES), what)
        _require_schema_version(obj, what)
        _require_kind(obj, Snapshot.KIND, what)
        unknown = set(obj) - Snapshot._KEYS
        if unknown:
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        last_session = obj.get("lastSessionId")
        if last_session is not None:
            last_session = require_str(last_session, f"{what}.lastSessionId", max_chars=128)
            if not last_session:
                raise SchemaError(f"{what}.lastSessionId cannot be empty; use null")
        return Snapshot(
            agent_id=require_agent_id(obj.get("agentId")),
            name=require_str(obj.get("name", ""), f"{what}.name", max_chars=200),
            snapshot_id=require_hex32(obj.get("snapshotId"), f"{what}.snapshotId"),
            parent=SnapshotRef.parse(obj.get("parent"), f"{what}.parent"),
            created_at=require_timestamp(obj.get("createdAt"), f"{what}.createdAt"),
            device_id=require_hex32(obj.get("deviceId"), f"{what}.deviceId"),
            runtime=RuntimeInfo.parse(obj.get("runtime")),
            last_session_id=last_session,
            template=TemplateOrigin.parse(obj.get("template")),
            files=_parse_files(obj.get("files", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": Snapshot.KIND,
            "agentId": self.agent_id,
            "name": self.name,
            "snapshotId": self.snapshot_id,
            "parent": self.parent.to_dict() if self.parent else None,
            "createdAt": self.created_at,
            "deviceId": self.device_id,
            "runtime": self.runtime.to_dict(),
            "lastSessionId": self.last_session_id,
            "template": self.template.to_dict() if self.template else None,
            "files": {p: r.to_dict() for p, r in sorted(self.files.items())},
        }

    def to_bytes(self) -> bytes:
        return dumps(self.to_dict())

    def objects(self) -> Dict[str, int]:
        """Every object this snapshot references, mapped to its size."""
        out: Dict[str, int] = {}
        for record in self.files.values():
            for piece in record.pieces:
                out[piece.object] = piece.size
        return out


@dataclass
class TemplateSnapshot:
    """A public template: reusable instructions and assets, no personal state."""

    template_id: str
    snapshot_id: str
    created_at: str
    runtime: RuntimeInfo
    files: Dict[str, FileRecord] = field(default_factory=dict)
    name: str = ""
    description: str = ""
    parent: Optional[SnapshotRef] = None

    KIND = "template-snapshot"
    _KEYS = frozenset({
        "schemaVersion", "kind", "templateId", "name", "description",
        "snapshotId", "parent", "createdAt", "runtime", "files",
    })

    @staticmethod
    def parse(data: bytes) -> "TemplateSnapshot":
        what = "template snapshot"
        obj = require_mapping(loads(data, what, max_bytes=MAX_SNAPSHOT_BYTES), what)
        _require_schema_version(obj, what)
        _require_kind(obj, TemplateSnapshot.KIND, what)
        unknown = set(obj) - TemplateSnapshot._KEYS
        if unknown:
            # Catches a private agent snapshot renamed to look like a template.
            raise SchemaError(f"{what} has unsupported keys {sorted(unknown)}")
        return TemplateSnapshot(
            template_id=require_agent_id(obj.get("templateId"), "templateId"),
            name=require_str(obj.get("name", ""), f"{what}.name", max_chars=200),
            description=require_str(
                obj.get("description", ""), f"{what}.description", max_chars=1000),
            snapshot_id=require_hex32(obj.get("snapshotId"), f"{what}.snapshotId"),
            parent=SnapshotRef.parse(obj.get("parent"), f"{what}.parent"),
            created_at=require_timestamp(obj.get("createdAt"), f"{what}.createdAt"),
            runtime=RuntimeInfo.parse(obj.get("runtime")),
            files=_parse_files(obj.get("files", {}), allowed="template"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": TemplateSnapshot.KIND,
            "templateId": self.template_id,
            "name": self.name,
            "description": self.description,
            "snapshotId": self.snapshot_id,
            "parent": self.parent.to_dict() if self.parent else None,
            "createdAt": self.created_at,
            "runtime": self.runtime.to_dict(),
            "files": {p: r.to_dict() for p, r in sorted(self.files.items())},
        }

    def to_bytes(self) -> bytes:
        return dumps(self.to_dict())

    def review_digest(self) -> str:
        """A deterministic digest over what publication would expose.

        Covers metadata plus each path's hash, size and executable bit, so a
        non-interactive publish can be confirmed against exact content.
        """
        parts = [
            f"templateId={self.template_id}",
            f"name={self.name}",
            f"description={self.description}",
            f"runtime={self.runtime.name}/{self.runtime.version}/{self.runtime.adapter}",
        ]
        for path, record in sorted(self.files.items()):
            parts.append(
                f"{path}\t{record.sha256}\t{record.size}\t{int(record.executable)}"
            )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# -- portable configuration (plan 9) -----------------------------------------

DEFAULT_TOOLSETS = ("hermes-cli",)
MAX_TOOLSETS = 100
MAX_TOOLSET_CHARS = 128
MAX_MODEL_CHARS = 1024


@dataclass
class PortableConfig:
    """The small allowlist of settings that travel with an agent.

    Deliberately not a copy of `config.yaml`. Anything that could carry a
    credential, an endpoint, or a shell command stays on the device, so a
    downloaded document cannot reconfigure where the agent sends its data.
    """

    model: str = ""
    toolsets: List[str] = field(default_factory=lambda: list(DEFAULT_TOOLSETS))
    max_turns: int = 90
    memory_enabled: bool = True
    user_profile_enabled: bool = True
    memory_char_limit: int = 2200
    user_char_limit: int = 1375

    _KEYS = frozenset({"schemaVersion", "model", "toolsets", "agent", "memory"})
    _MEMORY_KEYS = frozenset({
        "memory_enabled", "user_profile_enabled", "memory_char_limit", "user_char_limit",
    })

    @staticmethod
    def parse(data: bytes) -> "PortableConfig":
        what = "portable config"
        obj = require_mapping(
            loads(data, what, max_bytes=MAX_CORE_MARKDOWN_BYTES), what)
        _require_schema_version(obj, what)
        unknown = set(obj) - PortableConfig._KEYS
        if unknown:
            # A new key is a schema decision, never an automatic passthrough.
            raise SchemaError(
                f"{what} has unsupported keys {sorted(unknown)}; extending the "
                "allowlist requires a schema update"
            )

        raw_toolsets = obj.get("toolsets", list(DEFAULT_TOOLSETS))
        if not isinstance(raw_toolsets, list):
            raise SchemaError(f"{what}.toolsets must be a list")
        if len(raw_toolsets) > MAX_TOOLSETS:
            raise SchemaError(f"{what}.toolsets has more than {MAX_TOOLSETS} entries")
        toolsets = [
            require_str(t, f"{what}.toolsets[{i}]", max_chars=MAX_TOOLSET_CHARS)
            for i, t in enumerate(raw_toolsets)
        ]
        if any(not t.strip() for t in toolsets):
            raise SchemaError(f"{what}.toolsets has an empty entry")

        agent = require_mapping(obj.get("agent", {}), f"{what}.agent")
        agent_unknown = set(agent) - {"max_turns"}
        if agent_unknown:
            raise SchemaError(f"{what}.agent has unsupported keys {sorted(agent_unknown)}")

        memory = require_mapping(obj.get("memory", {}), f"{what}.memory")
        memory_unknown = set(memory) - PortableConfig._MEMORY_KEYS
        if memory_unknown:
            raise SchemaError(f"{what}.memory has unsupported keys {sorted(memory_unknown)}")

        return PortableConfig(
            model=require_str(obj.get("model", ""), f"{what}.model",
                              max_chars=MAX_MODEL_CHARS),
            toolsets=toolsets,
            max_turns=require_int(agent.get("max_turns", 90), f"{what}.agent.max_turns",
                                  low=1, high=1000),
            memory_enabled=require_bool(memory.get("memory_enabled", True),
                                        f"{what}.memory.memory_enabled"),
            user_profile_enabled=require_bool(memory.get("user_profile_enabled", True),
                                              f"{what}.memory.user_profile_enabled"),
            memory_char_limit=require_int(memory.get("memory_char_limit", 2200),
                                          f"{what}.memory.memory_char_limit",
                                          low=1, high=64_000),
            user_char_limit=require_int(memory.get("user_char_limit", 1375),
                                        f"{what}.memory.user_char_limit",
                                        low=1, high=64_000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "model": self.model,
            "toolsets": list(self.toolsets),
            "agent": {"max_turns": self.max_turns},
            "memory": {
                "memory_enabled": self.memory_enabled,
                "user_profile_enabled": self.user_profile_enabled,
                "memory_char_limit": self.memory_char_limit,
                "user_char_limit": self.user_char_limit,
            },
        }

    def to_bytes(self) -> bytes:
        return dumps(self.to_dict())
