"""Document schemas for the public base context and the private profile.

Both documents cross a trust boundary before they reach a prompt: the public
context is authored by someone else, and the private profile is served by a
homeserver operator. Validation here is therefore strict and total — a
document that does not match exactly is rejected rather than repaired, and
every string that can reach the system prompt is length-bounded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

# Bounds. The transport already caps a document at 64 KiB; these keep any
# single field from consuming that budget on its own.
MAX_ENTRY_CHARS = 4_000
MAX_ENTRIES = 200
MAX_INSTRUCTIONS_CHARS = 40_000
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 1_000
MAX_ID_CHARS = 128


class SchemaError(ValueError):
    """A document did not match its schema."""


def _require_mapping(raw: Any, what: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise SchemaError(f"{what} must be a JSON object, got {type(raw).__name__}")
    return raw


def _require_str(obj: Dict[str, Any], key: str, what: str, *, max_chars: int,
                 required: bool = True, default: str = "") -> str:
    if key not in obj or obj[key] is None:
        if required:
            raise SchemaError(f"{what} is missing required field {key!r}")
        return default
    value = obj[key]
    if not isinstance(value, str):
        raise SchemaError(f"{what} field {key!r} must be a string, got {type(value).__name__}")
    if len(value) > max_chars:
        raise SchemaError(
            f"{what} field {key!r} is {len(value)} chars, over the {max_chars} limit"
        )
    return value


def _require_schema_version(obj: Dict[str, Any], what: str) -> int:
    version = obj.get("schemaVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SchemaError(f"{what} has a missing or non-integer 'schemaVersion'")
    if version != SCHEMA_VERSION:
        raise SchemaError(
            f"{what} declares schemaVersion {version}; this plugin only understands "
            f"{SCHEMA_VERSION}. Upgrade hermes-pubky to read it."
        )
    return version


def _clean_entries(raw: Any, what: str) -> List[str]:
    """Validate an entry list, dropping blanks but rejecting wrong types.

    Blank entries are dropped rather than rejected because they carry no
    information and can legitimately appear after an edit; a non-string entry
    means the document is not what we think it is, so that fails loudly.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SchemaError(f"{what} must be a list, got {type(raw).__name__}")
    if len(raw) > MAX_ENTRIES:
        raise SchemaError(f"{what} has {len(raw)} entries, over the {MAX_ENTRIES} limit")
    out: List[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str):
            raise SchemaError(
                f"{what}[{index}] must be a string, got {type(entry).__name__}"
            )
        if len(entry) > MAX_ENTRY_CHARS:
            raise SchemaError(
                f"{what}[{index}] is {len(entry)} chars, over the {MAX_ENTRY_CHARS} limit"
            )
        stripped = entry.strip()
        if stripped:
            out.append(stripped)
    return out


@dataclass(frozen=True)
class BaseContext:
    """A public, shareable agent context document."""

    id: str
    name: str
    description: str
    instructions: str

    @staticmethod
    def parse(raw: Any) -> "BaseContext":
        obj = _require_mapping(raw, "base context")
        _require_schema_version(obj, "base context")
        return BaseContext(
            id=_require_str(obj, "id", "base context", max_chars=MAX_ID_CHARS),
            name=_require_str(obj, "name", "base context", max_chars=MAX_NAME_CHARS,
                              required=False),
            description=_require_str(obj, "description", "base context",
                                     max_chars=MAX_DESCRIPTION_CHARS, required=False),
            instructions=_require_str(obj, "instructions", "base context",
                                      max_chars=MAX_INSTRUCTIONS_CHARS),
        )

    @staticmethod
    def parse_bytes(data: bytes) -> "BaseContext":
        return BaseContext.parse(_load_json(data, "base context"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
        }


@dataclass(frozen=True)
class BaseContextRef:
    """The pinned reference to an approved base context."""

    url: str
    sha256: str

    @staticmethod
    def parse(raw: Any) -> Optional["BaseContextRef"]:
        if raw is None:
            return None
        obj = _require_mapping(raw, "baseContext")
        url = _require_str(obj, "url", "baseContext", max_chars=2048)
        digest = _require_str(obj, "sha256", "baseContext", max_chars=64)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise SchemaError("baseContext.sha256 must be 64 lowercase hex characters")
        return BaseContextRef(url=url, sha256=digest.lower())

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "sha256": self.sha256}


@dataclass
class Profile:
    """The private, portable user/memory overlay."""

    profile_id: str
    base_context: Optional[BaseContextRef] = None
    user: List[str] = field(default_factory=list)
    memory: List[str] = field(default_factory=list)
    revision: int = 0
    updated_at: str = ""

    @staticmethod
    def parse(raw: Any) -> "Profile":
        obj = _require_mapping(raw, "profile")
        _require_schema_version(obj, "profile")
        revision = obj.get("revision", 0)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise SchemaError("profile 'revision' must be a non-negative integer")
        return Profile(
            profile_id=_require_str(obj, "profileId", "profile", max_chars=64),
            base_context=BaseContextRef.parse(obj.get("baseContext")),
            user=_clean_entries(obj.get("user"), "profile.user"),
            memory=_clean_entries(obj.get("memory"), "profile.memory"),
            revision=revision,
            updated_at=_require_str(obj, "updatedAt", "profile", max_chars=64,
                                    required=False),
        )

    @staticmethod
    def parse_bytes(data: bytes) -> "Profile":
        return Profile.parse(_load_json(data, "profile"))

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "profileId": self.profile_id,
            "baseContext": self.base_context.to_dict() if self.base_context else None,
            "user": list(self.user),
            "memory": list(self.memory),
            "revision": self.revision,
            "updatedAt": self.updated_at,
        }
        return doc

    def to_bytes(self) -> bytes:
        """Serialize deterministically, so an unchanged profile hashes the same."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def entries(self, target: str) -> List[str]:
        if target == "user":
            return self.user
        if target == "memory":
            return self.memory
        raise SchemaError(f"unknown memory target {target!r}")


def _load_json(data: bytes, what: str) -> Any:
    if len(data) == 0:
        raise SchemaError(f"{what} document is empty")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{what} document is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{what} document is not valid JSON: {exc}") from exc
