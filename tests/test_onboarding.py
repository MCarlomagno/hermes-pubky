"""Creating an agent from existing content: nothing offered is silently dropped."""

from __future__ import annotations

from hermes_pubky.onboarding import plan_import


def test_a_workspace_alone_is_a_valid_import(tmp_path):
    workspace = tmp_path / "notes"
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"# report\n")
    plan = plan_import(None, workspace)
    assert [logical for logical, _p, _s in plan.files] == ["workspace/report.md"]


def test_portable_settings_are_read_from_the_profile(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_bytes(b"# me\n")
    (home / "config.yaml").write_text("model: openai/gpt-x\ntoolsets: [hermes-cli, web]\n")
    plan = plan_import(home, None)
    assert plan.portable.model == "openai/gpt-x"
    assert plan.portable.toolsets == ["hermes-cli", "web"]
