"""Chunking, hashing, assembly and cache behavior.

The properties that matter: exact bytes survive a round trip, a corrupt or
truncated piece is refused, a partial file never appears under its real name,
and eviction cannot take an object something still needs.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from hermes_pubky import models as m
from hermes_pubky.objects import (
    CACHE_BUDGET_BYTES,
    IntegrityError,
    ObjectCache,
    assemble,
    hash_file,
    stage_file,
)


def write(path: Path, data: bytes, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if executable:
        os.chmod(path, 0o700)
    return path


def round_trip(tmp_path: Path, data: bytes, logical: str, **kw) -> bytes:
    source = write(tmp_path / "src" / "file", data)
    staged = stage_file(source, logical, tmp_path / "objects", **kw)
    out = tmp_path / "out" / "file"
    assemble(staged.record, lambda ref: staged.objects[ref], out)
    return out.read_bytes()


class TestHashing:
    def test_matches_hashlib_for_a_large_file(self, tmp_path):
        data = os.urandom(3 * 1024 * 1024)
        source = write(tmp_path / "f", data)
        assert hash_file(source) == (hashlib.sha256(data).hexdigest(), len(data))

    def test_an_empty_file_hashes_to_the_empty_digest(self, tmp_path):
        assert hash_file(write(tmp_path / "f", b"")) == (m.EMPTY_SHA256, 0)


class TestStaging:
    def test_a_small_markdown_file_becomes_one_readable_object(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "SOUL.md", b"# me\n"), "profile/SOUL.md", tmp_path / "o")
        assert len(staged.record.pieces) == 1
        assert staged.record.pieces[0].object.endswith(".md")
        assert staged.record.size == 5

    def test_a_json_file_keeps_the_json_extension(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "c.json", b"{}"), "config/portable.json", tmp_path / "o")
        assert staged.record.pieces[0].object.endswith(".json")

    def test_an_unknown_type_becomes_a_bin_object(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "a.pdf", b"%PDF"), "workspace/a.pdf", tmp_path / "o")
        assert staged.record.pieces[0].object.endswith(".bin")

    def test_a_large_file_is_split_into_chunk_objects(self, tmp_path):
        data = os.urandom(int(2.5 * m.OBJECT_CHUNK_BYTES))
        staged = stage_file(
            write(tmp_path / "big", data), "workspace/big.bin", tmp_path / "o")
        sizes = [p.size for p in staged.record.pieces]
        assert len(sizes) == 3
        assert sizes[0] == sizes[1] == m.OBJECT_CHUNK_BYTES
        assert sizes[2] == len(data) - 2 * m.OBJECT_CHUNK_BYTES
        assert all(p.object.endswith(".chunk") for p in staged.record.pieces)
        assert sum(sizes) == staged.record.size == len(data)

    def test_the_database_is_always_chunked(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "db", b"small"), m.DATABASE_PATH, tmp_path / "o",
            force_chunked=True)
        assert staged.record.pieces[0].object.endswith(".chunk")

    def test_an_exactly_one_object_file_stays_a_single_object(self, tmp_path):
        data = os.urandom(m.OBJECT_CHUNK_BYTES)
        staged = stage_file(
            write(tmp_path / "f", data), "workspace/f.bin", tmp_path / "o")
        assert len(staged.record.pieces) == 1

    def test_an_empty_file_stages_no_objects(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "f", b""), "profile/SOUL.md", tmp_path / "o")
        assert staged.is_empty and staged.objects == {}
        assert staged.record.sha256 == m.EMPTY_SHA256

    def test_the_executable_bit_is_recorded(self, tmp_path):
        staged = stage_file(
            write(tmp_path / "s.sh", b"#!/bin/sh\n", executable=True),
            "profile/skills/s/run.sh", tmp_path / "o")
        assert staged.record.executable is True

    def test_an_oversized_file_is_refused_before_staging_anything(self, tmp_path):
        big = write(tmp_path / "SOUL.md", b"x" * (m.MAX_CORE_MARKDOWN_BYTES + 1))
        out = tmp_path / "o"
        with pytest.raises(m.SchemaError, match="over its"):
            stage_file(big, "profile/SOUL.md", out)
        assert not any(out.glob("*.md")), "no objects should have been written"

    def test_repeated_content_inside_one_file_is_stored_once(self, tmp_path):
        block = b"z" * m.OBJECT_CHUNK_BYTES
        staged = stage_file(
            write(tmp_path / "f", block * 2), "workspace/f.bin", tmp_path / "o")
        assert len(staged.record.pieces) == 2
        assert len(staged.objects) == 1, "identical chunks share one object"

    def test_staging_leaves_no_temporary_files(self, tmp_path):
        out = tmp_path / "o"
        stage_file(write(tmp_path / "f", os.urandom(2 * m.OBJECT_CHUNK_BYTES)),
                   "workspace/f.bin", out)
        assert not [p for p in out.iterdir() if p.name.startswith(".staging")]


class TestRoundTrip:
    @pytest.mark.parametrize("size", [1, 5, 1024, m.OBJECT_CHUNK_BYTES - 1,
                                      m.OBJECT_CHUNK_BYTES, m.OBJECT_CHUNK_BYTES + 1])
    def test_exact_bytes_survive(self, tmp_path, size):
        data = os.urandom(size)
        assert round_trip(tmp_path, data, "workspace/f.bin") == data

    def test_utf8_markdown_survives_byte_for_byte(self, tmp_path):
        data = "# Título\n\nprefiere español — y emoji 🎉\n".encode("utf-8")
        assert round_trip(tmp_path, data, "profile/SOUL.md") == data

    def test_an_empty_file_round_trips(self, tmp_path):
        assert round_trip(tmp_path, b"", "profile/SOUL.md") == b""

    def test_a_multi_chunk_file_round_trips(self, tmp_path):
        data = os.urandom(int(3.25 * m.OBJECT_CHUNK_BYTES))
        assert round_trip(tmp_path, data, "workspace/big.bin") == data

    def test_the_executable_bit_survives(self, tmp_path):
        source = write(tmp_path / "run.sh", b"#!/bin/sh\n", executable=True)
        staged = stage_file(source, "profile/skills/s/run.sh", tmp_path / "o")
        out = tmp_path / "out" / "run.sh"
        assemble(staged.record, lambda r: staged.objects[r], out)
        assert os.stat(out).st_mode & 0o100


class TestAssemblyRefusesBadBytes:
    def _staged(self, tmp_path, data=b"hello world"):
        source = write(tmp_path / "f", data)
        return stage_file(source, "workspace/f.bin", tmp_path / "o")

    def test_a_corrupted_object_is_refused(self, tmp_path):
        staged = self._staged(tmp_path)
        ref, path = next(iter(staged.objects.items()))
        path.write_bytes(b"tampered!!!")
        with pytest.raises(IntegrityError, match="does not match its hash"):
            assemble(staged.record, lambda r: staged.objects[r], tmp_path / "out")

    def test_a_truncated_object_is_refused(self, tmp_path):
        staged = self._staged(tmp_path)
        ref, path = next(iter(staged.objects.items()))
        path.write_bytes(b"hi")
        with pytest.raises(IntegrityError, match="bytes, expected"):
            assemble(staged.record, lambda r: staged.objects[r], tmp_path / "out")

    def test_a_wrong_whole_file_hash_is_refused(self, tmp_path):
        staged = self._staged(tmp_path)
        staged.record.sha256 = "c" * 64
        with pytest.raises(IntegrityError, match="whole-file hash"):
            assemble(staged.record, lambda r: staged.objects[r], tmp_path / "out")

    def test_a_failed_assembly_leaves_nothing_visible(self, tmp_path):
        staged = self._staged(tmp_path)
        ref, path = next(iter(staged.objects.items()))
        path.write_bytes(b"tampered!!!")
        out = tmp_path / "out" / "f.bin"
        with pytest.raises(IntegrityError):
            assemble(staged.record, lambda r: staged.objects[r], out)
        assert not out.exists()
        assert not list(out.parent.glob("*.partial"))

    def test_a_missing_object_is_refused(self, tmp_path):
        staged = self._staged(tmp_path)
        out = tmp_path / "out" / "f.bin"
        with pytest.raises(FileNotFoundError):
            assemble(staged.record, lambda r: tmp_path / "absent", out)
        assert not out.exists()


class TestObjectCache:
    def test_adopting_and_finding_an_object(self, tmp_path):
        cache = ObjectCache(tmp_path / "cache")
        staged = stage_file(write(tmp_path / "f", b"data"), "workspace/f.bin",
                            tmp_path / "o")
        ref, path = next(iter(staged.objects.items()))
        cache.adopt(ref, path)
        assert cache.has(ref) and cache.verify(ref)
        assert cache.size_of(ref) == 4

    def test_verify_catches_a_corrupted_cache_entry(self, tmp_path):
        cache = ObjectCache(tmp_path / "cache")
        staged = stage_file(write(tmp_path / "f", b"data"), "workspace/f.bin",
                            tmp_path / "o")
        ref, path = next(iter(staged.objects.items()))
        cache.adopt(ref, path)
        cache.path_for(ref).write_bytes(b"nope")
        assert cache.verify(ref) is False

    def test_a_reference_that_is_not_a_digest_is_refused(self, tmp_path):
        cache = ObjectCache(tmp_path / "cache")
        for ref in ("objects/../escape", "/etc/passwd", "objects/x.md"):
            with pytest.raises(m.SchemaError):
                cache.path_for(ref)

    def test_eviction_never_removes_a_protected_object(self, tmp_path):
        cache = ObjectCache(tmp_path / "cache", budget_bytes=1)
        refs = []
        for i in range(3):
            staged = stage_file(write(tmp_path / f"f{i}", bytes([i]) * 100),
                                "workspace/f.bin", tmp_path / f"o{i}")
            ref, path = next(iter(staged.objects.items()))
            cache.adopt(ref, path)
            refs.append(ref)

        removed = cache.evict_to_budget(protected=[refs[0]])
        assert refs[0] not in removed
        assert cache.has(refs[0])
        assert len(removed) >= 1

    def test_nothing_is_evicted_when_under_budget(self, tmp_path):
        cache = ObjectCache(tmp_path / "cache", budget_bytes=CACHE_BUDGET_BYTES)
        staged = stage_file(write(tmp_path / "f", b"data"), "workspace/f.bin",
                            tmp_path / "o")
        ref, path = next(iter(staged.objects.items()))
        cache.adopt(ref, path)
        assert cache.evict_to_budget(protected=[]) == []
        assert cache.has(ref)

    def test_an_empty_cache_is_safe_to_query(self, tmp_path):
        cache = ObjectCache(tmp_path / "absent")
        assert cache.total_bytes() == 0
        assert cache.evict_to_budget(protected=[]) == []
