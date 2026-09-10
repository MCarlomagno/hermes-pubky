#!/usr/bin/env python3
"""Run a real Hermes turn through the real launcher, with no homeserver.

Creates its own scratch root, seeds a working copy, then calls
`Supervisor.run(offline=True)`: the lock, the startup seal, the rendered
configuration, the discovery bridge, the watcher, the real `hermes_cli.main`
child against a local fake model, the plugin's capture hints, the final
checkpoint. Afterwards it wipes the working copy and restores it from the local
cache through the same install path a run uses.

    python scripts/check_managed_hermes_integration.py

Refuses to run if any path resolves inside the caller's real Hermes profile.
The A-to-B handoff script covers the same flow against a live homeserver.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

CHECKS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"         {detail}")
        sys.exit(1)
    CHECKS.append(label)


def assistant_messages(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id")]
    finally:
        conn.close()


def main() -> int:
    from fake_model import REPLY, FakeModel

    from hermes_pubky import hermes_adapter as adapter
    from hermes_pubky.database import open_or_create
    from hermes_pubky.journal import Journal
    from hermes_pubky.models import DATABASE_PATH, SnapshotRef
    from hermes_pubky.paths import Layout, write_env_file, write_private
    from hermes_pubky.supervisor import EXIT_SAVED_LOCALLY, Supervisor

    real_home = (Path.home() / ".hermes").resolve()
    root = Path(tempfile.mkdtemp(prefix="hermes-pubky-managed-"))
    if real_home == root or real_home in root.parents:
        raise SystemExit(f"refusing to run inside {real_home}")

    print(f"\nManaged Hermes integration check\n  root {root}\n" + "-" * 60)
    owner = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
    layout = Layout(root=root, network="testnet", owner=owner,
                    agent_id="default").ensure()

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
        layout.device_config_file.write_text(yaml.safe_dump(model.device_config()))
        write_env_file(layout.secrets_file, {"FAKE_MODEL_KEY": "not-a-real-key"})

        supervisor = Supervisor(layout, network="testnet")
        result = supervisor.run(query="Remember that I like short answers.",
                                offline=True)
        check("the launcher ran to completion offline",
              result.exit_code == EXIT_SAVED_LOCALLY and result.child_exit_code == 0,
              f"exit {result.exit_code}, child {result.child_exit_code}: {result.detail}")

        config = yaml.safe_load(layout.hermes_config_file.read_text())
        check("the generated config selects the managed provider",
              config["memory"]["provider"] == "pubky")
        check("the generated config points terminal.cwd at the workspace",
              config["terminal"]["cwd"] == str(layout.workspace))
        check("no approval gate was disabled",
              not any(k in config for k in adapter.FORBIDDEN_ANYWHERE))
        check("the discovery bridge was generated",
              (layout.plugin_dir / "__init__.py").is_file())

        replies = assistant_messages(layout.state_db)
        check("the model's reply reached the conversation database",
              any(REPLY in r for r in replies), f"assistant messages: {replies}")

        with Journal(layout.journal_file) as journal:
            pending = journal.active_checkpoints()
            base_id = journal.get_setting("base_snapshot_id")
            session_id = journal.get_setting("last_session_id")
            requests = journal._rows("SELECT kind, state FROM requests")
        check("the run left checkpoints to publish", len(pending) >= 2,
              f"{len(pending)} pending")
        check("the provider's capture hints were served",
              any(r["kind"] == "capture" and r["state"] == "done" for r in requests),
              str([tuple(r) for r in requests]))
        check("the session was recorded", bool(session_id))

        from hermes_pubky.models import Snapshot

        base = Snapshot.parse(layout.cached_snapshot(base_id).read_bytes())
        check("the final checkpoint names the session", base.last_session_id == session_id)
        check("the final checkpoint carries the conversation",
              DATABASE_PATH in base.files and base.files[DATABASE_PATH].size > 0)

        # Discovery, as Hermes itself performs it, inside the child environment.
        env = layout.child_environment()
        env["FAKE_MODEL_KEY"] = "not-a-real-key"
        check("the grant is withheld from the child",
              "HERMES_PUBKY_GRANT_SECRET" not in env)
        probe = subprocess.run(
            [sys.executable, "-c", DISCOVERY_PROBE],
            capture_output=True, text=True, env=env, timeout=300,
            cwd=str(layout.workspace))
        check("Hermes discovers and loads the managed provider",
              probe.returncode == 0, probe.stdout + probe.stderr)
        report = json.loads(probe.stdout.strip().splitlines()[-1])
        check("it exposes exactly the two workspace tools",
              report["tools"] == ["pubky_file_list", "pubky_file_fetch"])
        check("its prompt block does not repeat SOUL.md or memory entries",
              "Test agent" not in report["block"]
              and "prefers concise answers" not in report["block"])
        check("its prompt block names the workspace",
              str(layout.workspace) in report["block"])

        # Restoration through the same path a run uses, from the local cache.
        layout.soul_file.unlink()
        layout.agent_memory_file.unlink()
        layout.state_db.unlink()
        with supervisor.session() as s:
            s.install(base)
        check("instructions restore from the sealed checkpoint",
              layout.soul_file.read_bytes() == b"# Test agent\n\nAnswer briefly.\n")
        check("the conversation restores with the model's reply",
              any(REPLY in r for r in assistant_messages(layout.state_db)))
        with Journal(layout.journal_file) as journal:
            check("restoring did not disturb the pending checkpoints",
                  len(journal.active_checkpoints()) == len(pending))

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
assert provider.is_available()
provider.initialize("probe-session", hermes_home=os.environ["HERMES_HOME"],
                    platform="cli", agent_context="primary")
block = provider.system_prompt_block()
tools = [t["name"] for t in provider.get_tool_schemas()]
provider.shutdown()
print(json.dumps({"name": provider.name, "tools": tools, "block": block}))
"""


if __name__ == "__main__":
    raise SystemExit(main())
