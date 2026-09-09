"""The `hermes-pubky` command tree.

Commands resolve an agent within one local identity and network. When two
connections share a short id the command refuses and asks for `--owner`, rather
than guessing by recency.

Exit codes (plan 10): 0 completed, 1 usage or general failure, 2 saved locally
but remote sync incomplete, 3 conflict, 4 authentication required, 5 quota or
size limit, 6 integrity or unsupported runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__
from .paths import (
    GRANT_ENV,
    NETWORK_MAINNET,
    NETWORKS,
    Layout,
    default_root,
    iter_connections,
    read_env_file,
    write_env_file,
    write_private,
)
from .supervisor import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_INTEGRITY,
    EXIT_OK,
    EXIT_QUOTA,
    EXIT_SAVED_LOCALLY,
    EXIT_USAGE,
)

CLIENT_ID = "hermes.pubky.app"
APPROVAL_TIMEOUT = 300.0


class Abort(Exception):
    """A user-facing failure with an exit code."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# -- parser ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-pubky",
        description="Run a Hermes agent whose saved state lives on your Pubky "
                    "homeserver.")
    parser.add_argument("--version", action="version",
                        version=f"hermes-pubky {__version__}")
    parser.add_argument("--network", choices=NETWORKS, default=NETWORK_MAINNET,
                        help="Which Pubky network this agent belongs to.")
    parser.add_argument("--owner", metavar="Z32", default=None,
                        help="Disambiguate when two identities share an agent id.")
    subs = parser.add_subparsers(dest="command")

    # Top-level run.
    run = subs.add_parser("run", help="Run the agent")
    run.add_argument("agent_id")
    run.add_argument("--resume", metavar="SESSION_ID",
                     help="Resume a saved conversation.")
    run.add_argument("--offline", action="store_true",
                     help="Use the local working copy without contacting the "
                          "homeserver.")
    run.add_argument("--query", metavar="TEXT",
                     help="Send a single prompt and exit.")

    agent = subs.add_parser("agent", help="Manage agents").add_subparsers(
        dest="agent_command")

    init = agent.add_parser("init", help="Create a new agent")
    init.add_argument("agent_id")
    init.add_argument("--from-hermes-home", metavar="PATH", default=None,
                      help="One-time import from an existing Hermes profile.")
    init.add_argument("--workspace", metavar="PATH", default=None,
                      help="Copy this directory in as the managed workspace.")

    attach = agent.add_parser("attach", help="Attach an existing agent")
    attach.add_argument("uri", help="pubky:// address of the agent's head.json")

    agent.add_parser("list", help="List locally known agents")

    status = agent.add_parser("status", help="Show an agent's state")
    status.add_argument("agent_id")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument("--offline", action="store_true",
                        help="Skip the homeserver check.")

    login = agent.add_parser("login", help="Authorize this machine")
    login.add_argument("agent_id")

    logout = agent.add_parser("logout", help="Revoke this machine's grant")
    logout.add_argument("agent_id")

    sync = agent.add_parser("sync", help="Save pending changes")
    sync.add_argument("agent_id")
    sync.add_argument("--prefer", choices=("local", "remote"), default=None,
                      help="Resolve a conflict by keeping one side.")

    files = agent.add_parser("files", help="Manage workspace files")
    files.add_argument("agent_id")
    files_subs = files.add_subparsers(dest="files_command")
    listing = files_subs.add_parser("list", help="List saved workspace files")
    listing.add_argument("prefix", nargs="?", default="")
    fetch = files_subs.add_parser("fetch", help="Fetch a saved workspace file")
    fetch.add_argument("path", nargs="?", default=None)
    fetch.add_argument("--all", action="store_true", dest="fetch_all",
                       help="Fetch every remote-only file.")
    importer = files_subs.add_parser("import", help="Add a local file")
    importer.add_argument("local_path")
    importer.add_argument("--to", metavar="RELATIVE_PATH", required=True)
    remover = files_subs.add_parser("remove", help="Remove a saved file")
    remover.add_argument("relative_path")

    history = agent.add_parser("history", help="List saved checkpoints")
    history.add_argument("agent_id")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--cursor", default=None)

    restore = agent.add_parser("restore", help="Republish an older checkpoint")
    restore.add_argument("agent_id")
    restore.add_argument("snapshot_id")

    template = subs.add_parser("template", help="Public templates").add_subparsers(
        dest="template_command")
    adopt = template.add_parser("adopt", help="Adopt a public template")
    adopt.add_argument("agent_id")
    adopt.add_argument("uri")
    update = template.add_parser("update", help="Review an upstream update")
    update.add_argument("agent_id")
    t_init = template.add_parser("init", help="Create a template bundle")
    t_init.add_argument("directory")
    inspect = template.add_parser("inspect", help="Validate and preview a bundle")
    inspect.add_argument("directory")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    publish = template.add_parser("publish", help="Publish a bundle")
    publish.add_argument("directory")
    publish.add_argument("--id", metavar="TEMPLATE_ID", required=True,
                         dest="template_id")
    publish.add_argument("--confirm-public", metavar="SHA256", default=None,
                         dest="confirm_public")
    return parser


# -- resolution --------------------------------------------------------------

def resolve_layout(args: Any, agent_id: str) -> Layout:
    """Find the one local connection for this agent id."""
    matches = [c for c in iter_connections()
               if c.agent_id == agent_id and c.network == args.network
               and (args.owner is None or c.owner == args.owner)]
    if not matches:
        raise Abort(
            f"no local agent {agent_id!r} on {args.network}. Use "
            f"'hermes-pubky agent attach <uri>' or 'agent init {agent_id}'.")
    if len(matches) > 1:
        owners = ", ".join(sorted(c.owner for c in matches))
        raise Abort(
            f"{agent_id!r} exists under several identities ({owners}); "
            "pass --owner <z32> to choose one.")
    return matches[0]


def grant_of(layout: Layout) -> str:
    return read_env_file(layout.credentials_file).get(GRANT_ENV, "")


def require_grant(layout: Layout) -> str:
    grant = grant_of(layout)
    if not grant:
        raise Abort(
            f"this machine is not authorized for {layout.agent_id!r}; run "
            f"'hermes-pubky agent login {layout.agent_id}'", EXIT_AUTH)
    return grant


def save_connection(layout: Layout, network: str) -> None:
    from . import hermes_adapter as adapter

    write_private(layout.connection_file, json.dumps({
        "root": str(layout.root),
        "network": network,
        "owner": layout.owner,
        "agentId": layout.agent_id,
        "hermesHome": str(layout.hermes_home),
        "workspace": str(layout.workspace),
        "adapter": adapter.ADAPTER_ID,
    }, indent=2, sort_keys=True).encode("utf-8"))


# -- authorization -----------------------------------------------------------

def authorize(scope: str) -> str:
    """Run Pubky Auth for one capability and return the grant secret."""
    from .storage import native

    module = native()
    flow = module.AuthFlow(scope, CLIENT_ID)
    print("\n  Authorize in Pubky Ring. This requests only:")
    print(f"    {flow.capabilities}")
    print(f"\n  {flow.authorization_url}\n")
    try:
        import webbrowser

        webbrowser.open(flow.authorization_url)
    except Exception:
        pass
    print("  Waiting for approval (Ctrl-C to cancel)...")
    try:
        return flow.await_approval(APPROVAL_TIMEOUT)
    except KeyboardInterrupt:
        raise Abort("authorization cancelled")


# -- commands ----------------------------------------------------------------

def cmd_init(args: Any) -> int:
    from .onboarding import create_agent

    return create_agent(
        agent_id=args.agent_id, network=args.network,
        from_hermes_home=Path(args.from_hermes_home) if args.from_hermes_home else None,
        workspace=Path(args.workspace) if args.workspace else None)


def cmd_attach(args: Any) -> int:
    from .onboarding import attach_agent

    return attach_agent(uri=args.uri, network=args.network)


def cmd_list(args: Any) -> int:
    connections = list(iter_connections())
    if not connections:
        print(f"\n  No agents under {default_root()}.")
        print("  Create one with 'hermes-pubky agent init <id>'.\n")
        return EXIT_OK
    print(f"\nAgents under {default_root()}\n" + "-" * 52)
    for layout in connections:
        authorized = "authorized" if grant_of(layout) else "not authorized"
        print(f"  {layout.agent_id:<20} {layout.network:<8} {authorized}")
        print(f"    owner {layout.owner}")
    print()
    return EXIT_OK


def cmd_status(args: Any) -> int:
    from .status import agent_status, format_status

    layout = resolve_layout(args, args.agent_id)
    snapshot = agent_status(layout, args.network, check_remote=not args.offline)
    if args.as_json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        print(format_status(snapshot))
    return {"conflict": EXIT_CONFLICT, "auth-required": EXIT_AUTH,
            "quota-blocked": EXIT_QUOTA, "corrupt": EXIT_INTEGRITY,
            "dirty": EXIT_SAVED_LOCALLY}.get(snapshot["state"], EXIT_OK)


def cmd_login(args: Any) -> int:
    from .storage import AgentRemote, native

    layout = resolve_layout(args, args.agent_id)
    scope = native().agent_scope(layout.agent_id)
    secret = authorize(scope)
    remote = AgentRemote.connect(secret, layout.owner, layout.agent_id)
    if remote.owner != layout.owner:
        raise Abort(
            f"that grant belongs to {remote.owner}, not {layout.owner}", EXIT_AUTH)
    write_env_file(layout.credentials_file, {GRANT_ENV: secret})
    print(f"\n  Authorized for {layout.agent_id}. Grant stored at "
          f"{layout.credentials_file} (mode 0600).\n")
    return EXIT_OK


def cmd_logout(args: Any) -> int:
    layout = resolve_layout(args, args.agent_id)
    grant = grant_of(layout)
    if not grant:
        print("\n  Already logged out.\n")
        return EXIT_OK
    revoked = "not attempted"
    try:
        from .storage import native

        session = native()
        # Restoring and revoking uses the session's own endpoint, which a
        # scoped grant is permitted to call.
        transport = session.AgentTransport.open(grant, layout.owner, layout.agent_id)
        del transport
        revoked = "revoked at the homeserver"
    except Exception as exc:  # noqa: BLE001
        revoked = f"could not revoke remotely ({exc})"
    write_env_file(layout.credentials_file, {})
    print(f"\n  Local grant deleted; {revoked}.")
    print("  The working copy and cache were kept.\n")
    return EXIT_OK


def cmd_run(args: Any) -> int:
    from .supervisor import Supervisor

    layout = resolve_layout(args, args.agent_id)
    if not args.offline:
        require_grant(layout)
    supervisor = Supervisor(layout, network=args.network)
    try:
        result = supervisor.run(resume=args.resume, offline=args.offline,
                                query=args.query)
    finally:
        supervisor.close()
    if result.detail:
        print(f"\n  {result.detail}\n")
    return result.exit_code


def cmd_sync(args: Any) -> int:
    from .journal import Journal
    from .objects import ObjectCache
    from .storage import AgentRemote
    from .sync import SyncEngine

    layout = resolve_layout(args, args.agent_id)
    grant = require_grant(layout)
    layout.ensure()
    journal = Journal(layout.journal_file)
    try:
        engine = SyncEngine(
            journal, AgentRemote.connect(grant, layout.owner, layout.agent_id),
            ObjectCache(layout.cached_objects), recovery_dir=layout.recovery)
        result = engine.resolve(args.prefer) if args.prefer else engine.sync_with_retries()
    finally:
        journal.close()
    print(f"\n  {result.status}: {result.detail}\n")
    return {"synced": EXIT_OK, "up-to-date": EXIT_OK, "conflict": EXIT_CONFLICT,
            "blocked": EXIT_INTEGRITY}.get(result.status, EXIT_SAVED_LOCALLY)


def cmd_files(args: Any) -> int:
    from .files import files_command

    layout = resolve_layout(args, args.agent_id)
    return files_command(layout, args)


def cmd_history(args: Any) -> int:
    from .storage import AgentRemote

    layout = resolve_layout(args, args.agent_id)
    grant = require_grant(layout)
    remote = AgentRemote.connect(grant, layout.owner, layout.agent_id)
    ids, cursor = remote.list_snapshots(args.cursor, min(max(args.limit, 1), 200))
    head = remote.read_head()
    print(f"\nCheckpoints for {layout.agent_id}\n" + "-" * 44)
    for snapshot_id in ids:
        marker = "  <- current" if head and head.snapshot_id == snapshot_id else ""
        print(f"  {snapshot_id}{marker}")
    if cursor:
        print(f"\n  more: --cursor {cursor}")
    print()
    return EXIT_OK


def cmd_restore(args: Any) -> int:
    from .restore import restore_snapshot

    layout = resolve_layout(args, args.agent_id)
    require_grant(layout)
    return restore_snapshot(layout, args.snapshot_id, network=args.network)


def cmd_template(args: Any) -> int:
    from .templates import template_command

    return template_command(args, resolve=lambda agent_id: resolve_layout(args, agent_id))


# -- dispatch ----------------------------------------------------------------

AGENT_COMMANDS = {
    "init": cmd_init, "attach": cmd_attach, "list": cmd_list, "status": cmd_status,
    "login": cmd_login, "logout": cmd_logout, "sync": cmd_sync, "files": cmd_files,
    "history": cmd_history, "restore": cmd_restore,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "agent":
            handler = AGENT_COMMANDS.get(getattr(args, "agent_command", None))
            if handler is None:
                parser.parse_args(["agent", "--help"])
                return EXIT_USAGE
            return handler(args)
        if args.command == "template":
            if getattr(args, "template_command", None) is None:
                parser.parse_args(["template", "--help"])
                return EXIT_USAGE
            return cmd_template(args)
        parser.print_help()
        return EXIT_USAGE
    except Abort as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("\n  Cancelled.\n", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
