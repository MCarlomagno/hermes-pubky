"""Homeserver-backed Hermes agents.

The homeserver holds the authoritative saved state of a managed agent. A local
Hermes installation reconstructs a working copy, runs the agent, and saves
changes back.

Submodules are imported lazily so `import hermes_pubky` stays cheap and works
without Hermes or the compiled extension present: the manifest, journal and
checkpoint logic are all testable on their own.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]


def __getattr__(name: str):
    if name in ("PubkyMemoryProvider", "register"):
        from . import provider

        return getattr(provider, name)
    if name in ("main", "build_parser"):
        from . import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
