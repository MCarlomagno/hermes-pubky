"""The plugin shim that makes the provider discoverable by Hermes."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pubky import installer
from hermes_pubky.setup_flow import read_local_entries


class TestInstall:
    def test_writes_the_three_files_hermes_looks_for(self, home: Path):
        target, written = installer.install(home)
        assert target == home / "plugins" / "pubky"
        assert sorted(written) == ["__init__.py", "cli.py", "plugin.yaml"]
        for name in written:
            assert (target / name).is_file()

    def test_the_shim_advertises_itself_as_a_memory_provider(self, home: Path):
        # Hermes sniffs __init__.py for these names to route the plugin to
        # memory-provider discovery instead of the generic plugin loader.
        target, _ = installer.install(home)
        source = (target / "__init__.py").read_text()
        assert "MemoryProvider" in source or "register_memory_provider" in source

    def test_the_shim_delegates_rather_than_duplicating_logic(self, home: Path):
        target, _ = installer.install(home)
        assert "from hermes_pubky.provider import" in (target / "__init__.py").read_text()
        assert "from hermes_pubky.cli import" in (target / "cli.py").read_text()

    def test_the_manifest_pins_the_matching_package_version(self, home: Path):
        target, _ = installer.install(home)
        manifest = (target / "plugin.yaml").read_text()
        version = installer.package_version()
        assert f'version: "{version}"' in manifest
        assert f"hermes-pubky=={version}" in manifest
        assert "__VERSION__" not in manifest

    def test_the_manifest_declares_the_exclusive_kind(self, home: Path):
        target, _ = installer.install(home)
        assert "kind: exclusive" in (target / "plugin.yaml").read_text()

    def test_the_manifest_parses_as_yaml(self, home: Path):
        yaml = pytest.importorskip("yaml")
        target, _ = installer.install(home)
        meta = yaml.safe_load((target / "plugin.yaml").read_text())
        assert meta["name"] == "pubky"
        assert meta["description"]

    def test_is_idempotent_and_does_not_clobber_edits(self, home: Path):
        installer.install(home)
        marker = "# edited by hand\n"
        (home / "plugins" / "pubky" / "cli.py").write_text(marker, encoding="utf-8")

        _target, written = installer.install(home)

        assert written == []
        assert (home / "plugins" / "pubky" / "cli.py").read_text() == marker

    def test_force_overwrites(self, home: Path):
        installer.install(home)
        (home / "plugins" / "pubky" / "cli.py").write_text("# stale\n", encoding="utf-8")

        _target, written = installer.install(home, force=True)

        assert "cli.py" in written
        assert "from hermes_pubky.cli import" in (
            home / "plugins" / "pubky" / "cli.py"
        ).read_text()

    def test_is_installed_reflects_reality(self, home: Path):
        assert installer.is_installed(home) is False
        installer.install(home)
        assert installer.is_installed(home) is True


class TestUninstall:
    def test_removes_the_shim(self, home: Path):
        installer.install(home)
        assert installer.uninstall(home) is True
        assert not installer.plugin_dir(home).exists()

    def test_removing_nothing_is_not_an_error(self, home: Path):
        assert installer.uninstall(home) is False

    def test_leaves_the_users_data_alone(self, home: Path):
        data = home / "pubky" / "default"
        data.mkdir(parents=True)
        (data / "profile.json").write_text("{}", encoding="utf-8")

        installer.install(home)
        installer.uninstall(home)

        assert (data / "profile.json").exists()


class TestConsoleScript:
    def test_install_then_status_run_without_error(self, home: Path, capsys, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(home))
        from hermes_pubky.__main__ import main

        assert main(["install", "--home", str(home)]) == 0
        assert "hermes memory setup pubky" in capsys.readouterr().out

        assert main(["status", "--home", str(home)]) == 0
        assert "installed" in capsys.readouterr().out

    def test_no_command_prints_help_and_fails(self, capsys):
        from hermes_pubky.__main__ import main

        assert main([]) == 1
        assert "usage" in capsys.readouterr().out.lower()


class TestLocalEntryImport:
    def test_reads_bullet_entries_only(self, tmp_path: Path):
        path = tmp_path / "MEMORY.md"
        path.write_text(
            "# Memory\n\n"
            "Some prose that is not an entry.\n\n"
            "- first fact\n"
            "* second fact\n"
            "+ third fact\n"
            "\n"
            "## A heading\n",
            encoding="utf-8",
        )
        assert read_local_entries(path) == ["first fact", "second fact", "third fact"]

    def test_deduplicates(self, tmp_path: Path):
        path = tmp_path / "USER.md"
        path.write_text("- same\n- same\n", encoding="utf-8")
        assert read_local_entries(path) == ["same"]

    def test_a_missing_file_yields_nothing(self, tmp_path: Path):
        assert read_local_entries(tmp_path / "nope.md") == []

    def test_respects_the_entry_cap(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("hermes_pubky.setup_flow.MAX_ENTRIES", 3)
        path = tmp_path / "MEMORY.md"
        path.write_text("".join(f"- fact {i}\n" for i in range(10)), encoding="utf-8")
        assert len(read_local_entries(path)) == 3

    def test_skips_oversized_entries(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("hermes_pubky.setup_flow.MAX_ENTRY_CHARS", 10)
        path = tmp_path / "MEMORY.md"
        path.write_text("- short\n- " + "x" * 50 + "\n", encoding="utf-8")
        assert read_local_entries(path) == ["short"]
