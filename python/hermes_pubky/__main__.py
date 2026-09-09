"""``hermes-pubky`` console script — installs and inspects the Hermes shim.

Hermes discovers memory providers by scanning ``$HERMES_HOME/plugins/``, so
this command is what connects a pip install to a Hermes installation:

    uv pip install hermes-pubky
    hermes-pubky install
    hermes memory setup pubky
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-pubky",
        description="Install the Pubky portable-context plugin into Hermes.",
    )
    parser.add_argument("--version", action="version", version=f"hermes-pubky {__version__}")
    subs = parser.add_subparsers(dest="command")

    install = subs.add_parser("install", help="Write the plugin shim into $HERMES_HOME/plugins/")
    install.add_argument("--force", action="store_true", help="Overwrite existing shim files")
    install.add_argument("--home", default=None, help="Target a specific HERMES_HOME")

    uninstall = subs.add_parser("uninstall", help="Remove the plugin shim (data is kept)")
    uninstall.add_argument("--home", default=None, help="Target a specific HERMES_HOME")

    status = subs.add_parser("status", help="Show install and provider status")
    status.add_argument("--home", default=None, help="Target a specific HERMES_HOME")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    from pathlib import Path

    from .installer import install, is_installed, plugin_dir, uninstall
    from .paths import hermes_home

    args = build_parser().parse_args(argv)
    home = Path(args.home).expanduser() if getattr(args, "home", None) else hermes_home()

    if args.command == "install":
        target, written = install(home, force=args.force)
        print(f"\n  Plugin shim: {target}")
        if written:
            print(f"  Wrote: {', '.join(written)}")
        else:
            print("  Already installed (use --force to overwrite).")
        print("\n  Next: hermes memory setup pubky\n")
        return 0

    if args.command == "uninstall":
        removed = uninstall(home)
        print(f"\n  {'Removed' if removed else 'Nothing to remove at'} {plugin_dir(home)}\n")
        return 0

    if args.command == "status":
        from .remote import native_available
        from .status import format_status, status_snapshot

        print(f"\nhermes-pubky {__version__}\n" + "─" * 40)
        print(f"  HERMES_HOME       {home}")
        print(f"  plugin shim       {'installed' if is_installed(home) else 'NOT installed'}")
        print(f"  native extension  {'available' if native_available() else 'MISSING'}")
        print()
        print(format_status(status_snapshot()))
        return 0

    build_parser().print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
