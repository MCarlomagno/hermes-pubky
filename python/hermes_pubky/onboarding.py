"""Creating and attaching agents.

Fresh setup is the default and needs no previous plugin or Hermes profile. An
explicit `--from-hermes-home` is a one-time content import from the pinned
native layout, not a compatibility layer: the source is never modified and no
ongoing relationship is created.

There is no migration from the 0.1 plugin. An old-format address is refused
before any write, and old local or remote data is left untouched and
undiscovered.

Reference: implementation plan section 13.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import hermes_adapter as adapter
from .models import PortableConfig, SchemaError
from .paths import GRANT_ENV, Layout, layout_for, write_env_file, write_private
from .projection import scan_workspace
from .storage import AgentRemote, native
from .supervisor import (
    EXIT_AUTH,
    EXIT_INTEGRITY,
    EXIT_OK,
    EXIT_SAVED_LOCALLY,
    EXIT_USAGE,
)

STARTER_SOUL = """# Your agent

Replace this with how you want your agent to work: its focus, its tone, the
conventions it should follow. This file travels with the agent, so anything here
applies on every computer you run it from.
"""

STARTER_AGENTS_MD = """# Workspace

Files in this directory travel with the agent. Notes, references and outputs
belong here; anything elsewhere on the computer stays behind.
"""


def _out(text: str = "") -> None:
    print(text)


def _confirm(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        sys.stdout.write(f"  {question} ({hint}): ")
        sys.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def create_agent(*, agent_id: str, network: str,
                 from_hermes_home: Optional[Path] = None,
                 workspace: Optional[Path] = None) -> int:
    """Create a new remote agent and publish its first checkpoint."""
    module = native()
    try:
        scope = module.agent_scope(agent_id)
    except Exception as exc:  # noqa: BLE001
        _out(f"\n  {exc}\n")
        return EXIT_USAGE

    try:
        adapter.assert_supported_runtime()
    except SchemaError as exc:
        _out(f"\n  {exc}\n")
        return EXIT_INTEGRITY

    imported: Optional[ImportPlan] = None
    if from_hermes_home is not None or workspace is not None:
        try:
            imported = plan_import(from_hermes_home, workspace)
        except SchemaError as exc:
            _out(f"\n  {exc}\n")
            return EXIT_USAGE
        if not preview_import(imported):
            _out("\n  Nothing was imported and nothing was published.\n")
            return EXIT_OK

    from .cli import authorize

    secret = authorize(scope)
    remote = AgentRemote.connect(secret, _owner_of(secret), agent_id)
    owner = remote.owner

    if remote.read_head() is not None:
        _out(f"\n  {agent_id!r} already exists for {owner}. Use "
             f"'hermes-pubky agent attach {remote.uri}', or choose another id.\n")
        return EXIT_USAGE

    layout = layout_for(owner, agent_id, network=network).ensure()
    layout.assert_outside_workspace()
    write_env_file(layout.credentials_file, {GRANT_ENV: secret})

    if imported is not None:
        apply_import(imported, layout)
    else:
        _seed_fresh(layout)

    from .cli import save_connection

    save_connection(layout, network)
    return _publish_first(layout, remote, network,
                          imported.portable if imported else PortableConfig())


def _owner_of(secret: str) -> str:
    """The identity a grant belongs to.

    A capability names a path, not an owner, so this is only knowable after the
    grant is restored.
    """
    return native().session_owner(secret)


def _seed_fresh(layout: Layout) -> None:
    """A fresh profile: starter instructions, empty memory, empty workspace."""
    if not layout.soul_file.is_file():
        write_private(layout.soul_file, STARTER_SOUL.encode("utf-8"))
    for path in (layout.user_memory_file, layout.agent_memory_file):
        if not path.is_file():
            write_private(path, b"")
    if not layout.agents_md.is_file():
        write_private(layout.agents_md, STARTER_AGENTS_MD.encode("utf-8"))
    from .database import open_or_create

    open_or_create(layout)


def _publish_first(layout: Layout, remote: AgentRemote, network: str,
                   portable: PortableConfig) -> int:
    """Capture the new working copy and publish it, under the connection lock."""
    from .supervisor import Supervisor

    supervisor = Supervisor(layout, network=network, remote_factory=lambda: remote)
    with supervisor.session():
        # The generated configuration is where later runs read the portable
        # settings back from, so an imported model or toolset must land in it.
        adapter.write_config(layout, adapter.render_config(portable, layout, {}))
        result = supervisor.sync()
        base = supervisor._base()  # noqa: SLF001 - same package
    if not result.ok:
        _out(f"\n  Saved locally but not published: {result.detail}")
        _out(f"  Retry with 'hermes-pubky agent sync {layout.agent_id}'.\n")
        return EXIT_SAVED_LOCALLY

    _out(f"\n  Created {layout.agent_id}")
    _out(f"    address    {remote.uri}")
    _out(f"    workspace  {layout.workspace}")
    _out(f"    files      {len(base.files) if base else 0}")
    _out(f"\n  Run it with: hermes-pubky run {layout.agent_id}\n")
    return EXIT_OK


def attach_agent(*, uri: str, network: str) -> int:
    """Attach an existing agent to this machine."""
    module = native()
    try:
        owner, agent_id = module.parse_agent_uri(uri)
    except Exception as exc:  # noqa: BLE001
        _out(f"\n  {exc}")
        _out("\n  This release reads only /v2/ agent addresses. A 0.1 address is "
             "not supported and its data is left untouched.\n")
        return EXIT_USAGE

    try:
        adapter.assert_supported_runtime()
    except SchemaError as exc:
        _out(f"\n  {exc}\n")
        return EXIT_INTEGRITY

    from .cli import authorize, save_connection

    secret = authorize(module.agent_scope(agent_id))
    try:
        remote = AgentRemote.connect_uri(secret, uri)
    except Exception as exc:  # noqa: BLE001
        _out(f"\n  Could not use that grant: {exc}\n")
        return EXIT_AUTH
    if remote.owner != owner:
        _out(f"\n  That grant belongs to {remote.owner}, not {owner}.\n")
        return EXIT_AUTH

    head = remote.read_head()
    if head is None:
        _out(f"\n  There is no agent at {uri}.\n")
        return EXIT_INTEGRITY

    from .models import SnapshotRef

    snapshot = remote.read_snapshot(
        SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))

    layout = layout_for(owner, agent_id, network=network).ensure()
    layout.assert_outside_workspace()
    write_env_file(layout.credentials_file, {GRANT_ENV: secret})
    save_connection(layout, network)

    from .supervisor import Supervisor

    supervisor = Supervisor(layout, network=network, remote_factory=lambda: remote)
    with supervisor.session():
        # Anything already here is sealed against its own base first, so an
        # attach over an existing working copy reconciles rather than resets.
        supervisor.seal()
        if supervisor.journal.has_pending():
            _out("\n  This machine already has unsaved work for this agent; run "
                 f"'hermes-pubky agent sync {agent_id}' to reconcile it.\n")
            return EXIT_SAVED_LOCALLY
        try:
            supervisor.install(snapshot)
        except Exception as exc:  # noqa: BLE001
            _out(f"\n  Could not restore the working copy: {exc}\n")
            return EXIT_INTEGRITY

    _out(f"\n  Attached {agent_id}")
    _out(f"    owner      {owner}")
    _out(f"    checkpoint {snapshot.snapshot_id}")
    _out(f"    files      {len(snapshot.files)} saved "
         f"({sum(1 for p in snapshot.files if p.startswith('workspace/'))} in the workspace)")
    _out(f"\n  Configure any model credentials this machine needs in "
         f"{layout.secrets_file},")
    _out(f"  then run: hermes-pubky run {agent_id}\n")
    return EXIT_OK


# -- one-time import from a native Hermes profile ----------------------------

class ImportPlan:
    """What an explicit import would copy, before anything is written."""

    def __init__(self, source: Optional[Path], workspace: Optional[Path]) -> None:
        self.source = source
        self.workspace = workspace
        self.files: List[Tuple[str, Path, int]] = []
        self.excluded: List[Tuple[str, str]] = []
        self.database: Optional[Path] = None
        self.portable: PortableConfig = PortableConfig()


def plan_import(source: Optional[Path], workspace: Optional[Path]) -> ImportPlan:
    """Inventory a pinned-layout Hermes profile and/or a workspace. Reads only."""
    workspace = workspace.expanduser().resolve() if workspace else None
    if workspace is not None and not workspace.is_dir():
        raise SchemaError(f"{workspace} is not a directory")
    if source is None:
        plan = ImportPlan(None, workspace)
        _plan_workspace(plan)
        return plan

    source = source.expanduser().resolve()
    if not source.is_dir():
        raise SchemaError(f"{source} is not a directory")

    plan = ImportPlan(source, workspace)

    candidates = {
        "profile/SOUL.md": source / "SOUL.md",
        "profile/memories/USER.md": source / "memories" / "USER.md",
        "profile/memories/MEMORY.md": source / "memories" / "MEMORY.md",
    }
    for logical, path in candidates.items():
        if path.is_file():
            plan.files.append((logical, path, path.stat().st_size))
        elif logical == "profile/SOUL.md":
            plan.excluded.append((str(path), "not present"))

    # Root-level memory files belonged to an older layout; this release targets
    # the pinned one and does not fall back.
    for legacy in ("USER.md", "MEMORY.md"):
        if (source / legacy).is_file():
            plan.excluded.append(
                (legacy, "root-level memory is not the pinned 0.19 layout"))

    skills = source / "skills"
    if skills.is_dir():
        from .projection import scan_skills

        included, excluded = scan_skills(skills)
        for item in included:
            plan.files.append((item.logical_path, item.source,
                               item.source.stat().st_size))
        plan.excluded.extend(excluded)

    config = source / "config.yaml"
    if config.is_file():
        try:
            import yaml

            raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                plan.portable = adapter.extract_portable(raw)
        except Exception:
            plan.excluded.append(("config.yaml", "unreadable; defaults used"))
    # Raw configuration is never copied; only the allowlist is extracted.
    plan.excluded.append((".env", "credentials stay on their machine"))

    database = source / "state.db"
    if database.is_file():
        plan.database = database

    _plan_workspace(plan)

    # Plugin state of any kind is ignored, including the 0.1 plugin's.
    if (source / "plugins").is_dir():
        plan.excluded.append(("plugins/", "plugin state is never imported"))
    if (source / "pubky").is_dir():
        plan.excluded.append(("pubky/", "0.1 plugin data is unsupported"))

    return plan


def _plan_workspace(plan: ImportPlan) -> None:
    if plan.workspace is None:
        return
    included, excluded = scan_workspace(plan.workspace)
    for item in included:
        plan.files.append((item.logical_path, item.source,
                           item.source.stat().st_size))
    plan.excluded.extend(excluded)


def preview_import(plan: ImportPlan) -> bool:
    """Show what would be copied and ask. Cancellation imports nothing."""
    _out(f"\n  Importing from {plan.source or plan.workspace}\n" + "  " + "-" * 48)
    total = 0
    for logical, _path, size in plan.files:
        _out(f"    {logical:<44} {size:>9}")
        total += size
    if plan.database is not None:
        _out(f"    {'conversations/state.sqlite3':<44} "
             f"{plan.database.stat().st_size:>9}")
    _out(f"\n    {len(plan.files)} file(s), {total} bytes")
    if plan.workspace is not None:
        _out(f"    workspace copied from {plan.workspace}")
    if plan.excluded:
        _out(f"\n  Excluded ({len(plan.excluded)}):")
        for name, reason in plan.excluded[:12]:
            _out(f"    {name}  -  {reason}")
        if len(plan.excluded) > 12:
            _out(f"    ... and {len(plan.excluded) - 12} more")
    _out("\n  The source profile is never modified, and no ongoing link is "
         "created.")
    return _confirm("Import these into a new agent?", False)


def apply_import(plan: ImportPlan, layout: Layout) -> None:
    """Copy the planned bytes into the managed working copy."""
    mapping = {
        "profile/SOUL.md": layout.soul_file,
        "profile/memories/USER.md": layout.user_memory_file,
        "profile/memories/MEMORY.md": layout.agent_memory_file,
    }
    for logical, source, _size in plan.files:
        if logical in mapping:
            destination = mapping[logical]
        elif logical.startswith("profile/skills/"):
            destination = layout.skills_dir / logical[len("profile/skills/"):]
        elif logical.startswith("workspace/"):
            destination = layout.workspace / logical[len("workspace/"):]
        else:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exact bytes, and the executable bit for skill assets.
        shutil.copy2(source, destination)

    if not layout.soul_file.is_file():
        write_private(layout.soul_file, STARTER_SOUL.encode("utf-8"))
    if not layout.agents_md.is_file():
        write_private(layout.agents_md, STARTER_AGENTS_MD.encode("utf-8"))
    for path in (layout.user_memory_file, layout.agent_memory_file):
        if not path.is_file():
            write_private(path, b"")

    if plan.database is not None:
        # Through the backup API: a plain copy misses committed WAL frames.
        from .database import copy_database

        copy_database(plan.database, layout.state_db)
    else:
        from .database import open_or_create

        open_or_create(layout)
