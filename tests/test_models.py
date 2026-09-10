"""Manifest parsing: the trust boundary for every document off a homeserver.

Weighted towards rejection, because a permissive parser here is what turns a
hostile manifest into a path traversal or an unbounded allocation.
"""

from __future__ import annotations

import json

import pytest

from hermes_pubky import models as m

DIGEST = "a" * 64
OTHER = "b" * 64
ID32 = "0" * 32
STAMP = "2026-09-09T18:00:00Z"
RUNTIME = m.RuntimeInfo("hermes", "0.19.0", "hermes-0.19-sqlite22-v1")


def piece(digest: str = DIGEST, size: int = 3, ext: str = "md") -> dict:
    return {"object": f"objects/{digest}.{ext}", "sha256": digest, "size": size}


def snapshot_dict(**overrides) -> dict:
    doc = {
        "schemaVersion": 2,
        "kind": "agent-snapshot",
        "agentId": "default",
        "name": "My agent",
        "snapshotId": ID32,
        "parent": None,
        "createdAt": STAMP,
        "deviceId": "f" * 32,
        "runtime": RUNTIME.to_dict(),
        "lastSessionId": None,
        "template": None,
        "files": {},
    }
    doc.update(overrides)
    return doc


def as_bytes(doc: dict) -> bytes:
    return (json.dumps(doc) + "\n").encode("utf-8")


class TestCanonicalJson:
    def test_dumps_is_sorted_compact_and_newline_terminated(self):
        out = m.dumps({"b": 1, "a": {"d": 2, "c": 3}})
        assert out == b'{"a":{"c":3,"d":2},"b":1}\n'

    def test_dumps_refuses_non_finite_numbers(self):
        with pytest.raises(ValueError):
            m.dumps({"x": float("inf")})

    def test_loads_rejects_duplicate_keys(self):
        with pytest.raises(m.SchemaError, match="duplicate"):
            m.loads(b'{"a":1,"a":2}', "doc", max_bytes=100)

    def test_loads_rejects_non_finite_numbers(self):
        with pytest.raises(m.SchemaError, match="non-finite"):
            m.loads(b'{"x":Infinity}', "doc", max_bytes=100)

    def test_loads_enforces_the_cap_before_parsing(self):
        with pytest.raises(m.SchemaError, match="over the"):
            m.loads(b"{}" + b" " * 200, "doc", max_bytes=10)

    @pytest.mark.parametrize("bad", [b"", b"not json", b"\xff\xfe{}"])
    def test_loads_rejects_unusable_bytes(self, bad):
        with pytest.raises(m.SchemaError):
            m.loads(bad, "doc", max_bytes=1000)


class TestLogicalPaths:
    @pytest.mark.parametrize("good", [
        "profile/SOUL.md", "profile/memories/USER.md", "workspace/a/b/c.pdf",
        "config/portable.json", "profile/skills/my-skill/SKILL.md",
    ])
    def test_accepts_ordinary_paths(self, good):
        assert m.require_logical_path(good) == good

    @pytest.mark.parametrize("bad", [
        "", "/absolute", "a/../b", "../escape", "a//b", "a/./b", "a\\b",
        "trailing/", "a/ padded/b", "with\x00null", "with\nnewline", "a/b/.",
    ])
    def test_rejects_traversal_and_unusable_names(self, bad):
        with pytest.raises(m.SchemaError):
            m.require_logical_path(bad)

    def test_rejects_a_path_over_the_byte_limit(self):
        with pytest.raises(m.SchemaError, match="UTF-8 bytes"):
            m.require_logical_path("w/" + "x" * 1100)

    def test_rejects_a_non_nfc_remote_name(self):
        # "e" + combining acute, which NFC would fold to a single code point.
        with pytest.raises(m.SchemaError, match="NFC"):
            m.require_logical_path("workspace/café.md")

    def test_normalizes_a_locally_created_name(self):
        assert m.normalize_new_path("workspace/café.md") == "workspace/café.md"

    def test_case_only_differences_are_a_collision(self):
        with pytest.raises(m.SchemaError, match="collide"):
            m.reject_colliding_paths(["workspace/Report.md", "workspace/report.md"])

    def test_normalization_only_differences_are_a_collision(self):
        with pytest.raises(m.SchemaError, match="collide"):
            m.reject_colliding_paths(["workspace/café.md", "workspace/café.md"])


    def test_core_markdown_gets_the_tighter_cap(self):
        assert m.max_bytes_for("profile/SOUL.md") == m.MAX_CORE_MARKDOWN_BYTES
        assert m.max_bytes_for("workspace/big.pdf") == m.MAX_WORKSPACE_FILE_BYTES
        assert m.max_bytes_for(m.DATABASE_PATH) == m.MAX_DATABASE_BYTES


class TestObjectRefs:
    def test_extension_keeps_markdown_and_json_readable_on_the_homeserver(self):
        assert m.object_extension("profile/SOUL.md", chunked=False) == "md"
        assert m.object_extension("config/portable.json", chunked=False) == "json"
        assert m.object_extension("workspace/a.pdf", chunked=False) == "bin"
        assert m.object_extension("profile/SOUL.md", chunked=True) == "chunk"

    @pytest.mark.parametrize("ref", [
        "objects/../escape.md", "/objects/x.md", "objects/short.md",
        "https://elsewhere/objects/" + DIGEST + ".md", "objects/" + DIGEST + ".exe",
    ])
    def test_rejects_object_references_that_are_not_local_digests(self, ref):
        with pytest.raises(m.SchemaError):
            m.Piece.parse({"object": ref, "sha256": DIGEST, "size": 3}, "p")

    def test_rejects_a_piece_whose_name_disagrees_with_its_hash(self):
        with pytest.raises(m.SchemaError, match="names digest"):
            m.Piece.parse({"object": f"objects/{OTHER}.md", "sha256": DIGEST, "size": 3}, "p")

    def test_rejects_a_piece_over_the_object_cap(self):
        with pytest.raises(m.SchemaError):
            m.Piece.parse(
                {"object": f"objects/{DIGEST}.chunk", "sha256": DIGEST,
                 "size": m.OBJECT_CHUNK_BYTES + 1}, "p")


class TestFileRecords:
    def test_pieces_must_sum_to_the_file_size(self):
        with pytest.raises(m.SchemaError, match="sum to"):
            m.FileRecord.parse(
                {"sha256": DIGEST, "size": 10, "pieces": [piece(size=3)]},
                "profile/SOUL.md")

    def test_a_single_piece_hash_must_equal_the_file_hash(self):
        with pytest.raises(m.SchemaError, match="single piece"):
            m.FileRecord.parse(
                {"sha256": OTHER, "size": 3, "pieces": [piece()]}, "profile/SOUL.md")

    def test_an_empty_file_has_the_empty_hash_and_no_pieces(self):
        record = m.FileRecord.parse(
            {"sha256": m.EMPTY_SHA256, "size": 0, "pieces": []}, "profile/SOUL.md")
        assert record.size == 0 and record.pieces == []

    def test_an_empty_file_may_not_list_pieces(self):
        with pytest.raises(m.SchemaError, match="empty but lists"):
            m.FileRecord.parse(
                {"sha256": m.EMPTY_SHA256, "size": 0, "pieces": [piece()]},
                "profile/SOUL.md")

    def test_an_empty_file_must_use_the_empty_hash(self):
        with pytest.raises(m.SchemaError, match="empty hash"):
            m.FileRecord.parse(
                {"sha256": DIGEST, "size": 0, "pieces": []}, "profile/SOUL.md")

    def test_a_zero_length_piece_is_rejected(self):
        with pytest.raises(m.SchemaError, match="zero-length"):
            m.FileRecord.parse(
                {"sha256": DIGEST, "size": 3,
                 "pieces": [piece(size=3), piece(digest=OTHER, size=0)]},
                "profile/SOUL.md")

    def test_a_core_markdown_file_over_its_cap_is_rejected(self):
        with pytest.raises(m.SchemaError):
            m.FileRecord.parse(
                {"sha256": DIGEST, "size": m.MAX_CORE_MARKDOWN_BYTES + 1, "pieces": []},
                "profile/SOUL.md")

    def test_unsupported_keys_are_rejected(self):
        with pytest.raises(m.SchemaError, match="unsupported keys"):
            m.FileRecord.parse(
                {"sha256": m.EMPTY_SHA256, "size": 0, "pieces": [], "symlink": "/etc/passwd"},
                "profile/SOUL.md")


class TestHead:
    def test_round_trips(self):
        head = m.Head(kind=m.Head.AGENT, id="default", snapshot_id=ID32, sha256=DIGEST)
        assert m.Head.parse(head.to_bytes()) == head

    def test_a_template_head_uses_its_own_kind_and_id_field(self):
        head = m.Head(kind=m.Head.TEMPLATE, id="researcher", snapshot_id=ID32,
                      sha256=DIGEST)
        raw = head.to_bytes()
        assert b'"templateId"' in raw and b'"template-head"' in raw
        assert m.Head.parse(raw, template=True) == head

    def test_an_agent_head_is_not_accepted_as_a_template_head(self):
        agent = m.Head(kind=m.Head.AGENT, id="default", snapshot_id=ID32, sha256=DIGEST)
        with pytest.raises(m.SchemaError, match="kind"):
            m.Head.parse(agent.to_bytes(), template=True)

    @pytest.mark.parametrize("agent_id", ["", "Default", "-lead", "a" * 65, "has space",
                                          "../escape", "a/b"])
    def test_rejects_hostile_agent_ids(self, agent_id):
        with pytest.raises(m.SchemaError):
            m.Head.parse(as_bytes({
                "schemaVersion": 2, "kind": "agent-head", "agentId": agent_id,
                "snapshotId": ID32, "sha256": DIGEST}))

    def test_rejects_a_v1_document(self):
        with pytest.raises(m.SchemaError, match="no v1 reader"):
            m.Head.parse(as_bytes({
                "schemaVersion": 1, "kind": "agent-head", "agentId": "default",
                "snapshotId": ID32, "sha256": DIGEST}))


class TestSnapshot:
    def test_round_trips_with_every_optional_field_set(self):
        snap = m.Snapshot(
            agent_id="default", snapshot_id=ID32, created_at=STAMP,
            device_id="f" * 32, runtime=RUNTIME, name="My agent",
            parent=m.SnapshotRef(snapshot_id="1" * 32, sha256=OTHER),
            last_session_id="session-1",
            template=m.TemplateOrigin(
                url="pubky://k/pub/x/head.json", snapshot_id="2" * 32,
                sha256=DIGEST, adopted_at=STAMP,
                managed_paths={"profile/SOUL.md": DIGEST}),
            files={"profile/SOUL.md": m.FileRecord(
                sha256=DIGEST, size=3, pieces=[m.Piece(f"objects/{DIGEST}.md", DIGEST, 3)])},
        )
        again = m.Snapshot.parse(snap.to_bytes())
        assert again.to_bytes() == snap.to_bytes()
        assert again.template and again.template.managed_paths == {"profile/SOUL.md": DIGEST}

    def test_objects_lists_every_referenced_object(self):
        snap = m.Snapshot.parse(as_bytes(snapshot_dict(files={
            "profile/SOUL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
            "workspace/copy.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
        })))
        assert snap.objects() == {f"objects/{DIGEST}.md": 3}

    def test_the_same_object_may_not_be_described_two_different_ways(self):
        with pytest.raises(m.SchemaError, match="conflicting sizes"):
            m.Snapshot.parse(as_bytes(snapshot_dict(files={
                "a.md": {"sha256": DIGEST, "size": 3, "pieces": [piece(size=3)]},
                "b.md": {"sha256": DIGEST, "size": 4, "pieces": [piece(size=4)]},
            })))

    def test_rejects_unsupported_top_level_keys(self):
        with pytest.raises(m.SchemaError, match="unsupported keys"):
            m.Snapshot.parse(as_bytes(snapshot_dict(secrets={"api_key": "x"})))

    def test_rejects_a_traversing_file_path(self):
        with pytest.raises(m.SchemaError):
            m.Snapshot.parse(as_bytes(snapshot_dict(files={
                "../../etc/passwd": {"sha256": m.EMPTY_SHA256, "size": 0, "pieces": []}})))

    def test_rejects_an_empty_last_session_id(self):
        with pytest.raises(m.SchemaError, match="use null"):
            m.Snapshot.parse(as_bytes(snapshot_dict(lastSessionId="")))

    @pytest.mark.parametrize("stamp", [
        "yesterday", "2026-09-09", "", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z",
        "2026-09-09T25:00:00Z",
    ])
    def test_rejects_a_malformed_timestamp(self, stamp):
        with pytest.raises(m.SchemaError):
            m.Snapshot.parse(as_bytes(snapshot_dict(createdAt=stamp)))

    def test_rejects_too_many_files(self):
        files = {f"workspace/f{i}.md": {"sha256": m.EMPTY_SHA256, "size": 0, "pieces": []}
                 for i in range(m.MAX_LOGICAL_FILES + 1)}
        with pytest.raises(m.SchemaError, match="over"):
            m.Snapshot.parse(as_bytes(snapshot_dict(files=files)))


class TestTemplateSnapshot:
    def _template(self, files: dict) -> bytes:
        return as_bytes({
            "schemaVersion": 2, "kind": "template-snapshot", "templateId": "researcher",
            "name": "Researcher", "description": "d", "snapshotId": ID32,
            "parent": None, "createdAt": STAMP, "runtime": RUNTIME.to_dict(),
            "files": files,
        })

    def test_accepts_only_reusable_paths(self):
        snap = m.TemplateSnapshot.parse(self._template({
            "profile/SOUL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
            "profile/skills/s/SKILL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
            "workspace/AGENTS.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
            "config/portable.json": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
        }))
        assert len(snap.files) == 4

    @pytest.mark.parametrize("path", [
        "profile/memories/USER.md", "profile/memories/MEMORY.md",
        "conversations/state.sqlite3", "workspace/private-report.pdf",
    ])
    def test_refuses_personal_state(self, path):
        with pytest.raises(m.SchemaError, match="may not contain"):
            m.TemplateSnapshot.parse(self._template({
                path: {"sha256": DIGEST, "size": 3, "pieces": [piece()]}}))

    def test_refuses_an_agent_snapshot_wearing_a_template_kind(self):
        doc = snapshot_dict()
        doc["kind"] = "template-snapshot"
        doc["templateId"] = "researcher"
        with pytest.raises(m.SchemaError, match="unsupported keys"):
            m.TemplateSnapshot.parse(as_bytes(doc))

    def test_review_digest_covers_content_and_metadata(self):
        one = m.TemplateSnapshot.parse(self._template({
            "profile/SOUL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]}}))
        two = m.TemplateSnapshot.parse(self._template({
            "profile/SOUL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]},
            "workspace/AGENTS.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]}}))
        assert one.review_digest() != two.review_digest()
        assert len(one.review_digest()) == 64

    def test_review_digest_is_stable_across_parses(self):
        raw = self._template({
            "profile/SOUL.md": {"sha256": DIGEST, "size": 3, "pieces": [piece()]}})
        assert (m.TemplateSnapshot.parse(raw).review_digest()
                == m.TemplateSnapshot.parse(raw).review_digest())


class TestPortableConfig:
    def test_defaults_match_the_pinned_hermes_defaults(self):
        config = m.PortableConfig()
        assert config.toolsets == ["hermes-cli"]
        assert (config.max_turns, config.memory_char_limit, config.user_char_limit) \
            == (90, 2200, 1375)

    def test_round_trips(self):
        config = m.PortableConfig(model="anthropic/claude", toolsets=["hermes-cli", "web"])
        assert m.PortableConfig.parse(config.to_bytes()) == config

    @pytest.mark.parametrize("key", ["providers", "mcp_servers", "security", "terminal"])
    def test_refuses_keys_that_could_carry_credentials_or_endpoints(self, key):
        with pytest.raises(m.SchemaError, match="unsupported keys"):
            m.PortableConfig.parse(as_bytes({"schemaVersion": 2, key: {}}))

    @pytest.mark.parametrize("value", [0, 1001, True, "90", 1.5])
    def test_rejects_an_out_of_range_or_mistyped_max_turns(self, value):
        with pytest.raises(m.SchemaError):
            m.PortableConfig.parse(
                as_bytes({"schemaVersion": 2, "agent": {"max_turns": value}}))

    @pytest.mark.parametrize("value", [0, 64001, True])
    def test_rejects_an_out_of_range_memory_limit(self, value):
        with pytest.raises(m.SchemaError):
            m.PortableConfig.parse(
                as_bytes({"schemaVersion": 2, "memory": {"memory_char_limit": value}}))

    @pytest.mark.parametrize("value", [1, 0, "yes", None])
    def test_rejects_a_non_boolean_flag(self, value):
        with pytest.raises(m.SchemaError):
            m.PortableConfig.parse(
                as_bytes({"schemaVersion": 2, "memory": {"memory_enabled": value}}))

    def test_rejects_too_many_toolsets(self):
        with pytest.raises(m.SchemaError, match="more than"):
            m.PortableConfig.parse(as_bytes({
                "schemaVersion": 2, "toolsets": [f"t{i}" for i in range(m.MAX_TOOLSETS + 1)]}))

    def test_rejects_an_overlong_model_identifier(self):
        with pytest.raises(m.SchemaError):
            m.PortableConfig.parse(
                as_bytes({"schemaVersion": 2, "model": "x" * (m.MAX_MODEL_CHARS + 1)}))
