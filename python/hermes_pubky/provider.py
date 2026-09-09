"""The Hermes memory provider.

v0.1 is a *context* provider, not a semantic-memory backend. It injects a
portable overlay into the system prompt and mirrors built-in memory writes to
a private Pubky document. It deliberately exposes no tools, does no recall,
and never stores conversations or tool results.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from . import config as cfg
from .outbox import Operation, Outbox
from .paths import GRANT_SECRET_ENV, PROVIDER_NAME, Layout, hermes_home
from .prompt import filter_local_duplicates, render_block
from .remote import Remote, fetch_public_context, native_available
from .schema import BaseContext, Profile, SchemaError
from .store import Store
from .sync import BackgroundSyncer, Syncer

logger = logging.getLogger("hermes_pubky")

# Agent contexts that must never write. A cron or flush run is not the user
# talking, and a subagent's memory belongs to its parent; mirroring either
# would pollute the portable overlay.
READ_ONLY_CONTEXTS = frozenset({"subagent", "cron", "flush"})

try:  # Hermes is present at runtime but not during unit tests.
    from agent.memory_provider import MemoryProvider  # type: ignore
except Exception:  # pragma: no cover - exercised only outside Hermes
    class MemoryProvider:  # type: ignore[no-redef]
        """Minimal stand-in so the module imports without Hermes installed."""


class PubkyMemoryProvider(MemoryProvider):
    """Portable agent context backed by a Pubky homeserver."""

    def __init__(self) -> None:
        self._home = None
        self._layout: Optional[Layout] = None
        self._store: Optional[Store] = None
        self._outbox: Optional[Outbox] = None
        self._syncer: Optional[Syncer] = None
        self._background: Optional[BackgroundSyncer] = None
        self._profile_id = cfg.DEFAULT_PROFILE_ID
        self._profile: Optional[Profile] = None
        self._context: Optional[BaseContext] = None
        self._stale = True
        self._read_only = False
        self._initialized = False

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """Config-and-deps check only — no network, per the provider contract."""
        if not native_available():
            return False
        layout = self._layout or Layout(hermes_home(), cfg.DEFAULT_PROFILE_ID)
        return bool(cfg.read_grant_secret(layout.env_file))

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Prepare local state and refresh from the homeserver within budget."""
        del session_id
        self._home = hermes_home(kwargs.get("hermes_home"))
        self._profile_id = cfg.profile_id()
        self._layout = Layout(self._home, self._profile_id)
        self._layout.ensure()
        self._store = Store(self._layout)
        self._outbox = Outbox(self._layout.outbox)

        agent_context = str(kwargs.get("agent_context") or "primary").lower()
        self._read_only = agent_context in READ_ONLY_CONTEXTS

        # Serve the cache immediately so a slow or dead homeserver can never
        # delay the first turn.
        self._load_from_cache()

        self._syncer = Syncer(
            store=self._store,
            outbox=self._outbox,
            profile_id=self._profile_id,
            connect=self._connect,
        )
        self._refresh_within_budget(cfg.DEFAULT_STARTUP_TIMEOUT)

        if not self._read_only:
            self._background = BackgroundSyncer(self._syncer)
            self._background.start()
            if self._outbox.count():
                self._background.request()

        self._initialized = True

    def shutdown(self) -> None:
        background = self._background
        self._background = None
        if background is not None:
            # One last attempt so a write made seconds before exit is not
            # stranded until the next session.
            background.request()
            background.stop()

    # -- prompt ------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._initialized or self._layout is None:
            return ""
        profile = self._profile
        user_entries: List[str] = []
        memory_entries: List[str] = []
        if profile is not None:
            user_entries = filter_local_duplicates(profile.user, self._layout.user_md)
            memory_entries = filter_local_duplicates(profile.memory, self._layout.memory_md)
        try:
            return render_block(
                self._context,
                user_entries,
                memory_entries,
                profile_id=self._profile_id,
                stale=self._stale,
            )
        except Exception as exc:  # noqa: BLE001 - never break prompt assembly
            logger.debug("hermes-pubky: prompt block failed: %s", cfg.redact(exc))
            return ""

    # -- no tools, no recall, no transcripts in v0.1 -----------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        del query, session_id
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        del user_content, assistant_content, kwargs

    # -- mirroring built-in memory writes ----------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue a built-in memory write for mirroring to the profile."""
        if self._read_only or self._outbox is None:
            return
        op = Operation.build(action, target, content, metadata)
        if op is None:
            return
        try:
            if not self._outbox.append(op):
                logger.warning(
                    "hermes-pubky: outbox is full; dropping mirrored %s to %s. "
                    "Run 'hermes pubky sync' to drain it.",
                    op.action, op.target,
                )
                return
        except OSError as exc:
            logger.debug("hermes-pubky: could not queue write: %s", cfg.redact(exc))
            return

        # Reflect the change locally right away so the next system prompt is
        # consistent with what the user just told the agent to remember.
        if self._profile is not None:
            from .outbox import apply_operation

            apply_operation(self._profile, op)

        if self._background is not None:
            self._background.request()

    # -- Hermes setup / status integration ---------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Fields for the generic wizard.

        ``post_setup`` handles the real flow (it needs an interactive auth
        round trip), but Hermes also uses this list to report what is missing
        in ``hermes memory status``.
        """
        return [
            {
                "key": "profile_id",
                "description": "Pubky profile id",
                "default": cfg.DEFAULT_PROFILE_ID,
                "required": True,
            },
            {
                "key": "grant_secret",
                "description": "Pubky grant secret (created by 'hermes pubky login')",
                "secret": True,
                "required": True,
                "env_var": GRANT_SECRET_ENV,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home_path: str) -> None:
        del hermes_home_path
        config = cfg.load_hermes_config()
        cfg.set_provider_config(config, {"profile_id": values.get("profile_id")
                                         or cfg.DEFAULT_PROFILE_ID})
        cfg.save_hermes_config(config)

    def post_setup(self, hermes_home_path: str, config: Dict[str, Any]) -> None:
        """Interactive setup, invoked by ``hermes memory setup pubky``."""
        from .setup_flow import run_setup

        run_setup(hermes_home_path, config)

    def get_status_config(self, provider_config: Dict[str, Any]) -> Dict[str, Any]:
        """Shown by ``hermes memory status`` — must never leak the secret."""
        from .status import status_snapshot

        return status_snapshot(provider_config)

    def backup_paths(self) -> List[str]:
        # Everything this plugin writes lives under HERMES_HOME, which
        # `hermes backup` already walks.
        return []

    # -- internals ---------------------------------------------------------

    def _connect(self) -> Remote:
        assert self._layout is not None
        secret = cfg.read_grant_secret(self._layout.env_file)
        return Remote.connect(secret, cfg.DEFAULT_STARTUP_TIMEOUT)

    def _load_from_cache(self) -> None:
        assert self._store is not None
        self._profile = self._store.load_profile()
        context, _meta = self._store.load_context()
        self._context = context
        self._stale = True

    def _refresh_within_budget(self, budget_secs: float) -> None:
        """Refresh from the homeserver, giving up the wait after ``budget_secs``.

        The work continues on its thread past the deadline — it just stops
        being something Hermes' startup waits on. Whatever it finishes lands
        in the cache for the next turn.
        """
        done = threading.Event()

        def worker() -> None:
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 - offline is normal
                logger.debug("hermes-pubky: startup refresh failed: %s", cfg.redact(exc))
            finally:
                done.set()

        thread = threading.Thread(target=worker, name="hermes-pubky-init", daemon=True)
        thread.start()
        done.wait(timeout=budget_secs)

    def _refresh(self) -> None:
        """Pull the profile, then the pinned base context."""
        assert self._store is not None and self._syncer is not None

        result = self._syncer.sync()
        if result.status == "conflict":
            logger.warning("hermes-pubky: %s", result.detail)

        profile = self._store.load_profile()
        if profile is not None:
            self._profile = profile
            self._stale = not result.ok

        self._refresh_context()

    def _refresh_context(self) -> None:
        """Load the approved base context, re-fetching only when the pin matches.

        A pinned context whose bytes have changed is *not* adopted: the user
        approved a specific document, and silently swapping in new third-party
        instructions would defeat the point of pinning. The cached copy keeps
        being used until ``hermes pubky base refresh``.
        """
        assert self._store is not None
        profile = self._profile
        ref = profile.base_context if profile is not None else None

        if ref is None:
            self._context = None
            return

        cached, meta = self._store.load_context()
        if cached is not None and meta is not None \
                and meta.url == ref.url and meta.sha256 == ref.sha256:
            self._context = cached
            return

        try:
            raw, digest = fetch_public_context(ref.url, cfg.DEFAULT_STARTUP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - keep the cached copy
            logger.debug("hermes-pubky: base context fetch failed: %s", cfg.redact(exc))
            self._context = cached
            return

        if digest != ref.sha256:
            logger.warning(
                "hermes-pubky: base context at %s no longer matches the approved "
                "hash; continuing with the approved copy. Run "
                "'hermes pubky base refresh' to review and re-approve.",
                ref.url,
            )
            self._context = cached
            return

        try:
            self._context = BaseContext.parse_bytes(raw)
            self._store.save_context(raw, ref.url, digest)
        except SchemaError as exc:
            logger.warning("hermes-pubky: base context is invalid: %s", exc)
            self._context = cached


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_memory_provider(PubkyMemoryProvider())
