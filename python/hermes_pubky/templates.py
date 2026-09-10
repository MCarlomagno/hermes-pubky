"""Public templates: staging, publishing, adopting, and explicit updates.

A template carries reusable instructions and assets and nothing personal. It is
published from an explicit directory, never from a live agent, so a private file
cannot be swept in. Adoption copies verified bytes into the adopter's own
private objects, so the author's homeserver going away does not break recovery.

Reference: implementation plan section 12.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from . import hermes_adapter as adapter
from .journal import Journal, new_id, utcnow
from .models import (
    PORTABLE_CONFIG_PATH,
    FileRecord,
    Head,
    PortableConfig,
    SchemaError,
    SnapshotRef,
    TemplateOrigin,
    TemplateSnapshot,
)
from .objects import ObjectCache, assemble, hash_bytes, stage_file
from .paths import (
    GRANT_ENV,
    Layout,
    read_env_file,
    template_dir,
    write_env_file,
    write_private,
)
from .projection import NoChange
from .storage import PublicTemplateRemote, TemplatePublisher, native
from .supervisor import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_INTEGRITY,
    EXIT_OK,
    EXIT_USAGE,
)

if TYPE_CHECKING:  # pragma: no cover
    from .supervisor import Supervisor

# The only logical paths a template may contain.
BUNDLE_FILES = {
    "template.json": None,
    "SOUL.md": "profile/SOUL.md",
    "AGENTS.md": "workspace/AGENTS.md",
    "portable.json": PORTABLE_CONFIG_PATH,
}
BUNDLE_SKILLS = "skills"


def _out(text: str = "") -> None:
    print(text)


def _confirm(question: str) -> bool:
    """Ask, defaulting to no. Publication never defaults to yes."""
    try:
        sys.stdout.write(f"  {question} (y/N): ")
        sys.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# -- bundles -----------------------------------------------------------------

@dataclass
class Bundle:
    """A staging directory ready to become a public template."""

    directory: Path
    template_id: str
    name: str
    description: str
    files: Dict[str, Path] = field(default_factory=dict)

    def snapshot(self, staging: Path) -> Tuple[TemplateSnapshot, Dict[str, Path]]:
        """Build the template snapshot and stage its objects."""
        records: Dict[str, FileRecord] = {}
        objects: Dict[str, Path] = {}
        for logical, source in sorted(self.files.items()):
            staged = stage_file(source, logical, staging)
            records[logical] = staged.record
            objects.update(staged.objects)
        snapshot = TemplateSnapshot(
            template_id=self.template_id, snapshot_id=new_id(),
            created_at=utcnow(), runtime=adapter.runtime_info(),
            files=records, name=self.name, description=self.description)
        # Parse back so a staging bug cannot produce a document the adopter
        # would reject.
        TemplateSnapshot.parse(snapshot.to_bytes())
        return snapshot, objects


def read_bundle(directory: Path, template_id: Optional[str] = None) -> Bundle:
    """Validate a staging directory and list exactly what it would publish."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise SchemaError(f"{directory} is not a directory")

    manifest_path = directory / "template.json"
    if not manifest_path.is_file():
        raise SchemaError(
            f"{directory} has no template.json; create one with "
            "'hermes-pubky template init'")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"template.json is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SchemaError("template.json must contain an object")

    resolved_id = template_id or manifest.get("id")
    if not isinstance(resolved_id, str):
        raise SchemaError("template.json needs a string 'id'")
    native().template_scope(resolved_id)  # validates the id shape

    bundle = Bundle(
        directory=directory, template_id=resolved_id,
        name=str(manifest.get("name") or resolved_id),
        description=str(manifest.get("description") or ""))

    for name, logical in BUNDLE_FILES.items():
        if logical is None:
            continue
        path = directory / name
        if path.is_file():
            bundle.files[logical] = path

    skills = directory / BUNDLE_SKILLS
    if skills.is_dir():
        from .projection import scan_skills

        included, excluded = scan_skills(skills)
        for item in included:
            bundle.files[item.logical_path] = item.source
        for name, reason in excluded:
            _out(f"    excluded {name} ({reason})")

    if not bundle.files:
        raise SchemaError(
            f"{directory} contains nothing publishable; add SOUL.md, skills/, "
            "AGENTS.md or portable.json")

    # A portable.json in a bundle must still pass the allowlist.
    if PORTABLE_CONFIG_PATH in bundle.files:
        PortableConfig.parse(bundle.files[PORTABLE_CONFIG_PATH].read_bytes())
    return bundle


# -- commands ----------------------------------------------------------------

def template_command(args: Any, resolve: Callable[[str], Layout]) -> int:
    command = getattr(args, "template_command", None)
    try:
        if command == "init":
            return cmd_init(Path(args.directory))
        if command == "inspect":
            return cmd_inspect(Path(args.directory), as_json=args.as_json)
        if command == "publish":
            return cmd_publish(Path(args.directory), args.template_id,
                               args.confirm_public, network=args.network)
        if command in ("adopt", "update"):
            from .supervisor import Supervisor

            layout = resolve(args.agent_id)
            with Supervisor(layout, network=args.network).session() as supervisor:
                if command == "adopt":
                    return cmd_adopt(supervisor, args.uri)
                return cmd_update(supervisor)
    except SchemaError as exc:
        _out(f"\n  {exc}\n")
        return EXIT_USAGE
    _out("\n  Usage: hermes-pubky template init|inspect|publish|adopt|update\n")
    return EXIT_USAGE


def cmd_init(directory: Path) -> int:
    """Create a clean staging bundle."""
    directory = directory.expanduser()
    if directory.exists() and any(directory.iterdir()):
        _out(f"\n  {directory} is not empty; choose an empty directory.\n")
        return EXIT_USAGE
    (directory / BUNDLE_SKILLS).mkdir(parents=True, exist_ok=True)
    (directory / "template.json").write_text(json.dumps({
        "id": "my-template",
        "name": "My template",
        "description": "What this template is for.",
        "runtime": adapter.runtime_info().to_dict(),
    }, indent=2) + "\n", encoding="utf-8")
    (directory / "SOUL.md").write_text(
        "# Instructions\n\nWhat an agent adopting this template should do.\n",
        encoding="utf-8")
    (directory / "AGENTS.md").write_text(
        "# Workspace\n\nHow to use this agent's workspace.\n", encoding="utf-8")
    (directory / "portable.json").write_bytes(PortableConfig().to_bytes())
    _out(f"\n  Created a template bundle at {directory}")
    _out("  Edit it, then: hermes-pubky template inspect " + str(directory) + "\n")
    return EXIT_OK


def cmd_inspect(directory: Path, *, as_json: bool = False) -> int:
    """Validate a bundle and show exactly what publishing would expose."""
    bundle = read_bundle(directory)
    staging = directory / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        snapshot, _objects = bundle.snapshot(staging)
        digest = snapshot.review_digest()
        if as_json:
            _out(json.dumps({
                "templateId": snapshot.template_id, "name": snapshot.name,
                "description": snapshot.description, "reviewDigest": digest,
                "files": {p: {"sha256": r.sha256, "size": r.size,
                              "executable": r.executable}
                          for p, r in sorted(snapshot.files.items())},
            }, indent=2, sort_keys=True))
        else:
            _describe(snapshot, digest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return EXIT_OK


def _describe(snapshot: TemplateSnapshot, digest: str) -> None:
    _out(f"\n  template   {snapshot.template_id}")
    _out(f"  name       {snapshot.name}")
    _out(f"  description {snapshot.description}")
    _out("\n  These files would become public:")
    for logical, record in sorted(snapshot.files.items()):
        flag = " (executable)" if record.executable else ""
        _out(f"    {logical:<44} {record.size:>9}{flag}")
    _out(f"\n  review digest  {digest}")
    _out("\n  Nothing else is published: no memories, no conversations, no "
         "private workspace files.\n")


def cmd_publish(directory: Path, template_id: str, confirm: Optional[str],
                *, network: str) -> int:
    """Publish a bundle, after an explicit confirmation of its exact bytes."""
    bundle = read_bundle(directory, template_id)
    staging = directory / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        snapshot, objects = bundle.snapshot(staging)
        digest = snapshot.review_digest()
        _describe(snapshot, digest)

        if confirm is not None:
            if confirm != digest:
                _out("  --confirm-public does not match this bundle.")
                _out(f"    expected {digest}\n")
                return EXIT_USAGE
        elif not _confirm("Publish these files publicly?"):
            _out("\n  Nothing was published.\n")
            return EXIT_OK

        from .cli import authorize

        scope = native().template_scope(template_id)
        secret = authorize(scope)
        publisher = TemplatePublisher.connect(
            secret, native().session_owner(secret), template_id)

        # Objects first, then the snapshot, then the head.
        for reference, path in sorted(objects.items()):
            piece = next(p for record in snapshot.files.values()
                         for p in record.pieces if p.object == reference)
            publisher.put_object_from_path(piece, path)
        ref = publisher.put_snapshot(snapshot)
        publisher.write_head(Head(kind=Head.TEMPLATE, id=template_id,
                                  snapshot_id=ref.snapshot_id, sha256=ref.sha256))

        owner = native().session_owner(secret)
        receipt_dir = template_dir(owner, template_id, network=network)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        write_env_file(receipt_dir / "credentials.env", {GRANT_ENV: secret})
        write_private(receipt_dir / "receipt.json", json.dumps({
            "templateId": template_id, "snapshotId": ref.snapshot_id,
            "sha256": ref.sha256, "reviewDigest": digest,
            "publishedAt": utcnow(), "uri": publisher.uri,
        }, indent=2, sort_keys=True).encode("utf-8"))

        _out(f"\n  Published {template_id}")
        _out(f"    {publisher.uri}")
        _out(f"\n  Others adopt it with: hermes-pubky template adopt <agent-id> "
             f"{publisher.uri}\n")
        return EXIT_OK
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_adopt(supervisor: "Supervisor", uri: str) -> int:
    """Copy a reviewed template into a private agent as its own content."""
    layout = supervisor.layout
    if not read_env_file(layout.credentials_file).get(GRANT_ENV, ""):
        _out(f"\n  Not authorized; run 'hermes-pubky agent login "
             f"{layout.agent_id}'.\n")
        return EXIT_AUTH

    remote = PublicTemplateRemote.from_uri(uri)
    head = remote.read_head()
    if head is None:
        _out(f"\n  There is no template at {uri}.\n")
        return EXIT_INTEGRITY
    template = remote.read_snapshot(
        SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))

    base = _settled_base(supervisor)
    if base is None:
        return EXIT_CONFLICT

    overlaps = [p for p in template.files if p in base.files]
    _describe(template, template.review_digest())
    if overlaps:
        _out("  These would replace files you already have:")
        for path in sorted(overlaps):
            _out(f"    {path}")
        _out("")
    if not _confirm(f"Adopt this template into {layout.agent_id}?"):
        _out("\n  Nothing was adopted.\n")
        return EXIT_OK

    installed, portable = _copy_template(layout, supervisor.journal, remote, template)
    _out(f"\n  Copied {len(installed)} file(s) into {layout.agent_id}.")
    return _checkpoint_after_template(
        supervisor, portable,
        TemplateOrigin(url=remote.uri, snapshot_id=template.snapshot_id,
                       sha256=head.sha256, adopted_at=utcnow(),
                       managed_paths={p: r.sha256 for p, r in template.files.items()}))


def cmd_update(supervisor: "Supervisor") -> int:
    """Compare three versions of each adopted path and apply none on conflict."""
    layout = supervisor.layout
    if not read_env_file(layout.credentials_file).get(GRANT_ENV, ""):
        _out(f"\n  Not authorized; run 'hermes-pubky agent login "
             f"{layout.agent_id}'.\n")
        return EXIT_AUTH

    base = _settled_base(supervisor)
    if base is None:
        return EXIT_CONFLICT
    if base.template is None:
        _out("\n  This agent did not adopt a template.\n")
        return EXIT_USAGE

    origin = base.template
    remote = PublicTemplateRemote.from_uri(origin.url)
    head = remote.read_head()
    if head is None:
        _out(f"\n  {origin.url} is no longer published. Your copies are "
             "unaffected.\n")
        return EXIT_OK
    upstream = remote.read_snapshot(
        SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256))
    if upstream.snapshot_id == origin.snapshot_id:
        _out("\n  Already up to date.\n")
        return EXIT_OK

    plan = _three_way(origin, base, upstream)
    _out(f"\n  Update from {origin.snapshot_id[:8]} to "
         f"{upstream.snapshot_id[:8]}\n" + "  " + "-" * 48)
    for path in sorted(plan.updates):
        _out(f"    update    {path}")
    for path in sorted(plan.deletions):
        _out(f"    delete    {path}")
    for path in sorted(plan.unchanged):
        _out(f"    unchanged {path}")
    for path, reason in sorted(plan.conflicts.items()):
        _out(f"    CONFLICT  {path}  ({reason})")

    if plan.conflicts:
        _out("\n  You changed these locally and upstream changed them too. "
             "No part of the update was applied; resolve them by hand, or "
             "keep your versions and re-run.\n")
        return EXIT_CONFLICT
    if not plan.updates and not plan.deletions:
        _out("\n  Nothing to apply.\n")
        return EXIT_OK
    if not _confirm("Apply this update?"):
        _out("\n  Nothing was applied.\n")
        return EXIT_OK

    _installed, portable = _copy_template(layout, supervisor.journal, remote,
                                          upstream, only=set(plan.updates))
    for path in plan.deletions:
        destination = _template_destination(layout, path)
        if destination is not None:
            destination.unlink(missing_ok=True)
        supervisor.journal.mark_deleted(path)
    return _checkpoint_after_template(
        supervisor, portable,
        TemplateOrigin(url=origin.url, snapshot_id=upstream.snapshot_id,
                       sha256=head.sha256, adopted_at=utcnow(),
                       managed_paths={p: r.sha256 for p, r in upstream.files.items()}))


def _settled_base(supervisor: "Supervisor"):
    """The local base, once local changes are sealed and the remote agrees.

    Template changes are ordinary checkpoints on top of the local state, so
    that state has to be complete and publishable before one is applied.
    """
    supervisor.seal()
    base = supervisor._base()  # noqa: SLF001 - same package
    if base is None:
        _out("\n  This agent has no saved state yet; run it once first.\n")
        return None
    remote_head = supervisor._connect().read_head()  # noqa: SLF001
    pending = supervisor.journal.active_checkpoints()
    expected = ((pending[0].parent_snapshot_id, pending[0].parent_hash) if pending
                else (base.snapshot_id, hash_bytes(base.to_bytes())))
    if remote_head is None or (remote_head.snapshot_id, remote_head.sha256) != expected:
        _out("\n  The homeserver has a newer checkpoint than this machine; run "
             f"'hermes-pubky agent sync {supervisor.layout.agent_id}' first.\n")
        return None
    return base


@dataclass
class UpdatePlan:
    updates: List[str] = field(default_factory=list)
    deletions: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    conflicts: Dict[str, str] = field(default_factory=dict)


def _three_way(origin: TemplateOrigin, current, upstream) -> UpdatePlan:
    """Compare last-adopted, personal and upstream hashes for each path.

    A path is only ever replaced when the personal copy is exactly what was
    adopted. A new upstream file never lands on top of a personal file, and an
    upstream deletion is applied only to an untouched copy.
    """
    plan = UpdatePlan()
    for path, record in upstream.files.items():
        adopted = origin.managed_paths.get(path)
        personal = current.files.get(path)
        personal_hash = personal.sha256 if personal else None

        if adopted is None:
            if personal is None:
                plan.updates.append(path)  # new upstream file
            else:
                plan.conflicts[path] = "new upstream file would replace your file"
        elif record.sha256 == adopted:
            plan.unchanged.append(path)
        elif personal_hash == adopted:
            plan.updates.append(path)  # your copy is untouched
        else:
            plan.conflicts[path] = "changed locally and upstream"

    for path, adopted in origin.managed_paths.items():
        if path in upstream.files:
            continue
        personal = current.files.get(path)
        if personal is None:
            continue
        if personal.sha256 == adopted:
            plan.deletions.append(path)
        else:
            plan.conflicts[path] = "deleted upstream, changed locally"
    return plan


def _copy_template(layout: Layout, journal: Journal,
                   remote: PublicTemplateRemote, template: TemplateSnapshot,
                   only: Optional[set] = None
                   ) -> Tuple[List[str], Optional[PortableConfig]]:
    """Fetch verified template bytes into the agent's own working copy.

    Everything is assembled in staging first; the working copy changes only
    once every download has been verified, so a failure partway leaves it
    untouched. Portable settings are returned, not written as a file: they
    belong in the checkpoint's own `config/portable.json`.
    """
    cache = ObjectCache(layout.cached_objects)
    pieces = {piece.object: piece
              for record in template.files.values() for piece in record.pieces}

    def resolve(reference: str) -> Path:
        cached = cache.path_for(reference)
        if cached.is_file():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        remote.read_object_to_path(pieces[reference], cached)
        return cached

    staging = layout.staging / "template"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    planned: List[Tuple[str, Path, Path]] = []
    portable: Optional[PortableConfig] = None
    try:
        for logical, record in sorted(template.files.items()):
            if only is not None and logical not in only:
                continue
            staged = staging / logical
            assemble(record, resolve, staged)
            if logical == PORTABLE_CONFIG_PATH:
                portable = PortableConfig.parse(staged.read_bytes())
                continue
            destination = _template_destination(layout, logical)
            if destination is not None:
                planned.append((logical, staged, destination))

        installed: List[str] = []
        with journal.transaction():
            for logical, staged, destination in planned:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
                journal.mark_dirty(logical)
                installed.append(logical)
            journal.bump_generation()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if portable is not None:
        installed.append(PORTABLE_CONFIG_PATH)
    return installed, portable


def _template_destination(layout: Layout, logical: str) -> Optional[Path]:
    if logical == "profile/SOUL.md":
        return layout.soul_file
    if logical == "workspace/AGENTS.md":
        return layout.agents_md
    if logical.startswith("profile/skills/"):
        return layout.skills_dir / logical[len("profile/skills/"):]
    return None


def _checkpoint_after_template(supervisor: "Supervisor",
                               portable: Optional[PortableConfig],
                               origin: TemplateOrigin) -> int:
    """Publish one private checkpoint recording the adopted content."""
    journal = supervisor.journal
    projection = supervisor._projection()  # noqa: SLF001 - same package
    base = projection.base()
    effective = portable if portable is not None else supervisor._current_portable()  # noqa: SLF001
    candidate = projection.capture(base, template=origin, portable=effective)
    if isinstance(candidate, NoChange):
        _out("  Nothing changed.\n")
        return EXIT_OK
    # The next run renders its configuration from the base; keep the generated
    # file in step so the following capture does not read stale settings back.
    supervisor._render_runtime(effective or PortableConfig())  # noqa: SLF001
    result = supervisor._engine().sync_with_retries()  # noqa: SLF001
    _out(f"  {result.status}: {result.detail}\n")
    del journal
    return EXIT_OK if result.ok else EXIT_INTEGRITY
