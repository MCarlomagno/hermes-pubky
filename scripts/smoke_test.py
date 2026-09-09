#!/usr/bin/env python3
"""Post-install smoke test for a built wheel.

Confirms the wheel imports, the compiled extension loaded, the constants are
the ones the plugin promises, and the path policy is actually enforced. Run
against an installed package, not the source tree.
"""

from __future__ import annotations

import platform
import sys

PK = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


def main() -> int:
    import hermes_pubky
    from hermes_pubky import _native

    assert _native.REQUIRED_CAPABILITY == "/priv/hermes.pubky.app/v1/profiles/:rw"
    assert _native.MAX_DOCUMENT_BYTES == 65536
    assert _native.APP_NAMESPACE == "hermes.pubky.app"
    assert hermes_pubky.__version__ == _native.__version__

    _author, path, _url = _native.parse_context_url(f"pubky://{PK}/pub/ctx.json")
    assert path == "/pub/ctx.json", path

    # The policy must survive packaging, not just live in the source tree.
    for hostile in (f"pubky://{PK}/priv/x.json", f"pubky://{PK}/pub/../priv/x.json"):
        try:
            _native.parse_context_url(hostile)
        except _native.PubkyValidationError:
            pass
        else:
            raise AssertionError(f"path policy not enforced for {hostile}")

    assert _native.profile_path("default") == "/priv/hermes.pubky.app/v1/profiles/default.json"

    # The provider must be constructible without Hermes present.
    from hermes_pubky.provider import PubkyMemoryProvider

    assert PubkyMemoryProvider().name == "pubky"

    # The shim templates must be packaged, or `hermes-pubky install` breaks.
    from hermes_pubky.installer import TEMPLATE_DIR, FILES

    for template_name, _dest in FILES:
        assert (TEMPLATE_DIR / template_name).is_file(), f"missing template {template_name}"

    print(
        f"smoke test ok: hermes-pubky {hermes_pubky.__version__} "
        f"on {platform.system()}/{platform.machine()} "
        f"python {platform.python_version()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
