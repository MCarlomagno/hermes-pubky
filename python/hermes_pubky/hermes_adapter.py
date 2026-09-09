"""Everything specific to the pinned Hermes runtime.

The storage and sync modules never import Hermes. This module owns the paths,
the configuration shape, the conversation database and the discovery bridge, so
supporting another harness later means adding a sibling rather than untangling
the launcher.

Verified against `hermes-agent==0.19.0`, conversation schema 22. See
`tests/test_hermes_contract.py`, which fails if the installed package drifts.

Reference: implementation plan sections 8 and 9.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import PortableConfig, RuntimeInfo, SchemaError
from .paths import Layout, write_private

ADAPTER_ID = "hermes-0.19-sqlite22-v1"
RUNTIME_NAME = "hermes"
SUPPORTED_HERMES = "0.19.0"
SUPPORTED_DB_SCHEMA = 22

# Hermes separates memory entries with this exact delimiter. Files are stored
# and restored byte-for-byte; this exists for previews and counts only.
ENTRY_DELIMITER = "\n§\n"

# Options the launcher translates. `--query` in the CLI maps to Hermes' `-z`.
LAUNCH_MODULE = "hermes_cli.main"

# Local settings a device may override. Anything else in a device-config file
# is refused, so a stray key cannot redirect the managed profile.
DEVICE_OVERRIDABLE = frozenset({
    "model", "provider", "providers", "custom_providers", "fallback_providers",
    "auxiliary_model", "api_key", "base_url",
})

# Settings the launcher always controls. A downloaded document can never reach
# these, and a device file cannot either.
def required_settings(layout: Layout) -> Dict[str, Any]:
    return {
        "memory": {"provider": "pubky"},
        "terminal": {"backend": "local", "cwd": str(layout.workspace)},
    }


# Keys that would redirect the managed profile or disable an approval gate.
# Refused wherever they appear outside the launcher's own control.
FORBIDDEN_ANYWHERE = frozenset({
    "yolo", "accept_hooks", "worktree", "hermes_home", "safe_mode",
    "auto_approve", "dangerous_skip_permissions",
})


def runtime_info() -> RuntimeInfo:
    return RuntimeInfo(name=RUNTIME_NAME, version=SUPPORTED_HERMES,
                       adapter=ADAPTER_ID)


def installed_hermes_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return None


def assert_supported_runtime() -> str:
    """Refuse to run against an unverified Hermes before touching state."""
    found = installed_hermes_version()
    if found is None:
        raise SchemaError(
            "hermes-agent is not installed in this environment; install "
            f"hermes-agent=={SUPPORTED_HERMES} beside hermes-pubky")
    if found != SUPPORTED_HERMES:
        raise SchemaError(
            f"this release supports hermes-agent {SUPPORTED_HERMES}, found "
            f"{found}. Another version needs explicit adapter validation.")
    return found


# -- configuration -----------------------------------------------------------

def default_config() -> Dict[str, Any]:
    """Hermes' own defaults, read from the installed package."""
    try:
        from hermes_cli.config import DEFAULT_CONFIG  # type: ignore

        import copy

        return copy.deepcopy(dict(DEFAULT_CONFIG))
    except Exception:
        # Without Hermes present, fall back to the values the contract test
        # pins. Enough to render and test a configuration offline.
        return {
            "model": "",
            "toolsets": ["hermes-cli"],
            "agent": {"max_turns": 90},
            "memory": {
                "memory_enabled": True,
                "user_profile_enabled": True,
                "memory_char_limit": 2200,
                "user_char_limit": 1375,
            },
            "terminal": {"backend": "local", "cwd": "."},
        }


def load_device_config(layout: Layout) -> Dict[str, Any]:
    """Read this machine's local overrides, refusing anything out of scope."""
    path = layout.device_config_file
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise SchemaError("pyyaml is required to read device-config.yaml") from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SchemaError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"{path} must contain a mapping")

    for key in raw:
        if key in FORBIDDEN_ANYWHERE:
            raise SchemaError(
                f"{path} may not set {key!r}: the launcher controls the managed "
                "profile and its approval gates")
        if key not in DEVICE_OVERRIDABLE:
            raise SchemaError(
                f"{path} may not set {key!r}; local overrides are limited to "
                f"{sorted(DEVICE_OVERRIDABLE)}")
    return raw


def render_config(
    portable: PortableConfig,
    layout: Layout,
    device: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the effective configuration for the managed child.

    Order: Hermes defaults, then the portable allowlist, then this device's
    explicit local settings, then the settings the launcher must control. The
    last step is not overridable, so a downloaded document cannot point the
    agent somewhere else or turn off an approval prompt.
    """
    config = default_config()

    # Portable allowlist.
    if portable.model:
        config["model"] = portable.model
    config["toolsets"] = list(portable.toolsets)
    config.setdefault("agent", {})["max_turns"] = portable.max_turns
    memory = config.setdefault("memory", {})
    memory["memory_enabled"] = portable.memory_enabled
    memory["user_profile_enabled"] = portable.user_profile_enabled
    memory["memory_char_limit"] = portable.memory_char_limit
    memory["user_char_limit"] = portable.user_char_limit

    # Device-local settings: model endpoints and credentials belong here.
    for key, value in (device or {}).items():
        config[key] = value

    # Required last, so nothing above can have moved them.
    for key, value in required_settings(layout).items():
        section = config.setdefault(key, {})
        if isinstance(section, dict) and isinstance(value, dict):
            section.update(value)
        else:
            config[key] = value

    _assert_no_forbidden(config)
    return config


def _assert_no_forbidden(config: Dict[str, Any]) -> None:
    for key in FORBIDDEN_ANYWHERE:
        if key in config:
            raise SchemaError(
                f"refusing to write a configuration containing {key!r}")


def write_config(layout: Layout, config: Dict[str, Any]) -> Path:
    """Write the generated `config.yaml` into the dedicated home."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise SchemaError("pyyaml is required to write config.yaml") from exc
    body = yaml.safe_dump(config, sort_keys=True, allow_unicode=True)
    header = (
        "# Generated by hermes-pubky for a managed agent. Edits are replaced on\n"
        "# the next run. Portable settings live in the agent's snapshot; local\n"
        "# settings belong in device-config.yaml beside this profile.\n"
    )
    write_private(layout.hermes_config_file, (header + body).encode("utf-8"))
    return layout.hermes_config_file


def extract_portable(config: Dict[str, Any],
                     current: Optional[PortableConfig] = None) -> PortableConfig:
    """Read the allowlisted settings back out of a running configuration.

    Only the allowlist travels. Device-only and required fields stay local, so
    re-rendering can never upload a credential that was merged in.
    """
    base = current or PortableConfig()
    memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}

    def pick(value: Any, fallback: Any, kind: type) -> Any:
        return value if isinstance(value, kind) and not isinstance(value, bool) \
            or (kind is bool and isinstance(value, bool)) else fallback

    toolsets = config.get("toolsets")
    return PortableConfig(
        model=config.get("model") if isinstance(config.get("model"), str) else base.model,
        toolsets=list(toolsets) if isinstance(toolsets, list) and all(
            isinstance(t, str) for t in toolsets) else list(base.toolsets),
        max_turns=pick(agent.get("max_turns"), base.max_turns, int),
        memory_enabled=pick(memory.get("memory_enabled"), base.memory_enabled, bool),
        user_profile_enabled=pick(memory.get("user_profile_enabled"),
                                  base.user_profile_enabled, bool),
        memory_char_limit=pick(memory.get("memory_char_limit"),
                               base.memory_char_limit, int),
        user_char_limit=pick(memory.get("user_char_limit"), base.user_char_limit, int),
    )


# -- memory files ------------------------------------------------------------

def count_entries(data: bytes) -> int:
    """How many entries a Hermes memory file holds, for a preview."""
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return 0
    return len([part for part in text.split(ENTRY_DELIMITER.strip("\n")) if part.strip()])


# -- the Hermes discovery bridge --------------------------------------------

SHIM_INIT = '''"""Generated by hermes-pubky. Do not edit.

Hermes discovers memory providers by scanning $HERMES_HOME/plugins/, so this
file exists to make the managed provider visible. All behavior lives in the
installed hermes_pubky package.
"""

from hermes_pubky.provider import PubkyMemoryProvider, register  # noqa: F401

__all__ = ["PubkyMemoryProvider", "register"]
'''

SHIM_YAML = '''name: pubky
version: "{version}"
kind: exclusive
description: "Managed agent state stored on the user's Pubky homeserver."
'''


def write_plugin_shim(layout: Layout, version: str) -> List[str]:
    """Generate the profile-local plugin Hermes will discover.

    Regenerated on every run so it always matches the installed package. It is
    not an installer: it exists because Hermes requires directory discovery.
    """
    target = layout.plugin_dir
    target.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    files = {
        "__init__.py": SHIM_INIT,
        "plugin.yaml": SHIM_YAML.format(version=version),
    }
    for name, body in files.items():
        path = target / name
        encoded = body.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            continue
        write_private(path, encoded)
        written.append(name)
    return written


def launch_command(
    *,
    resume: Optional[str] = None,
    query: Optional[str] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
) -> List[str]:
    """The child's argv.

    Only the options in the plan's CLI are translated, and each is verified
    against the pinned parser by the contract test. `--query` becomes Hermes'
    `--oneshot`; `--no-restore-cwd` is always passed because the adapter rebases
    a session's recorded directory itself.
    """
    import sys

    argv = [sys.executable, "-m", LAUNCH_MODULE, "--no-restore-cwd"]
    if resume:
        argv += ["--resume", resume]
    if model:
        argv += ["--model", model]
    if toolsets:
        argv += ["--toolsets", ",".join(toolsets)]
    if query:
        argv += ["--oneshot", query]
    return argv


# -- conversation database ----------------------------------------------------
#
# Implemented in the conversation-recovery slice. Declared here so the
# supervisor's import surface is stable, and so an early call fails loudly
# instead of silently skipping saved conversations.

class ConversationUnsupported(SchemaError):
    """The conversation database could not be captured or restored."""


def capture_database(layout: Layout, cache: Any):
    """Consistent snapshot of `state.db`, chunked and hashed.

    Returns `(FileRecord, {object_reference: staged_path})`, or None when there
    is no database yet.
    """
    from .database import capture_database as _capture

    return _capture(layout, cache)


def restore_database(record: Any, resolve: Any, layout: Layout) -> None:
    """Rebuild `state.db` from a snapshot's chunks, verified end to end."""
    from .database import restore_database as _restore

    return _restore(record, resolve, layout)
