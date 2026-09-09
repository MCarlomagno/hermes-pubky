"""Schema validation.

Both documents cross a trust boundary, so the tests here are mostly about
what gets *rejected*.
"""

from __future__ import annotations

import json

import pytest

from hermes_pubky.schema import (
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    BaseContext,
    BaseContextRef,
    Profile,
    SchemaError,
)


def ctx_bytes(**overrides) -> bytes:
    doc = {
        "schemaVersion": 1,
        "id": "researcher",
        "name": "Researcher",
        "description": "Research-oriented agent instructions",
        "instructions": "Be rigorous. Cite sources.",
    }
    doc.update(overrides)
    return json.dumps(doc).encode("utf-8")


def profile_bytes(**overrides) -> bytes:
    doc = {
        "schemaVersion": 1,
        "profileId": "default",
        "baseContext": None,
        "user": ["prefers concise answers"],
        "memory": ["the deploy script lives in scripts/deploy.sh"],
        "revision": 3,
        "updatedAt": "2026-09-09T10:00:00Z",
    }
    doc.update(overrides)
    return json.dumps(doc).encode("utf-8")


class TestBaseContext:
    def test_parses_a_well_formed_document(self):
        ctx = BaseContext.parse_bytes(ctx_bytes())
        assert ctx.id == "researcher"
        assert ctx.instructions == "Be rigorous. Cite sources."

    def test_name_and_description_are_optional(self):
        raw = json.dumps(
            {"schemaVersion": 1, "id": "x", "instructions": "do things"}
        ).encode()
        ctx = BaseContext.parse_bytes(raw)
        assert ctx.name == "" and ctx.description == ""

    @pytest.mark.parametrize("bad", [b"", b"not json", b"[]", b'"a string"', b"null"])
    def test_rejects_non_object_documents(self, bad):
        with pytest.raises(SchemaError):
            BaseContext.parse_bytes(bad)

    def test_rejects_invalid_utf8(self):
        with pytest.raises(SchemaError, match="UTF-8"):
            BaseContext.parse_bytes(b"\xff\xfe{}")

    def test_rejects_a_future_schema_version(self):
        with pytest.raises(SchemaError, match="schemaVersion 2"):
            BaseContext.parse_bytes(ctx_bytes(schemaVersion=2))

    def test_rejects_a_missing_schema_version(self):
        raw = json.dumps({"id": "x", "instructions": "y"}).encode()
        with pytest.raises(SchemaError, match="schemaVersion"):
            BaseContext.parse_bytes(raw)

    def test_rejects_a_boolean_masquerading_as_a_version(self):
        # bool is an int subclass in Python; the check must not be fooled.
        with pytest.raises(SchemaError):
            BaseContext.parse_bytes(ctx_bytes(schemaVersion=True))

    def test_rejects_missing_required_fields(self):
        raw = json.dumps({"schemaVersion": 1, "id": "x"}).encode()
        with pytest.raises(SchemaError, match="instructions"):
            BaseContext.parse_bytes(raw)

    def test_rejects_wrong_field_types(self):
        with pytest.raises(SchemaError, match="must be a string"):
            BaseContext.parse_bytes(ctx_bytes(instructions=["a", "list"]))

    def test_rejects_oversized_instructions(self):
        with pytest.raises(SchemaError, match="over the"):
            BaseContext.parse_bytes(ctx_bytes(instructions="x" * (MAX_INSTRUCTIONS_CHARS + 1)))

    def test_round_trips(self):
        ctx = BaseContext.parse_bytes(ctx_bytes())
        assert BaseContext.parse(ctx.to_dict()) == ctx


class TestBaseContextRef:
    def test_parses_a_valid_ref(self):
        ref = BaseContextRef.parse({"url": "pubky://k/pub/a.json", "sha256": "ab" * 32})
        assert ref is not None and ref.sha256 == "ab" * 32

    def test_none_is_allowed(self):
        assert BaseContextRef.parse(None) is None

    @pytest.mark.parametrize(
        "digest", ["", "abc", "z" * 64, "AB" * 32 + "00", "g" * 64]
    )
    def test_rejects_a_malformed_hash(self, digest):
        with pytest.raises(SchemaError):
            BaseContextRef.parse({"url": "pubky://k/pub/a.json", "sha256": digest})

    def test_normalizes_hash_case(self):
        ref = BaseContextRef.parse({"url": "pubky://k/pub/a.json", "sha256": "AB" * 32})
        assert ref is not None and ref.sha256 == "ab" * 32


class TestProfile:
    def test_parses_a_well_formed_profile(self):
        profile = Profile.parse_bytes(profile_bytes())
        assert profile.revision == 3
        assert profile.user == ["prefers concise answers"]

    def test_rejects_a_negative_revision(self):
        with pytest.raises(SchemaError, match="revision"):
            Profile.parse_bytes(profile_bytes(revision=-1))

    def test_rejects_a_non_integer_revision(self):
        with pytest.raises(SchemaError, match="revision"):
            Profile.parse_bytes(profile_bytes(revision="3"))

    def test_rejects_non_string_entries(self):
        with pytest.raises(SchemaError, match="must be a string"):
            Profile.parse_bytes(profile_bytes(user=[{"nested": "object"}]))

    def test_rejects_too_many_entries(self):
        with pytest.raises(SchemaError, match="over the"):
            Profile.parse_bytes(profile_bytes(memory=["e"] * (MAX_ENTRIES + 1)))

    def test_rejects_an_oversized_entry(self):
        with pytest.raises(SchemaError, match="over the"):
            Profile.parse_bytes(profile_bytes(user=["x" * (MAX_ENTRY_CHARS + 1)]))

    def test_drops_blank_entries_but_keeps_the_rest(self):
        profile = Profile.parse_bytes(profile_bytes(user=["  ", "real fact", "\n\t"]))
        assert profile.user == ["real fact"]

    def test_strips_surrounding_whitespace(self):
        profile = Profile.parse_bytes(profile_bytes(user=["  padded  "]))
        assert profile.user == ["padded"]

    def test_missing_entry_lists_default_to_empty(self):
        raw = json.dumps({"schemaVersion": 1, "profileId": "default", "revision": 0}).encode()
        profile = Profile.parse_bytes(raw)
        assert profile.user == [] and profile.memory == []

    def test_serialization_is_deterministic(self):
        profile = Profile.parse_bytes(profile_bytes())
        assert profile.to_bytes() == Profile.parse_bytes(profile.to_bytes()).to_bytes()

    def test_round_trips_through_bytes(self):
        original = Profile.parse_bytes(
            profile_bytes(baseContext={"url": "pubky://k/pub/a.json", "sha256": "cd" * 32})
        )
        restored = Profile.parse_bytes(original.to_bytes())
        assert restored.base_context == original.base_context
        assert restored.user == original.user
        assert restored.revision == original.revision

    def test_entries_rejects_an_unknown_target(self):
        profile = Profile.parse_bytes(profile_bytes())
        with pytest.raises(SchemaError):
            profile.entries("secrets")
