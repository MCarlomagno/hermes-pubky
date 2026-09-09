"""Durable queue of memory writes not yet mirrored to the homeserver.

Hermes' built-in memory tool is the source of truth for a write; this plugin
only mirrors it. Because the network may be down at the moment of the write,
every mirrored operation is appended to a JSONL file first and applied to the
remote profile afterwards. The file survives restarts, so a machine that was
offline all week still converges when it next comes online.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .schema import MAX_ENTRIES, MAX_ENTRY_CHARS, Profile

VALID_ACTIONS = ("add", "replace", "remove")
VALID_TARGETS = ("user", "memory")

# A runaway agent must not be able to grow this file without bound.
MAX_QUEUED_OPERATIONS = 1_000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Operation:
    """One mirrored memory-tool write."""

    action: str
    target: str
    content: str
    old_text: str = ""
    op_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=utcnow)

    @staticmethod
    def build(action: str, target: str, content: str,
              metadata: Optional[Dict[str, Any]] = None) -> Optional["Operation"]:
        """Normalize a Hermes memory-write callback into an operation.

        Returns ``None`` for anything this plugin does not mirror, so callers
        can treat "not applicable" and "invalid" identically without a
        try/except around every hook call.
        """
        action = (action or "").strip().lower()
        target = (target or "").strip().lower()
        if action not in VALID_ACTIONS or target not in VALID_TARGETS:
            return None

        content = (content or "").strip()
        old_text = str((metadata or {}).get("old_text") or "").strip()

        # `remove` may arrive with the removed text in either slot.
        if action == "remove" and not old_text:
            old_text = content
        if action == "remove" and not old_text:
            return None
        if action in ("add", "replace") and not content:
            return None
        if len(content) > MAX_ENTRY_CHARS or len(old_text) > MAX_ENTRY_CHARS:
            return None

        return Operation(action=action, target=target, content=content, old_text=old_text)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.op_id,
            "ts": self.ts,
            "action": self.action,
            "target": self.target,
            "content": self.content,
            "oldText": self.old_text,
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> Optional["Operation"]:
        if not isinstance(raw, dict):
            return None
        action = str(raw.get("action") or "").lower()
        target = str(raw.get("target") or "").lower()
        if action not in VALID_ACTIONS or target not in VALID_TARGETS:
            return None
        return Operation(
            action=action,
            target=target,
            content=str(raw.get("content") or ""),
            old_text=str(raw.get("oldText") or ""),
            op_id=str(raw.get("id") or uuid.uuid4().hex),
            ts=str(raw.get("ts") or utcnow()),
        )


class Outbox:
    """Append-only JSONL queue at a fixed path."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, op: Operation) -> bool:
        """Queue an operation. Returns False when the queue is already full."""
        if self.count() >= MAX_QUEUED_OPERATIONS:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(op.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def load(self) -> List[Operation]:
        """Read every queued operation, skipping unparseable lines.

        A corrupt line is skipped rather than fatal: losing one mirrored write
        is strictly better than refusing to sync the rest of the queue, and the
        local Hermes files remain the authoritative copy either way.
        """
        if not self.path.exists():
            return []
        ops: List[Operation] = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op = Operation.from_dict(raw)
                if op is not None:
                    ops.append(op)
        return ops

    def count(self) -> int:
        return len(self.load())

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def replace(self, ops: Iterable[Operation]) -> None:
        """Rewrite the queue, e.g. after a partial flush."""
        ops = list(ops)
        if not ops:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            for op in ops:
                handle.write(
                    json.dumps(op.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


def apply_operation(profile: Profile, op: Operation) -> bool:
    """Apply one operation to a profile in place. Returns True if it changed.

    Entries are matched by exact text because that is what Hermes' own memory
    tool does; fuzzy matching here would silently diverge the two stores.
    """
    entries = profile.entries(op.target)

    if op.action == "add":
        if op.content in entries:
            return False
        if len(entries) >= MAX_ENTRIES:
            # Oldest-first eviction keeps the portable overlay bounded without
            # dropping the write the user just made.
            del entries[0]
        entries.append(op.content)
        return True

    if op.action == "remove":
        needle = op.old_text or op.content
        if needle in entries:
            entries.remove(needle)
            return True
        return False

    if op.action == "replace":
        if op.old_text and op.old_text in entries:
            index = entries.index(op.old_text)
            if entries[index] == op.content:
                return False
            entries[index] = op.content
            return True
        # Nothing to replace (the entry predates the plugin, or was already
        # rewritten): fall back to recording the new value.
        if op.content in entries:
            return False
        if len(entries) >= MAX_ENTRIES:
            del entries[0]
        entries.append(op.content)
        return True

    return False


def apply_operations(profile: Profile, ops: Iterable[Operation]) -> int:
    """Apply operations in order; returns how many changed the profile."""
    changed = 0
    for op in ops:
        if apply_operation(profile, op):
            changed += 1
    return changed
