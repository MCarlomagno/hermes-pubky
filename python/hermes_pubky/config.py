"""Provider configuration, secret storage, and log redaction.

Non-secret settings live in Hermes' ``config.yaml`` under ``memory.pubky`` so
``hermes memory status`` can display them. The grant secret lives only in the
profile-scoped ``.env`` at mode 0600 — never in config.yaml, never in the
cache, and never on the homeserver.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import GRANT_SECRET_ENV, PROVIDER_NAME

DEFAULT_PROFILE_ID = "default"

# Startup must not block Hermes; five seconds is the plan's budget for the
# whole fetch, shared by the profile and the base context.
DEFAULT_STARTUP_TIMEOUT = 5.0


def load_hermes_config() -> Dict[str, Any]:
    """Read Hermes' config.yaml, or an empty dict outside Hermes."""
    try:
        from hermes_cli.config import load_config  # type: ignore

        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def save_hermes_config(config: Dict[str, Any]) -> bool:
    try:
        from hermes_cli.config import save_config  # type: ignore

        save_config(config)
        return True
    except Exception:
        return False


def provider_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ``memory.pubky`` block, always a dict."""
    config = load_hermes_config() if config is None else config
    memory = config.get("memory")
    if not isinstance(memory, dict):
        return {}
    block = memory.get(PROVIDER_NAME)
    return block if isinstance(block, dict) else {}


def profile_id(config: Optional[Dict[str, Any]] = None) -> str:
    """Which Pubky profile this Hermes profile is bound to."""
    value = provider_config(config).get("profile_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_PROFILE_ID


def set_provider_config(config: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``values`` into ``memory.pubky`` on a config dict."""
    memory = config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        config["memory"] = memory
    block = memory.get(PROVIDER_NAME)
    if not isinstance(block, dict):
        block = {}
    block.update(values)
    memory[PROVIDER_NAME] = block
    return config


def activate(config: Dict[str, Any]) -> Dict[str, Any]:
    """Select this provider as the active memory provider."""
    memory = config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        config["memory"] = memory
    memory["provider"] = PROVIDER_NAME
    return config


def is_active(config: Optional[Dict[str, Any]] = None) -> bool:
    config = load_hermes_config() if config is None else config
    memory = config.get("memory")
    if not isinstance(memory, dict):
        return False
    return memory.get("provider") == PROVIDER_NAME


# -- grant secret -----------------------------------------------------------


def read_grant_secret(env_file: Optional[Path] = None) -> str:
    """Return the stored grant secret, preferring the live environment.

    Hermes loads ``.env`` into the process, so the environment is normally
    already correct; reading the file directly is the fallback for CLI paths
    that run before that happens.
    """
    from_env = os.environ.get(GRANT_SECRET_ENV, "").strip()
    if from_env:
        return from_env
    if env_file is None:
        return ""
    return _read_env_var(env_file, GRANT_SECRET_ENV)


def write_grant_secret(env_file: Path, secret: str) -> None:
    """Persist the grant secret to ``.env`` at mode 0600."""
    _upsert_env_var(env_file, GRANT_SECRET_ENV, secret)
    os.environ[GRANT_SECRET_ENV] = secret


def clear_grant_secret(env_file: Path) -> None:
    _upsert_env_var(env_file, GRANT_SECRET_ENV, None)
    os.environ.pop(GRANT_SECRET_ENV, None)


def _read_env_var(env_file: Path, key: str) -> str:
    try:
        text = env_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _upsert_env_var(env_file: Path, key: str, value: Optional[str]) -> None:
    """Set (or delete, when ``value`` is None) one variable in a .env file.

    Rewrites in place so unrelated Hermes secrets in the same file are
    preserved, and re-applies 0600 because the file now holds ours too.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    written = False
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name == key:
            if value is not None and not written:
                out.append(f"{key}={value}")
                written = True
            continue  # drop duplicates and, when deleting, the line itself
        out.append(line)
    if value is not None and not written:
        out.append(f"{key}={value}")

    env_file.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# -- redaction --------------------------------------------------------------

# The SDK's stored-credential token format, plus anything that looks like a
# JWS. Matching on shape means a secret is scrubbed even if it reaches a log
# through a path we did not anticipate.
_SECRET_PATTERNS = (
    re.compile(r"pubky-grant-credential-[A-Za-z0-9_.:-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
)

REDACTED = "<redacted>"


def redact(text: Any) -> str:
    """Scrub grant material from a string before it is logged or displayed."""
    if text is None:
        return ""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    live = os.environ.get(GRANT_SECRET_ENV, "").strip()
    if live and len(live) >= 8:
        out = out.replace(live, REDACTED)
    return out


def fingerprint(secret: str) -> str:
    """A short, non-reversible label for a secret, safe to show a user."""
    if not secret:
        return "(none)"
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
