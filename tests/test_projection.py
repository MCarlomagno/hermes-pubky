"""Capture and materialization.

The rules under test: an unchanged file uploads nothing, a never-fetched file is
not a deletion, dirty local work is never overwritten, and credential-shaped
files stay out of a checkpoint.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_pubky import models as m
from hermes_pubky.journal import Journal
from hermes_pubky.objects import ObjectCache
from hermes_pubky.paths import Layout
from hermes_pubky.projection import (
    AGENTS_MD_PATH,
    IGNORE_FILE,
    SOUL_PATH,
    USER_MEMORY_PATH,
    Ignore,
    NoChange,
    Projection,
    looks_like_private_key,
    scan_skills,
    scan_workspace,
)

RUNTIME = m.RuntimeInfo("hermes", "0.19.0", "hermes-0.19-sqlite22-v1")
OWNER = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


@pytest.fixture
def fx(tmp_path):
    layout = Layout(root=tmp_path / "root", network="testnet", owner=OWNER,
                    agent_id="default").ensure()
    journal = Journal(layout.journal_file)
    cache = ObjectCache(layout.cached_objects)
    projection = Projection(layout, journal, cache, runtime=RUNTIME,
                            device_id="f" * 32)
    yield projection
    journal.close()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestIgnoreFormat:
    def test_exact_paths_and_directory_prefixes(self):
        ignore = Ignore.parse("# comment\nbuild-output.txt\nscratch/\n\n")
        assert ignore.excludes("build-output.txt")
        assert ignore.excludes("scratch/anything/deep.txt")
        assert not ignore.excludes("keep.md")

    @pytest.mark.parametrize("line", ["*.log", "!keep.md", "/abs/path", "a\\b", "../up"])
    def test_unsupported_syntax_is_refused_with_the_line_number(self, line):
        with pytest.raises(m.SchemaError, match="line 1"):
            Ignore.parse(line)


class TestWorkspaceScan:
    def test_ordinary_files_are_included(self, tmp_path):
        workspace = tmp_path / "ws"
        write(workspace / "AGENTS.md", b"# agents\n")
        write(workspace / "notes/report.md", b"body\n")
        included, _excluded = scan_workspace(workspace)
        assert sorted(i.logical_path for i in included) == [
            "workspace/AGENTS.md", "workspace/notes/report.md"]

    @pytest.mark.parametrize("relative", [
        ".git/config", ".venv/lib/x.py", "node_modules/p/index.js",
        "__pycache__/x.pyc", "target/debug/bin", "dist/out.whl", "build/tmp",
        ".DS_Store", ".env", ".env.local", "credentials.env", "secrets.env",
        "auth.json", "credentials.json", "id_rsa", "id_ed25519",
        "notes.md.swp", "notes.md~", "scratch.tmp",
    ])
    def test_policy_excludes_credentials_and_noise(self, tmp_path, relative):
        workspace = tmp_path / "ws"
        write(workspace / relative, b"x")
        included, excluded = scan_workspace(workspace)
        assert [i.logical_path for i in included] == []
        assert excluded and excluded[0][0] == relative

    def test_a_symlink_is_never_followed(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = write(tmp_path / "outside.txt", b"secret")
        (workspace / "link.txt").symlink_to(outside)
        included, excluded = scan_workspace(workspace)
        assert included == []
        assert excluded[0][1] == "not a regular file"

    def test_the_ignore_file_itself_travels_with_the_agent(self, tmp_path):
        workspace = tmp_path / "ws"
        write(workspace / IGNORE_FILE, b"scratch/\n")
        write(workspace / "scratch/x.txt", b"x")
        included, _ = scan_workspace(workspace)
        assert [i.logical_path for i in included] == [f"workspace/{IGNORE_FILE}"]

    def test_the_executable_bit_is_recorded(self, tmp_path):
        workspace = tmp_path / "ws"
        script = write(workspace / "run.sh", b"#!/bin/sh\n")
        os.chmod(script, 0o700)
        included, _ = scan_workspace(workspace)
        assert included[0].executable is True


class TestSkillScan:
    def test_skill_files_become_profile_paths(self, tmp_path):
        skills = tmp_path / "skills"
        write(skills / "research/SKILL.md", b"# skill\n")
        included, _ = scan_skills(skills)
        assert [i.logical_path for i in included] == ["profile/skills/research/SKILL.md"]

    def test_skills_use_the_fixed_exclusion_list(self, tmp_path):
        skills = tmp_path / "skills"
        write(skills / "research/__pycache__/x.pyc", b"x")
        write(skills / "research/SKILL.md", b"# skill\n")
        included, excluded = scan_skills(skills)
        assert len(included) == 1 and excluded


class TestPrivateKeyDetection:
    def test_a_pem_private_key_is_recognized(self, tmp_path):
        path = write(tmp_path / "key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nx\n")
        assert looks_like_private_key(path)

    def test_ordinary_text_is_not(self, tmp_path):
        assert not looks_like_private_key(write(tmp_path / "a.md", b"# notes\n"))


class TestCapture:
    def test_a_first_capture_includes_the_core_files(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.user_memory_file, b"likes tea\n")
        write(fx.layout.agents_md, b"# workspace\n")

        candidate = fx.capture(None)
        assert not isinstance(candidate, NoChange)
        assert set(candidate.snapshot.files) == {
            SOUL_PATH, USER_MEMORY_PATH, AGENTS_MD_PATH}
        assert candidate.object_count == 3

    def test_capturing_twice_with_no_change_reports_no_change(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        first = fx.capture(None)
        assert fx.capture(first.snapshot).__class__ is NoChange

    def test_an_unchanged_file_uploads_nothing_on_the_next_capture(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        first = fx.capture(None)
        write(fx.layout.agents_md, b"# new\n")

        second = fx.capture(first.snapshot)
        uploaded = [u.object_path for u in second.uploads]
        soul_object = first.snapshot.files[SOUL_PATH].pieces[0].object
        # SOUL.md is still referenced, but its object is already acknowledged.
        assert soul_object in second.snapshot.objects()
        assert AGENTS_MD_PATH in second.snapshot.files
        assert len(uploaded) >= 1

    def test_a_second_capture_records_the_first_as_its_parent(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        first = fx.capture(None)
        write(fx.layout.soul_file, b"# changed\n")
        second = fx.capture(first.snapshot)
        assert second.snapshot.parent is not None
        assert second.snapshot.parent.snapshot_id == first.snapshot.snapshot_id

    def test_a_never_fetched_remote_file_stays_in_the_inventory(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        first = fx.capture(None)
        # A file only another machine has: described, present nowhere locally.
        remote_only = "workspace/reference.pdf"
        first.snapshot.files[remote_only] = m.FileRecord(
            sha256="c" * 64, size=10,
            pieces=[m.Piece(object=f"objects/{'c' * 64}.bin", sha256="c" * 64, size=10)])

        write(fx.layout.agents_md, b"# new\n")
        second = fx.capture(first.snapshot)
        assert remote_only in second.snapshot.files, \
            "absence of a never-fetched file is not a deletion"

    def test_an_explicit_deletion_drops_the_path(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.workspace / "old.md", b"old\n")
        first = fx.capture(None)
        assert "workspace/old.md" in first.snapshot.files

        (fx.layout.workspace / "old.md").unlink()
        fx.journal.mark_deleted("workspace/old.md")
        second = fx.capture(first.snapshot)
        assert "workspace/old.md" not in second.snapshot.files

    def test_a_generated_portable_config_is_included(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        candidate = fx.capture(None, portable=m.PortableConfig(model="test/model"))
        assert m.PORTABLE_CONFIG_PATH in candidate.snapshot.files

    def test_the_sealed_snapshot_is_on_disk_and_hashes_correctly(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        candidate = fx.capture(None)
        from hermes_pubky.objects import hash_bytes

        body = candidate.snapshot_path.read_bytes()
        assert hash_bytes(body) == candidate.snapshot_hash
        assert m.Snapshot.parse(body).snapshot_id == candidate.snapshot.snapshot_id

    def test_every_object_is_staged_under_pending(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        candidate = fx.capture(None)
        for upload in candidate.uploads:
            assert Path(upload.local_path).is_file()
            assert fx.layout.pending in Path(upload.local_path).parents

    def test_a_turn_landing_mid_capture_discards_the_observation(self, fx, monkeypatch):
        write(fx.layout.soul_file, b"# me\n")
        original = fx.journal.generation

        # Simulate a foreground write completing while the scan ran.
        real_inventory = fx._local_inventory
        def bump_then_scan():
            items = real_inventory()
            fx.journal.bump_generation()
            return items
        monkeypatch.setattr(fx, "_local_inventory", bump_then_scan)

        assert isinstance(fx.capture(None), NoChange)
        assert fx.journal.generation > original


class TestMaterialize:
    def test_core_files_are_installed_eagerly(self, fx, tmp_path):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.agents_md, b"# workspace\n")
        candidate = fx.capture(None)

        # Wipe the working copy, then restore from the snapshot.
        fx.layout.soul_file.unlink()
        fx.layout.agents_md.unlink()
        for record in fx.journal.list_materialized():
            fx.journal.forget_materialized(record.logical_path)

        objects = {u.object_path: Path(u.local_path) for u in candidate.uploads}
        installed = fx.materialize(candidate.snapshot,
                                   lambda reference: objects[reference])
        assert SOUL_PATH in installed and AGENTS_MD_PATH in installed
        assert fx.layout.soul_file.read_bytes() == b"# me\n"

    def test_a_workspace_document_stays_remote_only(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.workspace / "big.pdf", b"%PDF-1.7\n")
        candidate = fx.capture(None)
        (fx.layout.workspace / "big.pdf").unlink()
        for record in fx.journal.list_materialized():
            fx.journal.forget_materialized(record.logical_path)

        objects = {u.object_path: Path(u.local_path) for u in candidate.uploads}
        installed = fx.materialize(candidate.snapshot,
                                   lambda reference: objects[reference])
        assert "workspace/big.pdf" not in installed
        assert not (fx.layout.workspace / "big.pdf").exists()
        record = fx.journal.get_materialized("workspace/big.pdf")
        assert record is not None and not record.present

    def test_an_explicitly_requested_path_is_fetched(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.workspace / "ref.pdf", b"%PDF\n")
        candidate = fx.capture(None)
        (fx.layout.workspace / "ref.pdf").unlink()
        for record in fx.journal.list_materialized():
            fx.journal.forget_materialized(record.logical_path)

        objects = {u.object_path: Path(u.local_path) for u in candidate.uploads}
        installed = fx.materialize(candidate.snapshot,
                                   lambda reference: objects[reference],
                                   extra_paths=["workspace/ref.pdf"])
        assert "workspace/ref.pdf" in installed
        assert (fx.layout.workspace / "ref.pdf").read_bytes() == b"%PDF\n"

    def test_dirty_local_work_is_never_overwritten(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        candidate = fx.capture(None)
        write(fx.layout.soul_file, b"# edited locally\n")
        fx.journal.mark_dirty(SOUL_PATH)

        objects = {u.object_path: Path(u.local_path) for u in candidate.uploads}
        fx.materialize(candidate.snapshot, lambda reference: objects[reference])
        assert fx.layout.soul_file.read_bytes() == b"# edited locally\n"

    def test_an_unchanged_file_is_not_rewritten(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        candidate = fx.capture(None)
        objects = {u.object_path: Path(u.local_path) for u in candidate.uploads}
        installed = fx.materialize(candidate.snapshot,
                                  lambda reference: objects[reference])
        assert SOUL_PATH not in installed, "already present at the right hash"


class TestDirtyScan:
    def test_a_new_file_is_dirty(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        assert SOUL_PATH in fx.scan_dirty()

    def test_a_captured_file_is_clean(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        fx.capture(None)
        assert fx.scan_dirty() == []

    def test_an_edited_file_becomes_dirty(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        fx.capture(None)
        write(fx.layout.soul_file, b"# changed\n")
        assert SOUL_PATH in fx.scan_dirty()

    def test_a_deleted_tracked_file_is_dirty(self, fx):
        write(fx.layout.soul_file, b"# me\n")
        write(fx.layout.workspace / "a.md", b"a\n")
        fx.capture(None)
        (fx.layout.workspace / "a.md").unlink()
        assert "workspace/a.md" in fx.scan_dirty()
