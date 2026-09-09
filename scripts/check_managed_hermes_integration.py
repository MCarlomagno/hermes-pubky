#!/usr/bin/env python3
"""Launch a real Hermes as a managed child and verify the integration.

Creates its own scratch root, renders a working copy, generates the discovery
bridge, then runs an actual `hermes_cli.main` turn against a local fake model.
Nothing here contacts a homeserver, so it runs without a testnet.

    python scripts/check_managed_hermes_integration.py

Refuses to run if any path resolves inside the caller's real Hermes profile.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CHECKS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"         {detail}")
        sys.exit(1)
    CHECKS.append(label)


def main() -> int:
    from fake_model import FakeModel

    from hermes_pubky import hermes_adapter as adapter
    from hermes_pubky.database import open_or_create
    from hermes_pubky.journal import Journal
    from hermes_pubky.models import PortableConfig
    from hermes_pubky.paths import Layout, write_private

    real_home = (Path.home() / ".hermes").resolve()
    root = Path(tempfile.mkdtemp(prefix="hermes-pubky-managed-"))
    if real_home == root or real_home in root.parents:
        raise SystemExit(f"refusing to run inside {real_home}")

    print(f"\nManaged Hermes integration check\n  root {root}\n" + "-" * 60)
    owner = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
    layout = Layout(root=root, network="testnet", owner=owner,
                    agent_id="default").ensure()
    layout.assert_outside_workspace()

    version = adapter.assert_supported_runtime()
    check(f"pinned Hermes {version} is installed", True)

    # A working copy as `agent init` would leave it.
    write_private(layout.soul_file, b"# Test agent\n\nAnswer briefly.\n")
    write_private(layout.user_memory_file, b"prefers concise answers\n")
    write_private(layout.agent_memory_file, b"")
    write_private(layout.agents_md, b"# Workspace\n")
    (layout.skills_dir / "demo").mkdir(parents=True, exist_ok=True)
    write_private(layout.skills_dir / "demo" / "SKILL.md", b"# demo skill\n")
    (layout.workspace / "reference.md").write_bytes(b"# reference\n")
    open_or_create(layout)
    check("a working copy was materialized", layout.state_db.is_file())

    with FakeModel() as model:
        # A bare "custom" provider is not routable; it must name a
        # custom_providers entry, whose key_env supplies the api key.
        device = {
            "model": "fake/test-model",
            "provider": "custom:faketest",
            "custom_providers": [{
                "name": "faketest",
                "base_url": model.base_url,
                "key_env": "FAKE_MODEL_KEY",
            }],
        }
        config = adapter.render_config(PortableConfig(model="fake/test-model"),
                                      layout, device)
        adapter.write_config(layout, config)
        check("the generated config selects the managed provider",
              config["memory"]["provider"] == "pubky")
        check("the generated config points terminal.cwd at the workspace",
              config["terminal"]["cwd"] == str(layout.workspace))
        check("no approval gate was disabled",
              not any(k in config for k in adapter.FORBIDDEN_ANYWHERE))

        written = adapter.write_plugin_shim(layout, "0.2.0")
        check("the discovery bridge was generated",
              (layout.plugin_dir / "__init__.py").is_file(), str(written))

        connection = {
            "root": str(root), "network": "testnet", "owner": owner,
            "agentId": "default", "hermesHome": str(layout.hermes_home),
            "workspace": str(layout.workspace), "runId": "check-run",
        }
        write_private(layout.connection_file,
                      json.dumps(connection, indent=2).encode("utf-8"))

        env = layout.child_environment()
        env.update({k.upper() if k.isupper() else k: v for k, v in {}.items()})
        env["FAKE_MODEL_KEY"] = "not-a-real-key"
        check("the grant is withheld from the child",
              "HERMES_PUBKY_GRANT_SECRET" not in env)
        check("the child is pointed at the dedicated home",
              env["HERMES_HOME"] == str(layout.hermes_home))

        # Discovery, as Hermes itself performs it, inside the child environment.
        probe = subprocess.run(
            [sys.executable, "-c", DISCOVERY_PROBE],
            capture_output=True, text=True, env=env, timeout=300,
            cwd=str(layout.workspace))
        check("Hermes discovers and loads the managed provider",
              probe.returncode == 0, probe.stdout + probe.stderr)
        report = json.loads(probe.stdout.strip().splitlines()[-1])
        check("it is registered under the name 'pubky'", report["name"] == "pubky")
        check("it reports itself available in a managed child",
              report["available"] is True)
        check("it exposes exactly the two workspace tools",
              report["tools"] == ["pubky_file_list", "pubky_file_fetch"])
        check("its prompt block does not repeat SOUL.md",
              "Test agent" not in report["block"])
        check("its prompt block does not repeat memory entries",
              "prefers concise answers" not in report["block"])
        check("its prompt block names the workspace",
              str(layout.workspace) in report["block"])

        # A real turn through the pinned CLI.
        argv = adapter.launch_command(query="Remember that I like short answers.",
                                      model="fake/test-model")
        turn = subprocess.run(argv, capture_output=True, text=True, env=env,
                              timeout=600, cwd=str(layout.workspace))
        check("a real Hermes turn completes", turn.returncode == 0,
              (turn.stdout or "")[-3000:] + (turn.stderr or "")[-3000:])

        import sqlite3

        conn = sqlite3.connect(f"file:{layout.state_db}?mode=ro", uri=True)
        try:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        check("the turn was recorded in the managed database",
              sessions >= 1 and messages >= 1, f"{sessions} sessions, {messages} messages")

        journal = Journal(layout.journal_file)
        try:
            requests = journal.claim_requests()
            check("the provider queued work for the supervisor",
                  any(r.kind in ("capture", "file_fetch") for r in requests),
                  f"kinds: {[r.kind for r in requests]}")
        finally:
            journal.close()

    check("the caller's real profile was never touched",
          not (real_home / "plugins" / "pubky").exists()
          if real_home.exists() else True)

    print("-" * 60)
    print(f"  {len(CHECKS)} checks passed\n")
    shutil.rmtree(root, ignore_errors=True)
    return 0


DISCOVERY_PROBE = r"""
import json, os
from plugins.memory import discover_memory_providers, load_memory_provider
from agent.memory_provider import MemoryProvider

names = [n for n, _d, _a in discover_memory_providers()]
provider = load_memory_provider("pubky")
assert provider is not None, f"pubky not discovered among {names}"
assert isinstance(provider, MemoryProvider)
available = provider.is_available()
provider.initialize("probe-session", hermes_home=os.environ["HERMES_HOME"],
                    platform="cli", agent_context="primary")
block = provider.system_prompt_block()
tools = [t["name"] for t in provider.get_tool_schemas()]
provider.shutdown()
print(json.dumps({"name": provider.name, "available": available,
                  "tools": tools, "block": block}))
"""


if __name__ == "__main__":
    raise SystemExit(main())
