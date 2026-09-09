"""Portable Hermes agent context over Pubky.

Public surface:

* :class:`~hermes_pubky.provider.PubkyMemoryProvider` — the Hermes memory provider
* :func:`~hermes_pubky.cli.register_cli` — the ``hermes pubky`` command tree
* :mod:`hermes_pubky.installer` — writes the plugin shim Hermes discovers

Submodules are imported lazily so that ``import hermes_pubky`` stays cheap and
works without Hermes (or the compiled extension) present — the schema, outbox
and sync logic are all unit-testable on their own.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["PubkyMemoryProvider", "register", "register_cli", "pubky_command", "__version__"]


def __getattr__(name: str):
    if name in ("PubkyMemoryProvider", "register"):
        from . import provider

        return getattr(provider, name)
    if name in ("register_cli", "pubky_command"):
        from . import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
