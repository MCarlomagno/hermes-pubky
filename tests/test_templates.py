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
OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


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
    """Only a copy that is exactly what was adopted is ever replaced."""

    A, B, C = "a" * 64, "b" * 64, "c" * 64

    @pytest.mark.parametrize("adopted,personal,upstream,expected", [
        # untouched locally, changed upstream: update
        (A, A, B, ("updates", "profile/SOUL.md")),
        # unchanged upstream: nothing to do, whatever happened locally
        (A, A, A, ("unchanged", "profile/SOUL.md")),
        (A, C, A, ("unchanged", "profile/SOUL.md")),
        # changed on both sides: conflict
        (A, C, B, ("conflicts", "profile/SOUL.md")),
        # never adopted, absent locally: a new file is proposed
        (None, None, B, ("updates", "profile/SOUL.md")),
        # never adopted, but a personal file exists: never replaced
        (None, C, B, ("conflicts", "profile/SOUL.md")),
        # deleted upstream, untouched locally: deletion proposed
        (A, A, None, ("deletions", "profile/SOUL.md")),
        # deleted upstream, edited locally: conflict
        (A, C, None, ("conflicts", "profile/SOUL.md")),
    ])
    def test_each_path_lands_in_exactly_one_bucket(self, adopted, personal,
                                                     upstream, expected):
        path = "profile/SOUL.md"
        plan = _three_way(
            origin({path: adopted} if adopted else {}),
            snapshot_with({path: personal} if personal else {}),
            template_with({path: upstream} if upstream else {}))
        buckets = {"updates": plan.updates, "deletions": plan.deletions,
                   "unchanged": plan.unchanged, "conflicts": list(plan.conflicts)}
        assert {k: v for k, v in buckets.items() if v} == {expected[0]: [expected[1]]}

    def test_a_conflict_anywhere_is_visible_beside_the_clean_paths(self):
        # cmd_update applies nothing while conflicts remain; this asserts the
        # data that decision is made from.
        plan = _three_way(
            origin({"a.md": self.A, "b.md": self.A}),
            snapshot_with({"a.md": self.C, "b.md": self.A}),
            template_with({"a.md": self.B, "b.md": self.B}))
        assert list(plan.conflicts) == ["a.md"] and plan.updates == ["b.md"]


class TestAdoption:
    def test_template_settings_are_adopted_into_the_checkpoint(self, tmp_path, monkeypatch):
        """A template's portable.json becomes the agent's, not a stray file."""
        from fakes import FakeAgentRemote

        from hermes_pubky import hermes_adapter as adapter
        from hermes_pubky.journal import new_id
        from hermes_pubky.objects import ObjectCache, stage_file
        from hermes_pubky.paths import Layout
        from hermes_pubky.supervisor import Supervisor
        from hermes_pubky.templates import _checkpoint_after_template, _copy_template

        monkeypatch.setattr(adapter, "assert_supported_runtime", lambda: "0.19.0")
        remote = FakeAgentRemote()
        layout = Layout(root=tmp_path / "root", network="testnet", owner=OWNER,
                        agent_id="default").ensure()
        layout.soul_file.write_bytes(b"# mine\n")
        supervisor = Supervisor(layout, network="testnet", remote_factory=lambda: remote)

        # A published template whose objects the fake "homeserver" serves.
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "portable.json").write_bytes(
            m.PortableConfig(model="template/model").to_bytes())
        (bundle / "SOUL.md").write_bytes(b"# from the template\n")
        files, template_objects = {}, {}
        for name, logical in (("portable.json", m.PORTABLE_CONFIG_PATH),
                              ("SOUL.md", "profile/SOUL.md")):
            staged = stage_file(bundle / name, logical, tmp_path / "staged")
            files[logical] = staged.record
            for ref, path in staged.objects.items():
                remote.objects[ref] = path.read_bytes()
        template = m.TemplateSnapshot(template_id="researcher", snapshot_id=new_id(),
                                      created_at=STAMP, runtime=RUNTIME, files=files)

        with supervisor.session() as s:
            adapter.write_config(layout, adapter.render_config(
                m.PortableConfig(model="original/model"), layout, {}))
            assert s.sync().ok
            installed, portable = _copy_template(layout, s.journal, remote, template)
            assert portable is not None and portable.model == "template/model"
            assert layout.soul_file.read_bytes() == b"# from the template\n"
            assert not (layout.hermes_home / "portable-from-template.json").exists()
            code = _checkpoint_after_template(s, portable, m.TemplateOrigin(
                url="pubky://k/pub/t/head.json", snapshot_id=template.snapshot_id,
                sha256="a" * 64, adopted_at=STAMP,
                managed_paths={p: r.sha256 for p, r in files.items()}))
            assert code == 0
            base = s._base()  # noqa: SLF001
        head = remote.read_head()
        assert head.snapshot_id == base.snapshot_id
        assert base.template is not None
        assert base.files[m.PORTABLE_CONFIG_PATH].sha256 == files[m.PORTABLE_CONFIG_PATH].sha256
        # The rendered configuration follows, so the next capture agrees.
        import yaml

        assert yaml.safe_load(layout.hermes_config_file.read_text())["model"] == "template/model"
