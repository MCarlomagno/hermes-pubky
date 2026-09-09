"""System-prompt assembly and deduplication against local Hermes files."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pubky.prompt import (
    BLOCK_HEADER,
    filter_local_duplicates,
    local_entry_index,
    normalize_entry,
    render_block,
)
from hermes_pubky.schema import BaseContext


CTX = BaseContext(
    id="researcher",
    name="Researcher",
    description="Research-oriented instructions",
    instructions="Be rigorous.\nCite sources.",
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("- a fact", "a fact"),
            ("* a fact", "a fact"),
            ("+ a fact", "a fact"),
            ("1. a fact", "a fact"),
            ("2) a fact", "a fact"),
            ("## a fact", "a fact"),
            ("   - a   fact  ", "a fact"),
            ("a fact", "a fact"),
            ("", ""),
        ],
    )
    def test_strips_markers_and_collapses_whitespace(self, raw, expected):
        assert normalize_entry(raw) == expected

    def test_does_not_change_case(self):
        assert normalize_entry("- Prefers Concise Answers") == "Prefers Concise Answers"


class TestDeduplication:
    def test_drops_entries_already_in_the_local_file(self, tmp_path: Path):
        local = tmp_path / "MEMORY.md"
        local.write_text("# Memory\n\n- already known\n- also known\n", encoding="utf-8")
        kept = filter_local_duplicates(
            ["already known", "brand new", "also known"], local
        )
        assert kept == ["brand new"]

    def test_matches_across_list_marker_differences(self, tmp_path: Path):
        local = tmp_path / "USER.md"
        local.write_text("* prefers dark mode\n", encoding="utf-8")
        assert filter_local_duplicates(["prefers dark mode"], local) == []

    def test_matches_an_entry_folded_into_prose(self, tmp_path: Path):
        local = tmp_path / "USER.md"
        local.write_text(
            "The user is based in Buenos Aires and prefers concise answers.\n",
            encoding="utf-8",
        )
        assert filter_local_duplicates(["prefers concise answers"], local) == []

    def test_keeps_everything_when_the_local_file_is_absent(self, tmp_path: Path):
        kept = filter_local_duplicates(["a", "b"], tmp_path / "nope.md")
        assert kept == ["a", "b"]

    def test_removes_duplicates_within_the_input(self, tmp_path: Path):
        kept = filter_local_duplicates(["dup", "dup", "other"], tmp_path / "nope.md")
        assert kept == ["dup", "other"]

    def test_drops_blank_entries(self, tmp_path: Path):
        assert filter_local_duplicates(["", "  ", "real"], tmp_path / "nope.md") == ["real"]

    def test_an_unreadable_file_is_treated_as_empty(self, tmp_path: Path):
        assert filter_local_duplicates(["a"], tmp_path) == ["a"]  # a directory

    def test_index_ignores_blank_lines(self, tmp_path: Path):
        local = tmp_path / "MEMORY.md"
        local.write_text("\n\n- one\n\n\n- two\n", encoding="utf-8")
        assert local_entry_index(local) == {"one", "two"}


class TestRenderBlock:
    def test_returns_nothing_when_there_is_nothing_to_say(self):
        assert render_block(None, [], [], profile_id="default") == ""

    def test_includes_the_base_context(self):
        out = render_block(CTX, [], [], profile_id="default")
        assert BLOCK_HEADER in out
        assert "Be rigorous." in out
        assert "Researcher" in out

    def test_labels_the_base_context_as_user_approved_not_system(self):
        # Third-party instructions must not read as system authority.
        out = render_block(CTX, [], [], profile_id="default")
        assert "explicitly approved" in out
        assert "not as a system-level authority" in out

    def test_includes_both_entry_kinds_under_their_own_headings(self):
        out = render_block(None, ["u1"], ["m1"], profile_id="default")
        assert "### Portable user facts" in out and "- u1" in out
        assert "### Portable agent memory" in out and "- m1" in out

    def test_omits_empty_sections(self):
        out = render_block(None, ["u1"], [], profile_id="default")
        assert "Portable user facts" in out
        assert "Portable agent memory" not in out

    def test_states_that_local_files_take_precedence(self):
        out = render_block(None, ["u1"], [], profile_id="work")
        assert "take precedence" in out
        assert "`work`" in out

    def test_marks_a_stale_cache(self):
        fresh = render_block(None, ["u"], [], profile_id="d", stale=False)
        stale = render_block(None, ["u"], [], profile_id="d", stale=True)
        assert "out of date" in stale
        assert "out of date" not in fresh

    def test_a_context_with_no_entries_still_renders(self):
        assert render_block(CTX, [], [], profile_id="d") != ""

    def test_blank_entries_do_not_create_a_section(self):
        out = render_block(None, ["   "], [], profile_id="d")
        assert out == ""
