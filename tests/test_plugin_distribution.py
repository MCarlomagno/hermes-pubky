"""Exercise the shipped directory wrapper through Hermes' real loader."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(
    importlib.util.find_spec("hermes_cli") is None,
    reason="hermes-agent not installed",
)
@pytest.mark.parametrize("enabled", [False, True])
def test_companion_loads_without_activating_managed_state(tmp_path, enabled):
    wrapper = Path(__file__).resolve().parents[1] / "plugin"
    home = tmp_path / "home"
    shutil.copytree(wrapper, home / "plugins" / "hermes-pubky")
    config = {
        "plugins": {"enabled": ["hermes-pubky"] if enabled else []},
        "memory": {"provider": ""},
    }
    config_path = home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    original_config = config_path.read_bytes()
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_SAFE_MODE="0",
               HERMES_ENABLE_PROJECT_PLUGINS="0")
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler

enabled = sys.argv[1] == "True"
manager = get_plugin_manager()
manager.discover_and_load()
plugin = next(p for p in manager.list_plugins() if p["name"] == "hermes-pubky")
assert plugin["enabled"] == enabled, plugin
assert plugin["tools"] == plugin["hooks"] == plugin["middleware"] == 0, plugin
handler = get_plugin_command_handler("pubky")
if enabled:
    assert not plugin["error"], plugin
    help_text = handler("")
    assert "hermes-pubky agent init default" in help_text
    assert "hermes-pubky run default" in help_text
    assert "does not sync the current Hermes profile" in help_text
else:
    assert handler is None
# Loading the companion must not import the provider, native SDK or launcher.
assert not any(n == "hermes_pubky" or n.startswith("hermes_pubky.")
               for n in sys.modules)
""", str(enabled)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.read_bytes() == original_config
    assert not (home / "state.db").exists()
