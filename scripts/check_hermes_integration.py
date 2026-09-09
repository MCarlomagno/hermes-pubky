#!/usr/bin/env python3
"""Verify the plugin against a real Hermes installation.

Run with a Python that has both ``hermes-agent`` and ``hermes-pubky``
installed, and ``HERMES_HOME`` pointing at a scratch directory:

    HERMES_HOME=/tmp/hermes-home python scripts/check_hermes_integration.py

Checks, in order:

1. ``hermes-pubky install`` writes a shim Hermes can find.
2. Hermes' own discovery lists ``pubky`` and loads it as a ``MemoryProvider``.
3. The provider starts from cache with no homeserver, and injects a prompt
   block containing the base context and the portable overlay.
4. Entries already present in local ``USER.md`` / ``MEMORY.md`` are not
   repeated in that block.
5. ``hermes pubky`` is registered as a CLI command.

Exits non-zero on the first failure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONTEXT_URL = (
    "pubky://8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
    "/pub/hermes.pubky.app/v1/contexts/researcher.json"
)
FAKE_HASH = "a" * 64

CHECKS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    line = f"  [{mark}] {label}"
    if detail and not condition:
        line += f"\n         {detail}"
    print(line)
    CHECKS.append(label if condition else f"FAILED: {label}")
    if not condition:
        sys.exit(1)


def seed(home: Path) -> None:
    """Lay down a cached profile, a context, and local Hermes memory files."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "memory:\n  provider: pubky\n  pubky:\n    profile_id: default\n",
        encoding="utf-8",
    )
    cache = home / "pubky" / "default"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "profile.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "profileId": "default",
                "baseContext": {"url": CONTEXT_URL, "sha256": FAKE_HASH},
                "user": ["prefers concise answers", "based in Buenos Aires"],
                "memory": ["deploy via scripts/deploy.sh", "CI runs on GitHub Actions"],
                "revision": 4,
                "updatedAt": "2026-09-09T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (cache / "context.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "researcher",
                "name": "Researcher",
                "description": "Research-oriented agent instructions",
                "instructions": "Be rigorous. Cite sources.",
            }
        ),
        encoding="utf-8",
    )
    (cache / "context.meta.json").write_text(
        json.dumps(
            {"url": CONTEXT_URL, "sha256": FAKE_HASH, "approvedAt": "2026-09-09T10:00:00Z"}
        ),
        encoding="utf-8",
    )
    # These two lines must be filtered out of the injected block.
    (home / "USER.md").write_text("# User\n\n- prefers concise answers\n", encoding="utf-8")
    (home / "MEMORY.md").write_text(
        "# Memory\n\n- deploy via scripts/deploy.sh\n", encoding="utf-8"
    )


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME") or "/tmp/hermes-home")
    os.environ["HERMES_HOME"] = str(home)
    # No grant: the provider must fall back to the cache rather than block.
    os.environ.pop("HERMES_PUBKY_GRANT_SECRET", None)

    print(f"\nHermes integration check (HERMES_HOME={home})\n" + "-" * 60)

    from hermes_pubky.installer import install, is_installed

    install(home, force=True)
    seed(home)
    check("shim installed into $HERMES_HOME/plugins/pubky", is_installed(home))

    from agent.memory_provider import MemoryProvider
    from plugins.memory import (
        discover_memory_providers,
        discover_plugin_cli_commands,
        load_memory_provider,
    )

    names = [name for name, _desc, _avail in discover_memory_providers()]
    check("Hermes discovers 'pubky'", "pubky" in names, f"discovered: {names}")

    provider = load_memory_provider("pubky")
    check("Hermes loads the provider", provider is not None)
    check("it is a MemoryProvider", isinstance(provider, MemoryProvider))
    check("it reports the right name", provider.name == "pubky")
    check("it exposes no tools in v0.1", provider.get_tool_schemas() == [])
    check("it offers a post_setup hook", hasattr(provider, "post_setup"))

    provider.initialize(
        "integration-check",
        hermes_home=str(home),
        platform="cli",
        agent_context="primary",
    )
    block = provider.system_prompt_block()
    print("\n--- injected system prompt block ---")
    print(block)
    print("--- end ---\n")

    check("a block is produced from cache alone", bool(block.strip()))
    check("it carries the base context", "Be rigorous. Cite sources." in block)
    check("it carries portable user facts", "based in Buenos Aires" in block)
    check("it carries portable agent memory", "CI runs on GitHub Actions" in block)
    check(
        "USER.md entries are not duplicated",
        "prefers concise answers" not in block,
    )
    check(
        "MEMORY.md entries are not duplicated",
        "deploy via scripts/deploy.sh" not in block,
    )
    check("a stale cache is marked as such", "out of date" in block)

    # A cron run must never write, even though the provider is active.
    from hermes_pubky.outbox import Outbox
    from hermes_pubky.paths import Layout

    provider.shutdown()
    cron = load_memory_provider("pubky")
    cron.initialize("cron-check", hermes_home=str(home), agent_context="cron")
    cron.on_memory_write("add", "memory", "written by cron", {})
    queued = Outbox(Layout(home, "default").outbox).count()
    check("a cron context does not write", queued == 0, f"{queued} op(s) queued")
    cron.shutdown()

    commands = [c["name"] for c in discover_plugin_cli_commands()]
    check("'hermes pubky' is registered", "pubky" in commands, f"registered: {commands}")

    print("-" * 60)
    print(f"  {len(CHECKS)} checks passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
