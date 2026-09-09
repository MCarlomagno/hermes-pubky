"""Installs the plugin shim into ``$HERMES_HOME/plugins/pubky/``.

Hermes 0.19 discovers memory providers by scanning that directory, not
through Python entry points, so a pip install alone is not enough to make the
provider visible. This writes three small files that import the real code from
the installed ``hermes_pubky`` package, which keeps upgrades a plain
``uv pip install -U`` with no shim churn.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Tuple

from .paths import PROVIDER_NAME, hermes_home

TEMPLATE_DIR = Path(__file__).parent / "plugin_template"

# (template name, destination name)
FILES: Tuple[Tuple[str, str], ...] = (
    ("__init__.py.tmpl", "__init__.py"),
    ("cli.py.tmpl", "cli.py"),
    ("plugin.yaml.tmpl", "plugin.yaml"),
)


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("hermes-pubky")
    except Exception:
        return "0.1.0"


def plugin_dir(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "plugins" / PROVIDER_NAME


def is_installed(home: Path | None = None) -> bool:
    return (plugin_dir(home) / "__init__.py").exists()


def install(home: Path | None = None, *, force: bool = False) -> Tuple[Path, List[str]]:
    """Write the shim. Returns the directory and the files written.

    Existing files are left alone unless ``force`` is set, so a user who has
    customized the shim is never silently overwritten.
    """
    target = plugin_dir(home)
    target.mkdir(parents=True, exist_ok=True)
    version = package_version()

    written: List[str] = []
    for template_name, dest_name in FILES:
        dest = target / dest_name
        if dest.exists() and not force:
            continue
        body = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        body = body.replace("__VERSION__", version)
        dest.write_text(body, encoding="utf-8")
        written.append(dest_name)
    return target, written


def uninstall(home: Path | None = None) -> bool:
    """Remove the shim directory. The plugin's data and cache are kept."""
    target = plugin_dir(home)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True
