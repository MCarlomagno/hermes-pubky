"""Memory-operation normalization, queueing, and replay."""

from __future__ import annotations

import json

import pytest

from hermes_pubky.outbox import Operation, Outbox, apply_operation, apply_operations
from hermes_pubky.schema import MAX_ENTRIES, MAX_ENTRY_CHARS, Profile


def profile(**kw) -> Profile:
    return Profile(profile_id="default", **kw)


class TestOperationBuild:
    def test_builds_an_add(self):
        op = Operation.build("add", "memory", "  a fact  ")
        assert op is not None
        assert (op.action, op.target, op.content) == ("add", "memory", "a fact")

    def test_normalizes_case_and_whitespace_in_action_and_target(self):
        op = Operation.build(" ADD ", " User ", "fact")
        assert op is not None and (op.action, op.target) == ("add", "user")

    @pytest.mark.parametrize("action", ["", "delete", "append", "search", None])
    def test_ignores_unmirrored_actions(self, action):
        assert Operation.build(action, "memory", "x") is None

    @pytest.mark.parametrize("target", ["", "skills", "soul", None])
    def test_ignores_unknown_targets(self, target):
        assert Operation.build("add", target, "x") is None

    def test_ignores_an_empty_add(self):
        assert Operation.build("add", "memory", "   ") is None

    def test_takes_old_text_from_metadata(self):
        op = Operation.build("replace", "user", "new", {"old_text": "old"})
        assert op is not None and op.old_text == "old"

    def test_remove_falls_back_to_content_when_old_text_is_absent(self):
        op = Operation.build("remove", "memory", "gone")
        assert op is not None and op.old_text == "gone"

    def test_remove_with_neither_field_is_ignored(self):
        assert Operation.build("remove", "memory", "", {}) is None

    def test_ignores_oversized_content(self):
        assert Operation.build("add", "memory", "x" * (MAX_ENTRY_CHARS + 1)) is None

    def test_serialization_round_trips(self):
        op = Operation.build("replace", "user", "new", {"old_text": "old"})
        assert op is not None
        restored = Operation.from_dict(op.to_dict())
        assert restored is not None
        assert restored.to_dict() == op.to_dict()

    def test_from_dict_rejects_junk(self):
        assert Operation.from_dict({"action": "nope"}) is None
        assert Operation.from_dict("not a dict") is None  # type: ignore[arg-type]


class TestApplyOperation:
    def test_add_appends(self):
        p = profile()
        assert apply_operation(p, Operation("add", "memory", "one")) is True
        assert p.memory == ["one"]

    def test_add_is_idempotent(self):
        p = profile(memory=["one"])
        assert apply_operation(p, Operation("add", "memory", "one")) is False
        assert p.memory == ["one"]

    def test_add_evicts_the_oldest_entry_at_the_cap(self):
        p = profile(memory=[f"e{i}" for i in range(MAX_ENTRIES)])
        assert apply_operation(p, Operation("add", "memory", "newest")) is True
        assert len(p.memory) == MAX_ENTRIES
        assert p.memory[-1] == "newest"
        assert "e0" not in p.memory

    def test_remove_deletes_a_matching_entry(self):
        p = profile(user=["a", "b"])
        assert apply_operation(p, Operation("remove", "user", "b", old_text="b")) is True
        assert p.user == ["a"]

    def test_remove_of_an_absent_entry_is_a_no_op(self):
        p = profile(user=["a"])
        assert apply_operation(p, Operation("remove", "user", "z", old_text="z")) is False
        assert p.user == ["a"]

    def test_replace_swaps_in_place(self):
        p = profile(memory=["a", "old", "c"])
        assert apply_operation(p, Operation("replace", "memory", "new", old_text="old")) is True
        assert p.memory == ["a", "new", "c"]

    def test_replace_of_an_unknown_entry_records_the_new_value(self):
        # The old entry may predate the plugin; losing the new value would be
        # worse than appending it.
        p = profile(memory=["a"])
        assert apply_operation(p, Operation("replace", "memory", "new", old_text="???")) is True
        assert p.memory == ["a", "new"]

    def test_replace_to_an_identical_value_is_a_no_op(self):
        p = profile(memory=["same"])
        assert apply_operation(p, Operation("replace", "memory", "same", old_text="same")) is False

    def test_targets_are_independent(self):
        p = profile()
        apply_operation(p, Operation("add", "user", "u"))
        apply_operation(p, Operation("add", "memory", "m"))
        assert p.user == ["u"] and p.memory == ["m"]

    def test_apply_operations_counts_only_real_changes(self):
        p = profile(memory=["dup"])
        changed = apply_operations(p, [
            Operation("add", "memory", "dup"),     # no-op
            Operation("add", "memory", "fresh"),   # change
            Operation("remove", "memory", "gone", old_text="gone"),  # no-op
        ])
        assert changed == 1
        assert p.memory == ["dup", "fresh"]


class TestOutbox:
    def test_append_and_load_preserve_order(self, outbox: Outbox):
        for i in range(3):
            outbox.append(Operation("add", "memory", f"entry {i}"))
        assert [op.content for op in outbox.load()] == ["entry 0", "entry 1", "entry 2"]

    def test_load_of_a_missing_file_is_empty(self, outbox: Outbox):
        assert outbox.load() == []
        assert outbox.count() == 0

    def test_survives_a_simulated_restart(self, outbox: Outbox):
        outbox.append(Operation("add", "user", "durable"))
        reopened = Outbox(outbox.path)
        assert [op.content for op in reopened.load()] == ["durable"]

    def test_skips_corrupt_lines_but_keeps_the_rest(self, outbox: Outbox):
        outbox.append(Operation("add", "memory", "good one"))
        with open(outbox.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"action": "bogus"}) + "\n")
            fh.write("\n")
        outbox.append(Operation("add", "memory", "good two"))
        assert [op.content for op in outbox.load()] == ["good one", "good two"]

    def test_clear_empties_the_queue(self, outbox: Outbox):
        outbox.append(Operation("add", "memory", "x"))
        outbox.clear()
        assert outbox.load() == []
        outbox.clear()  # idempotent

    def test_replace_rewrites_the_queue(self, outbox: Outbox):
        for i in range(3):
            outbox.append(Operation("add", "memory", f"e{i}"))
        outbox.replace([Operation("add", "memory", "only")])
        assert [op.content for op in outbox.load()] == ["only"]

    def test_replace_with_nothing_clears_the_file(self, outbox: Outbox):
        outbox.append(Operation("add", "memory", "x"))
        outbox.replace([])
        assert not outbox.path.exists()

    def test_refuses_to_grow_past_the_cap(self, outbox: Outbox, monkeypatch):
        monkeypatch.setattr("hermes_pubky.outbox.MAX_QUEUED_OPERATIONS", 3)
        assert all(outbox.append(Operation("add", "memory", f"e{i}")) for i in range(3))
        assert outbox.append(Operation("add", "memory", "overflow")) is False
        assert outbox.count() == 3

    def test_unicode_survives_a_round_trip(self, outbox: Outbox):
        outbox.append(Operation("add", "user", "prefiere español — y emoji 🎉"))
        assert outbox.load()[0].content == "prefiere español — y emoji 🎉"
