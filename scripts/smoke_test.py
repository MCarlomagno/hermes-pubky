#!/usr/bin/env python3
"""Post-install smoke test for a built wheel.

Confirms the wheel imports, the compiled extension loaded, the protocol
constants are what the package promises, and the path policy survived
packaging. Run against an installed package, not the source tree.
"""

from __future__ import annotations

import platform
import sys

PK = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


def main() -> int:
    import hermes_pubky
    from hermes_pubky import _native

    assert _native.PROTOCOL_VERSION == "v2"
    assert _native.APP_NAMESPACE == "hermes.pubky.app"
    assert _native.MAX_OBJECT_BYTES == 1024 * 1024
    assert _native.MAX_HEAD_BYTES == 4 * 1024
    assert _native.MAX_SNAPSHOT_BYTES == 1024 * 1024
    assert hermes_pubky.__version__ == _native.__version__, (
        f"{hermes_pubky.__version__} != {_native.__version__}")

    # Addresses and scopes.
    uri = _native.agent_uri(PK, "default")
    assert uri.endswith("/priv/hermes.pubky.app/v2/agents/default/head.json"), uri
    assert _native.parse_agent_uri(uri) == (PK, "default")
    assert _native.agent_scope("default") == \
        "/priv/hermes.pubky.app/v2/agents/default/:rw"
    assert _native.template_scope("researcher") == \
        "/pub/hermes.pubky.app/v2/templates/researcher/:rw"

    # The policy must survive packaging, not just live in the source tree.
    for hostile in (
        f"pubky://{PK}/priv/hermes.pubky.app/v1/profiles/default.json",
        f"pubky://{PK}/pub/hermes.pubky.app/v2/templates/x/head.json",
        f"pubky://{PK}/priv/hermes.pubky.app/v2/agents/../escape/head.json",
    ):
        try:
            _native.parse_agent_uri(hostile)
        except _native.PubkyValidationError:
            pass
        else:
            raise AssertionError(f"address policy not enforced for {hostile}")

    # Every module the launcher needs must be present in the wheel.
    import importlib

    for name in ("cli", "database", "files", "hermes_adapter", "journal",
                 "models", "objects", "onboarding", "paths", "projection",
                 "provider", "restore", "status", "storage", "supervisor",
                 "sync", "templates"):
        importlib.import_module(f"hermes_pubky.{name}")

    # Declared runtime dependencies must be resolvable in a Hermes-free
    # install; the config writer has no fallback if pyyaml is absent.
    import yaml

    assert yaml.safe_load(yaml.safe_dump({"model": "x"})) == {"model": "x"}

    from hermes_pubky import cli, models, provider

    assert cli.build_parser() is not None
    assert provider.PubkyMemoryProvider().name == "pubky"
    assert models.SCHEMA_VERSION == 2

    print(
        f"smoke test ok: hermes-pubky {hermes_pubky.__version__} "
        f"on {platform.system()}/{platform.machine()} "
        f"python {platform.python_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
