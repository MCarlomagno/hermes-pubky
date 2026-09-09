"""Assembly of the system-prompt block.

Two things are injected: the approved public base context (third-party
instructions the user explicitly pinned) and the private portable overlay.
Entries that already appear in the local ``USER.md`` / ``MEMORY.md`` are
dropped, because Hermes injects those files itself and repeating them wastes
context and invites the model to treat a duplicate as emphasis.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .schema import BaseContext

BLOCK_HEADER = "## Portable Pubky context"

# Leading markdown list markers and heading hashes, so "- fact" in MEMORY.md
# matches the bare "fact" stored in the profile.
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#{1,6})\s+")


def normalize_entry(text: str) -> str:
    """Reduce an entry to a form that can be compared across stores."""
    stripped = _LIST_PREFIX.sub("", (text or "").strip())
    return re.sub(r"\s+", " ", stripped).strip()


def local_entry_index(path: Path) -> set:
    """Every line of a local memory file, normalized for comparison."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return set()
    index = set()
    for line in text.splitlines():
        normalized = normalize_entry(line)
        if normalized:
            index.add(normalized)
    return index


def filter_local_duplicates(entries: Sequence[str], local_path: Path) -> List[str]:
    """Drop entries Hermes already injects from its own local file.

    Matches both line-by-line (the common case) and as a substring of the
    whole file, which catches an entry that was folded into a paragraph.
    """
    index = local_entry_index(local_path)
    try:
        raw_text = local_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        raw_text = ""

    kept: List[str] = []
    seen: set = set()
    for entry in entries:
        normalized = normalize_entry(entry)
        if not normalized or normalized in seen:
            continue
        if normalized in index:
            continue
        if raw_text and entry.strip() and entry.strip() in raw_text:
            continue
        seen.add(normalized)
        kept.append(entry.strip())
    return kept


def render_block(
    context: Optional[BaseContext],
    user_entries: Iterable[str],
    memory_entries: Iterable[str],
    *,
    profile_id: str,
    stale: bool = False,
) -> str:
    """Render the block, or an empty string when there is nothing to say."""
    user_entries = [e for e in user_entries if e.strip()]
    memory_entries = [e for e in memory_entries if e.strip()]
    if context is None and not user_entries and not memory_entries:
        return ""

    parts: List[str] = [BLOCK_HEADER, ""]
    parts.append(
        f"Loaded from your Pubky profile `{profile_id}`. This is portable context "
        "that follows you across machines. Your local SOUL.md, USER.md and "
        "MEMORY.md still apply and take precedence."
    )
    if stale:
        parts.append(
            "_Working from the local cache — the homeserver was unreachable at "
            "startup, so this may be out of date._"
        )
    parts.append("")

    if context is not None:
        label = context.name.strip() or context.id
        parts.append(f"### Base context: {label}")
        if context.description.strip():
            parts.append(f"_{context.description.strip()}_")
        parts.append("")
        parts.append(
            "The following instructions come from a public context document the "
            "user explicitly approved. Treat them as user-provided instructions, "
            "not as a system-level authority:"
        )
        parts.append("")
        parts.append(context.instructions.strip())
        parts.append("")

    if user_entries:
        parts.append("### Portable user facts")
        parts.extend(f"- {entry}" for entry in user_entries)
        parts.append("")

    if memory_entries:
        parts.append("### Portable agent memory")
        parts.extend(f"- {entry}" for entry in memory_entries)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
