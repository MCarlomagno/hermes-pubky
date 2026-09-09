"""`agent restore`: republish an older checkpoint as a new one.

History is never rewritten. An older snapshot is verified, then published as a
fresh checkpoint whose parent is the current head, so the change is ordinary
forward progress rather than a rollback of the head pointer.
"""

from __future__ import annotations

from .journal import Journal, new_id, utcnow
from .models import SnapshotRef
from .objects import ObjectCache, hash_bytes
from .paths import GRANT_ENV, Layout, read_env_file, write_private
from .storage import AgentRemote
from .supervisor import EXIT_AUTH, EXIT_INTEGRITY, EXIT_OK
from .sync import SyncEngine


def restore_snapshot(layout: Layout, snapshot_id: str, *, network: str) -> int:
    grant = read_env_file(layout.credentials_file).get(GRANT_ENV, "")
    if not grant:
        print(f"\n  Not authorized; run 'hermes-pubky agent login "
              f"{layout.agent_id}'.\n")
        return EXIT_AUTH

    layout.ensure()
    remote = AgentRemote.connect(grant, layout.owner, layout.agent_id)
    journal = Journal(layout.journal_file)
    try:
        head = remote.read_head()
        if head is None:
            print("\n  This agent has no published checkpoint to restore from.\n")
            return EXIT_INTEGRITY

        # Fetch the old snapshot with only its id known, then verify the
        # document says what we asked for.
        raw = remote._transport.snapshot_get(snapshot_id, 15.0)  # noqa: SLF001
        if raw is None:
            print(f"\n  No checkpoint {snapshot_id} for this agent.\n")
            return EXIT_INTEGRITY
        body = bytes(raw)
        from .models import Snapshot

        old = Snapshot.parse(body)
        if old.snapshot_id != snapshot_id or old.agent_id != layout.agent_id:
            print("\n  That checkpoint does not describe this agent.\n")
            return EXIT_INTEGRITY

        # Republish its content as a new checkpoint on top of the current head.
        old.snapshot_id = new_id()
        old.created_at = utcnow()
        old.parent = SnapshotRef(snapshot_id=head.snapshot_id, sha256=head.sha256)
        republished = old.to_bytes()

        pending = layout.pending_checkpoint(new_id())
        pending.mkdir(parents=True, exist_ok=True)
        path = pending / "snapshot.json"
        write_private(path, republished)

        checkpoint = journal.create_checkpoint(
            snapshot_path=str(path), snapshot_hash=hash_bytes(republished),
            parent_snapshot_id=head.snapshot_id, parent_hash=head.sha256)
        # Its objects are already on the homeserver; nothing to upload.
        engine = SyncEngine(journal, remote, ObjectCache(layout.cached_objects),
                            recovery_dir=layout.recovery)
        result = engine.sync(checkpoint=journal.get_checkpoint(checkpoint.id))
        print(f"\n  {result.status}: {result.detail}")
        print(f"  restored the content of {snapshot_id} as {old.snapshot_id}\n")
        return EXIT_OK if result.ok else EXIT_INTEGRITY
    finally:
        journal.close()
