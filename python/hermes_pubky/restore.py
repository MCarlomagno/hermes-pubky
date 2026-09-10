"""`agent restore`: republish an older checkpoint as a new one.

History is never rewritten. An older snapshot is verified, then published as a
fresh checkpoint whose parent is the current head, so the change is ordinary
forward progress rather than a rollback of the head pointer. The restored
content is then installed, so the next run starts from it.
"""

from __future__ import annotations

from .journal import Upload, new_id, utcnow
from .models import Snapshot, SnapshotRef
from .objects import hash_bytes
from .paths import write_private
from .supervisor import EXIT_CONFLICT, EXIT_INTEGRITY, EXIT_OK, Supervisor


def restore_snapshot(supervisor: Supervisor, snapshot_id: str) -> int:
    layout, journal = supervisor.layout, supervisor.journal
    if journal.has_pending():
        print("\n  This machine has unsaved work; run 'hermes-pubky agent sync' "
              "first so the restore builds on a known state.\n")
        return EXIT_CONFLICT

    remote = supervisor._connect()  # noqa: SLF001 - same package
    head = remote.read_head()
    if head is None:
        print("\n  This agent has no published checkpoint to restore from.\n")
        return EXIT_INTEGRITY

    # Fetch the old snapshot with only its id known, then verify the document
    # says what we asked for.
    raw = remote._transport.snapshot_get(snapshot_id, 15.0)  # noqa: SLF001
    if raw is None:
        print(f"\n  No checkpoint {snapshot_id} for this agent.\n")
        return EXIT_INTEGRITY
    old = Snapshot.parse(bytes(raw))
    if old.snapshot_id != snapshot_id or old.agent_id != layout.agent_id:
        print("\n  That checkpoint does not describe this agent.\n")
        return EXIT_INTEGRITY

    # Republish its content as a new checkpoint on top of the current head.
    old.snapshot_id = new_id()
    old.created_at = utcnow()
    old.parent = SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256)
    republished = old.to_bytes()

    checkpoint_id = new_id()
    pending = layout.pending_checkpoint(checkpoint_id)
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / "snapshot.json"
    write_private(path, republished)

    with journal.transaction():
        checkpoint = journal.create_checkpoint(
            snapshot_path=str(path), snapshot_hash=hash_bytes(republished),
            parent_snapshot_id=head.snapshot_id, parent_hash=head.sha256,
            checkpoint_id=checkpoint_id)
        # Its objects are already on the homeserver, referenced by the older
        # snapshot; record them so the publication knows they are covered.
        journal.record_uploads(checkpoint.id, [
            Upload(checkpoint_id=checkpoint.id, object_path=reference,
                   sha256=reference.split("/", 1)[1].split(".", 1)[0], size=size,
                   local_path=str(supervisor.cache.path_for(reference)),
                   acknowledged=True)
            for reference, size in sorted(old.objects().items())])
    engine = supervisor._engine()  # noqa: SLF001
    result = engine.sync(checkpoint=journal.get_checkpoint(checkpoint.id))
    print(f"\n  {result.status}: {result.detail}")
    if not result.ok:
        return EXIT_INTEGRITY
    supervisor.install(old)
    print(f"  restored the content of {snapshot_id} as {old.snapshot_id}; the "
          "working copy now matches it\n")
    return EXIT_OK
