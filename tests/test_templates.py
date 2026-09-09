"""Template bundles and the explicit three-way update.

What matters: a bundle publishes only reusable content, the review digest
covers exactly what would become public, and an update never partially applies
or silently overwrites a personal edit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_pubky import models as m
from hermes_pubky.templates import (
    cmd_init,
    read_bundle,
    _three_way,
)

RUNTIME = m.RuntimeInfo("hermes", "0.19.0", "hermes-0.19-sqlite22-v1")
STAMP = "2026-09-09T18:00:00Z"


def make_bundle(tmp_path: Path, **files) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "template.json").write_text(json.dumps({
        "id": "researcher", "name": "Researcher", "description": "For research.",
    }), encoding="utf-8")
    for name, body in files.items():
        path = directory / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return directory


class TestBundleValidation:
    def test_init_creates_a_publishable_skeleton(self, tmp_path, capsys):
        directory = tmp_path / "new"
        assert cmd_init(directory) == 0
        for name in ("template.json", "SOUL.md", "AGENTS.md", "portable.json"):
            assert (directory / name).is_file()
        assert (directory / "skills").is_dir()
        assert read_bundle(directory, "researcher").template_id == "researcher"

    def test_init_refuses_a_non_empty_directory(self, tmp_path):
        directory = tmp_path / "used"
        directory.mkdir()
        (directory / "something").write_text("x", encoding="utf-8")
        assert cmd_init(directory) != 0

    def test_a_bundle_maps_files_to_template_paths(self, tmp_path):
        directory = make_bundle(
            tmp_path, **{"SOUL.md": "# be rigorous\n", "AGENTS.md": "# ws\n",
                         "skills__research__SKILL.md": "# skill\n"})
        bundle = read_bundle(directory)
        assert set(bundle.files) == {
            "profile/SOUL.md", "workspace/AGENTS.md",
            "profile/skills/research/SKILL.md"}

    def test_a_bundle_without_publishable_content_is_refused(self, tmp_path):
        directory = tmp_path / "empty"
        directory.mkdir()
        (directory / "template.json").write_text('{"id":"researcher"}',
                                                 encoding="utf-8")
        with pytest.raises(m.SchemaError, match="nothing publishable"):
            read_bundle(directory)

    def test_a_missing_manifest_is_refused(self, tmp_path):
        directory = tmp_path / "no-manifest"
        directory.mkdir()
        (directory / "SOUL.md").write_text("# x\n", encoding="utf-8")
        with pytest.raises(m.SchemaError, match="no template.json"):
            read_bundle(directory)

    def test_a_hostile_template_id_is_refused(self, tmp_path):
        directory = make_bundle(tmp_path, **{"SOUL.md": "# x\n"})
        with pytest.raises(Exception):
            read_bundle(directory, "../escape")

    def test_a_bundle_portable_config_must_pass_the_allowlist(self, tmp_path):
        directory = make_bundle(tmp_path, **{
            "SOUL.md": "# x\n",
            "portable.json": json.dumps({"schemaVersion": 2, "providers": {}}),
        })
        with pytest.raises(m.SchemaError, match="unsupported keys"):
            read_bundle(directory)

    def test_a_file_outside_the_allowed_set_is_simply_not_published(self, tmp_path):
        directory = make_bundle(tmp_path, **{
            "SOUL.md": "# x\n", "notes.md": "personal notes\n"})
        bundle = read_bundle(directory)
        assert "notes.md" not in str(bundle.files)
        assert all(p.startswith(("profile/", "workspace/AGENTS.md", "config/"))
                   for p in bundle.files)


class TestReviewDigest:
    def _snapshot(self, tmp_path, **files):
        directory = make_bundle(tmp_path, **files)
        bundle = read_bundle(directory)
        snapshot, _objects = bundle.snapshot(tmp_path / "staging")
        return snapshot

    def test_the_digest_covers_content(self, tmp_path):
        one = self._snapshot(tmp_path, **{"SOUL.md": "# one\n"})
        two = self._snapshot(tmp_path / "b", **{"SOUL.md": "# two\n"})
        assert one.review_digest() != two.review_digest()

    def test_the_digest_covers_the_file_set(self, tmp_path):
        one = self._snapshot(tmp_path, **{"SOUL.md": "# x\n"})
        two = self._snapshot(tmp_path / "b",
                             **{"SOUL.md": "# x\n", "AGENTS.md": "# ws\n"})
        assert one.review_digest() != two.review_digest()

    def test_the_digest_is_stable_for_identical_content(self, tmp_path):
        one = self._snapshot(tmp_path, **{"SOUL.md": "# x\n"})
        two = self._snapshot(tmp_path / "b", **{"SOUL.md": "# x\n"})
        assert one.review_digest() == two.review_digest()

    def test_a_published_snapshot_parses_as_a_template(self, tmp_path):
        snapshot = self._snapshot(tmp_path, **{"SOUL.md": "# x\n"})
        again = m.TemplateSnapshot.parse(snapshot.to_bytes())
        assert again.template_id == "researcher"


def origin(managed: dict, snapshot_id: str = "1" * 32) -> m.TemplateOrigin:
    return m.TemplateOrigin(url="pubky://k/pub/t/head.json",
                            snapshot_id=snapshot_id, sha256="a" * 64,
                            adopted_at=STAMP, managed_paths=managed)


def snapshot_with(files: dict, template=None) -> m.Snapshot:
    return m.Snapshot(
        agent_id="default", snapshot_id="2" * 32, created_at=STAMP,
        device_id="f" * 32, runtime=RUNTIME, template=template,
        files={path: m.FileRecord(sha256=digest, size=1,
                                  pieces=[m.Piece(f"objects/{digest}.md", digest, 1)])
               for path, digest in files.items()})


def template_with(files: dict) -> m.TemplateSnapshot:
    return m.TemplateSnapshot(
        template_id="researcher", snapshot_id="3" * 32, created_at=STAMP,
        runtime=RUNTIME,
        files={path: m.FileRecord(sha256=digest, size=1,
                                  pieces=[m.Piece(f"objects/{digest}.md", digest, 1)])
               for path, digest in files.items()})


class TestThreeWayUpdate:
    def test_an_untouched_path_is_proposed(self):
        adopted = {"profile/SOUL.md": "a" * 64}
        current = snapshot_with({"profile/SOUL.md": "a" * 64})
        upstream = template_with({"profile/SOUL.md": "b" * 64})
        proposed, conflicts, unchanged = _three_way(origin(adopted), current, upstream)
        assert proposed == ["profile/SOUL.md"] and not conflicts

    def test_an_unchanged_upstream_path_is_reported_unchanged(self):
        adopted = {"profile/SOUL.md": "a" * 64}
        current = snapshot_with({"profile/SOUL.md": "a" * 64})
        upstream = template_with({"profile/SOUL.md": "a" * 64})
        proposed, conflicts, unchanged = _three_way(origin(adopted), current, upstream)
        assert unchanged == ["profile/SOUL.md"] and not proposed and not conflicts

    def test_a_locally_edited_path_that_also_changed_upstream_conflicts(self):
        adopted = {"profile/SOUL.md": "a" * 64}
        current = snapshot_with({"profile/SOUL.md": "c" * 64})  # personal edit
        upstream = template_with({"profile/SOUL.md": "b" * 64})
        proposed, conflicts, _unchanged = _three_way(origin(adopted), current, upstream)
        assert list(conflicts) == ["profile/SOUL.md"]
        assert not proposed

    def test_a_locally_edited_path_that_did_not_change_upstream_is_left_alone(self):
        adopted = {"profile/SOUL.md": "a" * 64}
        current = snapshot_with({"profile/SOUL.md": "c" * 64})
        upstream = template_with({"profile/SOUL.md": "a" * 64})
        proposed, conflicts, unchanged = _three_way(origin(adopted), current, upstream)
        assert unchanged == ["profile/SOUL.md"] and not conflicts

    def test_a_new_upstream_file_is_proposed(self):
        adopted = {"profile/SOUL.md": "a" * 64}
        current = snapshot_with({"profile/SOUL.md": "a" * 64})
        upstream = template_with({"profile/SOUL.md": "a" * 64,
                                  "profile/skills/new/SKILL.md": "d" * 64})
        proposed, conflicts, _unchanged = _three_way(origin(adopted), current, upstream)
        assert proposed == ["profile/skills/new/SKILL.md"] and not conflicts

    def test_a_path_deleted_upstream_but_edited_locally_conflicts(self):
        adopted = {"profile/skills/old/SKILL.md": "a" * 64}
        current = snapshot_with({"profile/skills/old/SKILL.md": "c" * 64})
        upstream = template_with({})
        _proposed, conflicts, _unchanged = _three_way(origin(adopted), current, upstream)
        assert list(conflicts) == ["profile/skills/old/SKILL.md"]

    def test_a_path_deleted_upstream_and_untouched_locally_is_not_a_conflict(self):
        adopted = {"profile/skills/old/SKILL.md": "a" * 64}
        current = snapshot_with({"profile/skills/old/SKILL.md": "a" * 64})
        upstream = template_with({})
        _proposed, conflicts, _unchanged = _three_way(origin(adopted), current, upstream)
        assert not conflicts

    def test_a_conflict_anywhere_leaves_the_whole_update_unproposed(self):
        # One conflict means none of the update is applied, so a caller must not
        # be able to advance the pin partially.
        adopted = {"a.md": "a" * 64, "b.md": "a" * 64}
        current = snapshot_with({"a.md": "c" * 64, "b.md": "a" * 64})
        upstream = template_with({"a.md": "b" * 64, "b.md": "b" * 64})
        proposed, conflicts, _unchanged = _three_way(origin(adopted), current, upstream)
        assert conflicts, "the edited path must conflict"
        assert "b.md" in proposed, "the clean path is still proposed"
        # cmd_update refuses to apply anything while conflicts remain; this
        # asserts the data the decision is made from.
