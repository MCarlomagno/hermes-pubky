#!/usr/bin/env python3
"""The A-to-B acceptance scenario, through the real CLI, against a real testnet.

Machine A creates an agent and runs a real Hermes turn against a local fake
model. Machine B starts with nothing but the same grant, attaches, runs twice,
and edits a workspace file. Machine A runs again and picks all of it up. Then
the grant is revoked and shown to be dead. Every step is the shipped command
line; only Ring approval is replaced by the fixture's pre-authorized grant.

Needs a running, unused testnet fixture (revocation consumes its grant):

    TEST_PUBKY_CONNECTION_STRING=postgres://... \\
      cargo run --example testnet_fixture > /tmp/fixture.json &
    HERMES_PUBKY_TESTNET=1 python scripts/check_managed_handoff.py \\
      --fixture /tmp/fixture.json

Creates its own scratch roots and refuses to touch the caller's real profile.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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


def load_fixture(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"{path} does not contain fixture JSON yet")


def assistant_messages(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id")]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True,
                        help="JSON emitted by the testnet fixture")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if os.environ.get("HERMES_PUBKY_TESTNET") != "1":
        raise SystemExit("set HERMES_PUBKY_TESTNET=1 so the SDK uses the testnet")

    fixture = load_fixture(Path(args.fixture))
    owner, agent_id, grant = fixture["owner"], fixture["agentId"], fixture["agentGrant"]

    from fake_model import REPLY, FakeModel

    from hermes_pubky import cli
    from hermes_pubky.paths import layout_for, write_env_file
    from hermes_pubky.storage import AgentRemote

    real_home = (Path.home() / ".hermes").resolve()
    scratch = Path(tempfile.mkdtemp(prefix="hermes-pubky-handoff-"))
    if real_home == scratch or real_home in scratch.parents:
        raise SystemExit(f"refusing to run inside {real_home}")
    print(f"\nA-to-B handoff through the CLI against the testnet\n  scratch {scratch}\n"
          + "-" * 60)

    def run_cli(*argv: str) -> int:
        with patch.object(cli, "authorize", return_value=grant):
            return cli.main(["--network", "testnet", *argv])

    def head():
        # The homeserver keeps one session per grant, so every CLI step above
        # replaced this script's; open a fresh one to look.
        return AgentRemote.connect(grant, owner, agent_id).read_head()

    def use(name: str):
        """Point the CLI at one computer's management root."""
        os.environ["HERMES_PUBKY_HOME"] = str(scratch / name)
        return layout_for(owner, agent_id, network="testnet")

    def machine(name: str, model: FakeModel):
        layout = use(name).ensure()
        layout.device_config_file.write_text(yaml.safe_dump(model.device_config()))
        write_env_file(layout.secrets_file, {"FAKE_MODEL_KEY": "not-a-real-key"})
        return layout

    with FakeModel() as model:
        # -- machine A: create, run once ----------------------------------------
        a = machine("machine-a", model)
        check("machine A creates the agent", run_cli("agent", "init", agent_id) == 0)
        created = head()
        check("the first checkpoint is on the homeserver", created is not None)
        uri = AgentRemote.connect(grant, owner, agent_id).uri
        (a.workspace / "report.md").write_bytes(b"# Report\n\nFindings.\n")

        code = run_cli("run", agent_id, "--query", "Reply briefly.")
        check("machine A runs a real Hermes turn", code == 0, f"exit {code}")
        check("the model's reply is in A's conversation",
              any(REPLY in r for r in assistant_messages(a.state_db)))
        after_a = head()
        check("the run advanced the homeserver's checkpoint",
              after_a.snapshot_id != created.snapshot_id)

        # -- machine B: attach with nothing but the grant, run twice --------------
        b = machine("machine-b", model)
        check("machine B starts empty", not b.soul_file.exists())
        check("machine B attaches", run_cli("agent", "attach", uri) == 0)
        check("instructions recovered byte for byte",
              b.soul_file.read_bytes() == a.soul_file.read_bytes())
        check("the conversation recovered with A's reply",
              any(REPLY in r for r in assistant_messages(b.state_db)))
        check("workspace documents stay remote until asked for",
              not (b.workspace / "report.md").exists())

        code = run_cli("run", agent_id, "--query", "Reply briefly.")
        check("machine B runs on the attached copy", code == 0, f"exit {code}")
        code = run_cli("run", agent_id, "--query", "Reply briefly.")
        check("machine B runs a second time without conflict", code == 0, f"exit {code}")
        check("B's conversation holds all three turns",
              len(assistant_messages(b.state_db)) == 3,
              str(assistant_messages(b.state_db)))
        after_b = head()
        check("each run published a checkpoint",
              after_b.snapshot_id != after_a.snapshot_id)

        # -- machine B revises a workspace document and saves ----------------------
        check("B fetches the report on demand",
              run_cli("agent", "files", agent_id, "fetch", "report.md") == 0)
        (b.workspace / "report.md").write_bytes(b"# Report\n\nRevised on B.\n")
        check("B saves the revision", run_cli("agent", "sync", agent_id) == 0)

        # -- machine A picks everything up ------------------------------------------
        use("machine-a")
        code = run_cli("run", agent_id, "--query", "Reply briefly.")
        check("machine A runs again without conflict", code == 0, f"exit {code}")
        check("A recovered B's turns and added its own",
              len(assistant_messages(a.state_db)) == 4,
              str(assistant_messages(a.state_db)))
        check("A's stale report was refreshed, not republished",
              (a.workspace / "report.md").read_bytes() == b"# Report\n\nRevised on B.\n")
        code = run_cli("agent", "status", agent_id)
        check("status reports the agent synced", code == 0, f"exit {code}")

        # -- revocation, last: it consumes the fixture's grant -----------------------
        check("logout revokes the grant", run_cli("agent", "logout", agent_id) == 0)
        try:
            AgentRemote.connect(grant, owner, agent_id)
        except Exception as exc:  # noqa: BLE001 - the expected outcome
            check("the revoked grant is refused by the homeserver", True, str(exc))
        else:
            check("the revoked grant is refused by the homeserver", False,
                  "the retained secret still opened a session")

    print("-" * 60)
    print(f"  {len(CHECKS)} checks passed\n")
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
