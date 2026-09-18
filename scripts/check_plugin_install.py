#!/usr/bin/env python3
"""Validate the directory plugin and install its dependency in a fresh venv.

Before publishing, pass --wheel-dir dist so Hermes resolves the exact pinned
package from the candidate wheel. The wrapper is copied as the catalog's
subdirectory install would copy it; no source checkout of hermes-pubky is put
on the child interpreter's path.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the isolated installation check")
    source = Path(__file__).resolve().parents[1] / "plugin"
    with tempfile.TemporaryDirectory(prefix="hermes-pubky-install-") as directory:
        root = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("HERMES_")
               and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        env.update(HERMES_HOME=str(root / "home"), HERMES_SAFE_MODE="0",
                   HERMES_ENABLE_PROJECT_PLUGINS="0",
                   UV_FIND_LINKS=str(args.wheel_dir.resolve()),
                   PIP_FIND_LINKS=str(args.wheel_dir.resolve()))

        def run(*command: str) -> None:
            subprocess.run(command, env=env, cwd=root, check=True, timeout=300)

        run(uv, "venv", "--python", sys.executable, str(root / "venv"))
        python = str(root / "venv" / "bin" / "python")
        run(uv, "pip", "install", "--python", python,
            "-e", str(args.hermes_source.resolve()))
        run(python, "-c", "import importlib.util; "
            "assert importlib.util.find_spec('hermes_pubky') is None")
        target = root / "home" / "plugins" / "hermes-pubky"
        shutil.copytree(source, target)
        (root / "home" / "config.yaml").write_text(
            'memory:\n  provider: ""\n', encoding="utf-8")
        # This is the reviewer's admission gate, including the install scanner.
        run(python, "-m", "hermes_cli.main", "plugins", "validate", str(target),
            "--install-deps")
        run(python, "-m", "hermes_cli.main", "plugins", "enable", "hermes-pubky")
        run(python, "-c", PROBE)
        run(str(root / "venv" / "bin" / "hermes-pubky"), "--help")
        assert not (root / "home" / "state.db").exists()
        print("Fresh wrapper installation, dependency loading, scanner, and launcher passed.")


PROBE = '''
import os, sys
from pathlib import Path
import yaml
from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler
manager = get_plugin_manager()
manager.discover_and_load()
plugin = next(p for p in manager.list_plugins() if p['name'] == 'hermes-pubky')
assert plugin['enabled'] and not plugin['error'], plugin
assert plugin['tools'] == plugin['hooks'] == plugin['middleware'] == 0, plugin
help_text = get_plugin_command_handler('pubky')('')
assert 'conversation database snapshots' in help_text
assert 'remote Pubky homeserver' in help_text
assert not any(n == 'hermes_pubky' or n.startswith('hermes_pubky.') for n in sys.modules)
config = yaml.safe_load((Path(os.environ['HERMES_HOME']) / 'config.yaml').read_text())
assert config['memory']['provider'] == ''
# The separately installed dependency includes BOTH the provider and native SDK.
from hermes_pubky import _native
from hermes_pubky.provider import PubkyMemoryProvider
from hermes_pubky.hermes_adapter import assert_supported_runtime
assert_supported_runtime()
assert not PubkyMemoryProvider().is_available()
'''


if __name__ == "__main__":
    main()
