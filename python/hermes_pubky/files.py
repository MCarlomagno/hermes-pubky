"""The `agent files` subcommands.

A saved workspace file is only ever created, fetched or removed through the
inventory, so the absence of a never-fetched file is never read as a deletion
and a fetch never overwrites uncheckpointed local work.

Reference: implementation plan section 11.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .journal import Journal, MaterializedFile
from .models import SchemaError, Snapshot, normalize_new_path
from .objects import ObjectCache
from .paths import GRANT_ENV, Layout, read_env_file
from .projection import (
    Ignore,
    Projection,
    excluded_reason,
    looks_like_private_key,
)
from .supervisor import EXIT_AUTH, EXIT_INTEGRITY, EXIT_OK, EXIT_USAGE


def files_command(layout: Layout, args: Any) -> int:
    command = getattr(args, "files_command", None)
    layout.ensure()
    journal = Journal(layout.journal_file)
    try:
        if command == "list":
            return _list(layout, journal, getattr(args, "prefix", "") or "")
        if command == "fetch":
            return _fetch(layout, journal, args)
        if command == "import":
            return _import(layout, journal, args)
        if command == "remove":
            return _remove(layout, journal, args.relative_path)
        print("\n  Usage: hermes-pubky agent files <id> list|fetch|import|remove\n")
        return EXIT_USAGE
    finally:
        journal.close()


def _list(layout: Layout, journal: Journal, prefix: str) -> int:
    rows = []
    for record in journal.list_materialized():
        if not record.logical_path.startswith("workspace/") or record.explicit_delete:
            continue
        relative = record.logical_path[len("workspace/"):]
        if prefix and not relative.startswith(prefix):
            continue
        local = layout.workspace / relative
        present = record.present and local.is_file()
        size = local.stat().st_size if local.is_file() else None
        state = "dirty" if record.dirty else ("local" if present else "remote-only")
        rows.append((relative, state, size))

    if not rows:
        print(f"\n  No saved workspace files{f' under {prefix!r}' if prefix else ''}.\n")
        return EXIT_OK
    print(f"\nWorkspace files for {layout.agent_id}\n" + "-" * 56)
    for relative, state, size in sorted(rows):
        shown = f"{size:>9}" if size is not None else "        -"
        print(f"  {state:<12} {shown}  {relative}")
    print()
    return EXIT_OK


def _snapshot_and_projection(layout: Layout, journal: Journal):
    from . import hermes_adapter as adapter
    from .paths import DeviceIdentity

    snapshot_id = journal.get_setting("cached_snapshot_id")
    snapshot = None
    if isinstance(snapshot_id, str):
        path = layout.cached_snapshot(snapshot_id)
        if path.is_file():
            try:
                snapshot = Snapshot.parse(path.read_bytes())
            except SchemaError:
                snapshot = None
    projection = Projection(
        layout, journal, ObjectCache(layout.cached_objects),
        runtime=adapter.runtime_info(),
        device_id=DeviceIdentity.load_or_create(layout.root).device_id)
    return snapshot, projection


def _fetch(layout: Layout, journal: Journal, args: Any) -> int:
    from .storage import AgentRemote

    grant = read_env_file(layout.credentials_file).get(GRANT_ENV, "")
    if not grant:
        print(f"\n  Not authorized; run 'hermes-pubky agent login "
              f"{layout.agent_id}'.\n")
        return EXIT_AUTH

    snapshot, projection = _snapshot_and_projection(layout, journal)
    if snapshot is None:
        print("\n  No saved snapshot is cached locally; run the agent once "
              "or 'agent sync' first.\n")
        return EXIT_INTEGRITY

    if getattr(args, "fetch_all", False):
        wanted = [r.logical_path for r in journal.list_materialized()
                  if r.logical_path.startswith("workspace/")
                  and not r.present and not r.explicit_delete]
    else:
        if not args.path:
            print("\n  Usage: agent files <id> fetch PATH | --all\n")
            return EXIT_USAGE
        wanted = [f"workspace/{args.path.lstrip('/')}"]

    missing = [p for p in wanted if p not in snapshot.files]
    if missing:
        print(f"\n  Not saved for this agent: {', '.join(missing)}\n")
        return EXIT_USAGE

    dirty = [p for p in wanted
             if (journal.get_materialized(p) or MaterializedFile(p, "", False, False, False)).dirty]
    if dirty:
        print(f"\n  These have local changes; fetching would overwrite them: "
              f"{', '.join(dirty)}\n")
        return EXIT_USAGE

    remote = AgentRemote.connect(grant, layout.owner, layout.agent_id)
    cache = ObjectCache(layout.cached_objects)
    pieces = {piece.object: piece
              for record in snapshot.files.values() for piece in record.pieces}

    def resolve(reference: str) -> Path:
        cached = cache.path_for(reference)
        if cached.is_file():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        remote.read_object_to_path(pieces[reference], cached)
        return cached

    installed = projection.materialize(snapshot, resolve, extra_paths=wanted)
    for logical in installed:
        print(f"  fetched {logical[len('workspace/'):]}")
    if not installed:
        print("  already present")
    print()
    return EXIT_OK


def _import(layout: Layout, journal: Journal, args: Any) -> int:
    source = Path(args.local_path).expanduser()
    if not source.is_file():
        print(f"\n  {source} is not a file.\n")
        return EXIT_USAGE
    if source.is_symlink():
        print("\n  Refusing to import a link; links are never followed.\n")
        return EXIT_USAGE
    if looks_like_private_key(source):
        print("\n  That looks like a private key. Credentials are outside this "
              "release's portability boundary.\n")
        return EXIT_USAGE

    try:
        relative = normalize_new_path(args.to.lstrip("/"))
    except SchemaError as exc:
        print(f"\n  {exc}\n")
        return EXIT_USAGE

    reason = excluded_reason(source, relative, Ignore.load(layout.workspace))
    if reason and not reason.startswith("listed in"):
        print(f"\n  {relative} would be excluded ({reason}); choose another "
              "destination name.\n")
        return EXIT_USAGE

    destination = layout.workspace / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    journal.mark_dirty(f"workspace/{relative}")
    journal.bump_generation()
    print(f"\n  Imported to {destination}.")
    print("  It joins the agent's saved files at the next checkpoint.\n")
    return EXIT_OK


def _remove(layout: Layout, journal: Journal, relative_path: str) -> int:
    relative = relative_path.lstrip("/")
    logical = f"workspace/{relative}"
    record = journal.get_materialized(logical)
    if record is None:
        print(f"\n  {relative} is not one of this agent's saved files.\n")
        return EXIT_USAGE

    local = layout.workspace / relative
    if local.is_file():
        backup = layout.recovery / "removed" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, backup)
        local.unlink()
        print(f"  a copy was kept at {backup}")
    journal.mark_deleted(logical)
    journal.bump_generation()
    print(f"\n  {relative} will be removed from the agent's saved files at the "
          "next checkpoint.")
    print("  Earlier checkpoints still contain it; this is not secure erasure.\n")
    return EXIT_OK
