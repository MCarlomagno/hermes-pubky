"""Transport adapters, driven through a fake native layer.

`AgentRemote` is the injectable I/O boundary, so these tests cover the checks
it performs on the way in and out without touching a network: digest
verification, size agreement, and refusing to write a document to the wrong
place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pytest

from hermes_pubky import models as m
from hermes_pubky.objects import IntegrityError, hash_bytes, stage_file
from hermes_pubky.storage import AgentRemote, PublicTemplateRemote, TemplatePublisher

OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
RUNTIME = m.RuntimeInfo("hermes", "0.19.0", "hermes-0.19-sqlite22-v1")


class FakeTransport:
    """In-memory stand-in for the compiled transport."""

    def __init__(self, owner: str = OWNER, agent_id: str = "default",
                 template_id: str = "researcher") -> None:
        self.owner = owner
        self.agent_id = agent_id
        self.template_id = template_id
        self.uri = f"pubky://{owner}/priv/hermes.pubky.app/v2/agents/{agent_id}/head.json"
        self.capability = f"/priv/hermes.pubky.app/v2/agents/{agent_id}/:rw"
        self.capabilities = [self.capability]
        self.head: Optional[bytes] = None
        self.snapshots: Dict[str, bytes] = {}
        self.objects: Dict[str, bytes] = {}
        # Set to corrupt what a read returns, to drive the verification paths.
        self.corrupt_snapshot_readback = False

    def head_get(self, timeout_secs=10.0):
        return self.head

    def head_put(self, body, timeout_secs=15.0):
        self.head = bytes(body)

    def snapshot_get(self, snapshot_id, timeout_secs=15.0):
        raw = self.snapshots.get(snapshot_id)
        if raw is not None and self.corrupt_snapshot_readback:
            return b'{"corrupted":true}'
        return raw

    def snapshot_put(self, snapshot_id, body, timeout_secs=20.0):
        self.snapshots[snapshot_id] = bytes(body)

    def object_get(self, reference, destination, timeout_secs=30.0):
        data = self.objects[reference]
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def object_put(self, reference, source, timeout_secs=30.0):
        data = Path(source).read_bytes()
        self.objects[reference] = data
        return len(data)

    def list_snapshots(self, cursor, limit, timeout_secs=20.0):
        ids = sorted(self.snapshots)
        start = ids.index(cursor) + 1 if cursor in ids else 0
        page = ids[start:start + limit]
        nxt = page[-1] if len(page) == limit else None
        return page, nxt


def snapshot(agent_id: str = "default", snapshot_id: str = "0" * 32, **kw) -> m.Snapshot:
    return m.Snapshot(
        agent_id=agent_id, snapshot_id=snapshot_id,
        created_at="2026-09-09T18:00:00Z", device_id="f" * 32, runtime=RUNTIME, **kw)


@pytest.fixture
def remote() -> AgentRemote:
    return AgentRemote(FakeTransport())


class TestHead:
    def test_absent_head_reads_as_none(self, remote):
        assert remote.read_head() is None

    def test_head_round_trips(self, remote):
        head = m.Head(kind=m.Head.AGENT, id="default", snapshot_id="0" * 32,
                      sha256="a" * 64)
        remote.write_head(head)
        assert remote.read_head() == head

    def test_refuses_a_head_belonging_to_another_agent(self, remote):
        stray = m.Head(kind=m.Head.AGENT, id="other", snapshot_id="0" * 32,
                       sha256="a" * 64)
        with pytest.raises(m.SchemaError, match="refusing to write"):
            remote.write_head(stray)

    def test_a_corrupt_head_document_is_refused(self, remote):
        remote._transport.head = b'{"schemaVersion":2,"kind":"agent-head"}'
        with pytest.raises(m.SchemaError):
            remote.read_head()


class TestSnapshots:
    def test_put_returns_a_reference_matching_the_stored_bytes(self, remote):
        snap = snapshot()
        ref = remote.put_snapshot(snap)
        assert ref.snapshot_id == snap.snapshot_id
        assert ref.sha256 == hash_bytes(snap.to_bytes())

    def test_read_verifies_the_digest_the_head_claims(self, remote):
        snap = snapshot()
        ref = remote.put_snapshot(snap)
        assert remote.read_snapshot(ref).to_bytes() == snap.to_bytes()

        wrong = m.SnapshotRef(snapshot_id=snap.snapshot_id, sha256="b" * 64)
        with pytest.raises(IntegrityError, match="does not match the digest"):
            remote.read_snapshot(wrong)

    def test_a_missing_snapshot_referenced_by_a_head_is_an_integrity_error(self, remote):
        ref = m.SnapshotRef(snapshot_id="1" * 32, sha256="a" * 64)
        with pytest.raises(IntegrityError, match="missing but the head"):
            remote.read_snapshot(ref)

    def test_a_readback_that_differs_from_the_upload_is_refused(self, remote):
        remote._transport.corrupt_snapshot_readback = True
        with pytest.raises(IntegrityError, match="differ from what was uploaded"):
            remote.put_snapshot(snapshot())

    def test_a_snapshot_whose_id_disagrees_with_its_reference_is_refused(self, remote):
        snap = snapshot(snapshot_id="0" * 32)
        body = snap.to_bytes()
        # Store the same bytes under a different id, as a confused server might.
        remote._transport.snapshots["1" * 32] = body
        ref = m.SnapshotRef(snapshot_id="1" * 32, sha256=hash_bytes(body))
        with pytest.raises(IntegrityError, match="head says"):
            remote.read_snapshot(ref)

    def test_listing_pages_and_validates_ids(self, remote):
        for index in range(5):
            remote.put_snapshot(snapshot(snapshot_id=f"{index:032x}"))
        seen, cursor = [], None
        for _ in range(10):
            ids, cursor = remote.list_snapshots(cursor, limit=2)
            seen.extend(ids)
            if cursor is None:
                break
        assert sorted(seen) == [f"{i:032x}" for i in range(5)]

    def test_a_malformed_id_from_a_listing_is_refused(self, remote):
        remote._transport.snapshots["not-a-hex-id"] = b"{}"
        with pytest.raises(m.SchemaError):
            remote.list_snapshots(limit=10)


class TestObjects:
    def _piece(self, tmp_path: Path, data: bytes):
        source = tmp_path / "src"
        source.write_bytes(data)
        staged = stage_file(source, "workspace/f.bin", tmp_path / "objects")
        return staged.record.pieces[0], next(iter(staged.objects.values()))

    def test_object_round_trips(self, remote, tmp_path):
        piece, staged = self._piece(tmp_path, b"payload")
        assert remote.put_object_from_path(piece, staged) == 7
        out = tmp_path / "out" / "f.bin"
        assert remote.read_object_to_path(piece, out) == 7
        assert out.read_bytes() == b"payload"

    def test_uploading_bytes_that_disagree_with_the_reference_is_refused(
            self, remote, tmp_path):
        piece, _staged = self._piece(tmp_path, b"payload")
        lying = tmp_path / "lying"
        lying.write_bytes(b"different")
        with pytest.raises(IntegrityError, match="does not match"):
            remote.put_object_from_path(piece, lying)

    def test_a_download_of_the_wrong_length_is_refused(self, remote, tmp_path):
        piece, staged = self._piece(tmp_path, b"payload")
        remote.put_object_from_path(piece, staged)
        remote._transport.objects[piece.object] = b"short"
        with pytest.raises(IntegrityError, match="manifest says"):
            remote.read_object_to_path(piece, tmp_path / "out" / "f.bin")


class TestPublicTemplate:
    def test_an_agent_head_is_not_accepted_as_a_template_head(self):
        transport = FakeTransport()
        agent_head = m.Head(kind=m.Head.AGENT, id="default", snapshot_id="0" * 32,
                            sha256="a" * 64)
        transport.head = agent_head.to_bytes()
        with pytest.raises(m.SchemaError, match="kind"):
            PublicTemplateRemote(transport).read_head()

    def test_a_template_snapshot_is_verified_against_its_reference(self):
        transport = FakeTransport()
        snap = m.TemplateSnapshot(
            template_id="researcher", snapshot_id="0" * 32,
            created_at="2026-09-09T18:00:00Z", runtime=RUNTIME)
        transport.snapshots[snap.snapshot_id] = snap.to_bytes()
        remote = PublicTemplateRemote(transport)
        ref = m.SnapshotRef(snapshot_id=snap.snapshot_id,
                            sha256=hash_bytes(snap.to_bytes()))
        assert remote.read_snapshot(ref).template_id == "researcher"

        wrong = m.SnapshotRef(snapshot_id=snap.snapshot_id, sha256="b" * 64)
        with pytest.raises(IntegrityError):
            remote.read_snapshot(wrong)


class TestTemplatePublisher:
    def test_refuses_to_publish_a_private_agent_head(self):
        publisher = TemplatePublisher(FakeTransport())
        agent_head = m.Head(kind=m.Head.AGENT, id="default", snapshot_id="0" * 32,
                            sha256="a" * 64)
        with pytest.raises(m.SchemaError, match="non-template head"):
            publisher.write_head(agent_head)

    def test_publishes_a_template_head(self):
        transport = FakeTransport()
        publisher = TemplatePublisher(transport)
        head = m.Head(kind=m.Head.TEMPLATE, id="researcher", snapshot_id="0" * 32,
                      sha256="a" * 64)
        publisher.write_head(head)
        assert b"template-head" in transport.head
