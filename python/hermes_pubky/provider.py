"""The managed lifecycle bridge Hermes loads.

This provider does no remote I/O and injects no recalled content. Hermes
already loads `SOUL.md`, the memory files and skill definitions from the
dedicated profile, so repeating them here would duplicate prompt content. What
it does is tell the supervisor when saved state has changed, and give the model
two tools for the workspace catalogue.

A direct, unmanaged invocation reports that the user should start through
`hermes-pubky run`. It never falls back to an overlay or to prompting from a
remote document.

Reference: implementation plan section 7.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes_pubky.provider")

PROVIDER_NAME = "pubky"

# Contexts that must not schedule remote writes of their own. A cron or flush
# run is not the user talking, and a subagent's memory belongs to its parent.
NONPRIMARY_CONTEXTS = frozenset({"subagent", "cron", "flush"})

REQUEST_FILE_FETCH = "file_fetch"
REQUEST_CAPTURE = "capture"

MAX_LIST_LIMIT = 200

try:  # Hermes is present at runtime but not in the unit suite.
    from agent.memory_provider import MemoryProvider  # type: ignore
except Exception:  # pragma: no cover - exercised only outside Hermes
    class MemoryProvider:  # type: ignore[no-redef]
        """Minimal stand-in so this module imports without Hermes."""


class PubkyMemoryProvider(MemoryProvider):
    """Notifies the supervisor and exposes the workspace catalogue."""

    def __init__(self) -> None:
        self._journal = None
        self._layout = None
        self._connection = None
        self._session_id = ""
        self._run_id = ""
        self._read_only = False
        self._ready = False

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    # -- availability -------------------------------------------------------

    def is_available(self) -> bool:
        """Managed launch plus a validated local connection. No network.

        A downloaded configuration file alone must not activate an arbitrary
        local path, so both the launch environment and the connection file have
        to agree.
        """
        from .paths import is_managed_child, managed_connection_file

        if not is_managed_child():
            return False
        connection = managed_connection_file()
        if connection is None or not connection.is_file():
            return False
        try:
            return bool(self._load_connection(connection))
        except Exception:
            return False

    @staticmethod
    def _load_connection(path: Path) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        for key in ("network", "owner", "agentId", "hermesHome", "workspace"):
            if not isinstance(parsed.get(key), str) or not parsed[key]:
                return None
        return parsed

    # -- lifecycle ----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Bind to the connection and record the session and context."""
        from .journal import Journal
        from .paths import Layout, managed_connection_file

        self._session_id = session_id or ""
        agent_context = str(kwargs.get("agent_context") or "primary").lower()
        self._read_only = agent_context in NONPRIMARY_CONTEXTS

        connection_file = managed_connection_file()
        if connection_file is None:
            logger.warning(
                "hermes-pubky: this Hermes was not started by 'hermes-pubky run'; "
                "managed state will not be saved. Start it with "
                "'hermes-pubky run <agent-id>'.")
            return
        connection = self._load_connection(connection_file)
        if connection is None:
            logger.warning("hermes-pubky: %s is not a usable connection file",
                           connection_file)
            return

        supplied_home = kwargs.get("hermes_home")
        expected = Path(connection["hermesHome"])
        if supplied_home and Path(supplied_home).resolve() != expected.resolve():
            logger.warning(
                "hermes-pubky: this process is using %s but the connection "
                "describes %s; not saving managed state",
                supplied_home, expected)
            return

        self._connection = connection
        self._layout = Layout(root=Path(connection["root"]),
                              network=connection["network"],
                              owner=connection["owner"],
                              agent_id=connection["agentId"])
        self._run_id = str(connection.get("runId") or "")
        try:
            self._journal = Journal(self._layout.journal_file)
        except Exception as exc:  # noqa: BLE001 - never break Hermes startup
            logger.warning("hermes-pubky: could not open the journal: %s", exc)
            return
        self._ready = True

    def shutdown(self) -> None:
        """Hint a final capture. The supervisor performs the real one."""
        self._request_capture("shutdown")
        journal, self._journal = self._journal, None
        if journal is not None:
            journal.close()
        self._ready = False

    # -- prompt -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """A short status and workspace notice. No recalled content.

        Hermes already injects SOUL.md, the memory files and skill definitions
        from this profile. Repeating them would double the prompt.
        """
        if not self._ready or self._layout is None:
            return ""
        try:
            catalogue = self._catalogue()
        except Exception:  # noqa: BLE001
            return ""
        remote_only = [entry for entry in catalogue if not entry["materialized"]]
        lines = [
            "## Managed workspace",
            "",
            f"This agent's saved state lives on the user's Pubky homeserver. Its "
            f"workspace is `{self._layout.workspace}`; files there travel with the "
            "agent to other computers.",
        ]
        if remote_only:
            lines += [
                "",
                f"{len(remote_only)} workspace file(s) are saved remotely but not "
                "present locally. Use `pubky_file_list` to see the catalogue and "
                "`pubky_file_fetch` to bring one into the workspace before reading "
                "it. Do not create a placeholder in its place.",
            ]
        lines += [
            "",
            "Historical paths in this conversation may refer to a previous "
            "computer. The workspace path above is the current one.",
        ]
        return "\n".join(lines) + "\n"

    # -- no recall, no transcripts ------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        del query, session_id
        return ""

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        del messages
        self._request_capture("pre-compress")
        return ""

    # -- change notifications ----------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mark the memory file dirty. The file itself is what gets captured."""
        del content
        if not self._ready or self._read_only or self._journal is None:
            return
        if action not in ("add", "replace", "remove"):
            return
        logical = {"user": "profile/memories/USER.md",
                   "memory": "profile/memories/MEMORY.md"}.get(target)
        if logical is None:
            return
        try:
            self._journal.mark_dirty(logical)
            self._journal.bump_generation()
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes-pubky: could not mark %s dirty: %s", logical, exc)
        self._request_capture(f"memory {action}", metadata=metadata)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None
                  ) -> None:
        """Ask for a capture of completed saved work.

        The two flattened strings are deliberately unused: the conversation is
        recovered from the database snapshot, not reconstructed from them.
        """
        del user_content, assistant_content, messages
        if session_id:
            self._session_id = session_id
        self._request_capture("turn complete")

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False,
                          **kwargs: Any) -> None:
        """Track the current session and mark the conversation changed."""
        del parent_session_id, reset, kwargs
        self._session_id = new_session_id or self._session_id
        if not self._ready or self._journal is None:
            return
        try:
            self._journal.mark_dirty("conversations/state.sqlite3")
            self._journal.bump_generation()
        except Exception:  # noqa: BLE001
            pass
        self._request_capture("rewind" if rewound else "session switch")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        del messages
        self._request_capture("session end")

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "",
                      **kwargs: Any) -> None:
        del task, result, child_session_id, kwargs
        # A subagent's own writes land in this profile, so the parent run still
        # captures them; nothing extra to record here.

    # -- tools --------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._ready:
            return []
        return [
            {
                "name": "pubky_file_list",
                "description": (
                    "List this agent's saved workspace files, including ones "
                    "stored remotely that are not present locally. Reads a "
                    "local catalogue; does not contact the homeserver."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Optional workspace-relative prefix."},
                        "cursor": {"type": "string",
                                   "description": "Continue a previous listing."},
                        "limit": {"type": "integer",
                                  "description": f"Maximum entries, up to {MAX_LIST_LIMIT}."},
                    },
                    "required": [],
                },
            },
            {
                "name": "pubky_file_fetch",
                "description": (
                    "Bring one saved workspace file into the local workspace so "
                    "ordinary file tools can read it. Returns pending if the "
                    "transfer is still running."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Workspace-relative path to fetch."},
                    },
                    "required": ["path"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs: Any) -> str:
        del kwargs
        if not self._ready:
            return json.dumps({
                "error": "this agent is not running under 'hermes-pubky run'"})
        try:
            if tool_name == "pubky_file_list":
                return json.dumps(self._tool_list(args))
            if tool_name == "pubky_file_fetch":
                return json.dumps(self._tool_fetch(args))
        except Exception as exc:  # noqa: BLE001 - a tool must return, not raise
            return json.dumps({"error": str(exc)})
        return json.dumps({"error": f"unknown tool {tool_name}"})

    def _tool_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        prefix = str(args.get("path") or "").strip().lstrip("/")
        limit = args.get("limit")
        limit = MAX_LIST_LIMIT if not isinstance(limit, int) or limit <= 0 \
            else min(limit, MAX_LIST_LIMIT)
        cursor = str(args.get("cursor") or "")

        entries = [e for e in self._catalogue()
                   if e["path"].startswith(prefix)]
        if cursor:
            entries = [e for e in entries if e["path"] > cursor]
        page = entries[:limit]
        return {
            "files": page,
            "next_cursor": page[-1]["path"] if len(page) == limit else None,
            "workspace": str(self._layout.workspace),
        }

    def _tool_fetch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import hashlib

        from .journal import REQUEST_DONE, REQUEST_FAILED

        relative = str(args.get("path") or "").strip().lstrip("/")
        if not relative:
            return {"error": "path is required"}
        logical = f"workspace/{relative}"
        record = self._journal.get_materialized(logical)
        if record is None:
            return {"error": f"{relative} is not one of this agent's saved files"}
        local = self._layout.workspace / relative
        if record.present and local.is_file() and not record.dirty:
            return {"status": "present", "path": str(local),
                    "size": local.stat().st_size, "sha256": record.base_hash}
        if record.dirty:
            return {"error": f"{relative} has local changes; fetching would "
                             "overwrite them"}

        # A stable id makes the request idempotent across retries.
        request_id = hashlib.sha256(
            f"{REQUEST_FILE_FETCH}:{logical}".encode("utf-8")).hexdigest()[:32]
        request = self._journal.enqueue_request(
            REQUEST_FILE_FETCH, {"logicalPath": logical}, self._run_id,
            request_id=request_id)
        if request.state == REQUEST_DONE:
            return {"status": "present", **request.result}
        if request.state == REQUEST_FAILED:
            return {"status": "failed", **request.result}
        return {"status": "pending", "request_id": request.id,
                "detail": "the launcher is fetching this file; ask again shortly"}

    # -- internals ----------------------------------------------------------

    def _catalogue(self) -> List[Dict[str, Any]]:
        """The workspace catalogue from the local inventory."""
        if self._journal is None or self._layout is None:
            return []
        out: List[Dict[str, Any]] = []
        for record in self._journal.list_materialized():
            if not record.logical_path.startswith("workspace/"):
                continue
            if record.explicit_delete:
                continue
            relative = record.logical_path[len("workspace/"):]
            local = self._layout.workspace / relative
            size = local.stat().st_size if local.is_file() else None
            out.append({
                "path": relative,
                "materialized": bool(record.present and local.is_file()),
                "dirty": bool(record.dirty),
                "size": size,
                "sha256": record.base_hash or None,
            })
        return sorted(out, key=lambda entry: entry["path"])

    def _request_capture(self, reason: str,
                         metadata: Optional[Dict[str, Any]] = None) -> None:
        """Hint that saved state changed. Best effort, never blocking."""
        if not self._ready or self._read_only or self._journal is None:
            return
        try:
            self._journal.enqueue_request(
                REQUEST_CAPTURE,
                {"reason": reason, "sessionId": self._session_id,
                 "writeOrigin": (metadata or {}).get("write_origin", "")},
                self._run_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes-pubky: could not queue a capture: %s", exc)

    # -- Hermes setup integration -------------------------------------------

    def get_tool_schemas_count(self) -> int:  # pragma: no cover - convenience
        return len(self.get_tool_schemas())

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """No wizard: setup happens through `hermes-pubky agent init|attach`."""
        return []

    def backup_paths(self) -> List[str]:
        """The management root sits outside HERMES_HOME, so declare it."""
        from .paths import default_root

        return [str(default_root())]


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_memory_provider(PubkyMemoryProvider())
