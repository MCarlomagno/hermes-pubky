"""An in-memory stand-in for a homeserver.

The Python suite runs without a network: ``Remote`` is the only boundary the
plugin crosses, so substituting it here covers every sync path. Writes are
recorded so tests can assert on what was actually pushed, and the failure
hooks let tests drive the retry and fallback paths.
"""

from __future__ import annotations

import json
from typing import Dict, Optional

from hermes_pubky.schema import Profile


class FakeRemote:
    """An in-memory stand-in for a homeserver.

    Records every write so tests can assert on what was actually pushed, and
    can be told to fail so retry/fallback paths are exercised.
    """

    def __init__(self, profile: Optional[Profile] = None) -> None:
        self.stored: Dict[str, bytes] = {}
        self.puts: list = []
        self.fail_fetch: Optional[Exception] = None
        self.fail_put: Optional[Exception] = None
        self.public_key = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"
        self.capabilities = ["/priv/hermes.pubky.app/v1/profiles/:rw"]
        if profile is not None:
            self.stored[profile.profile_id] = profile.to_bytes()

    def fetch_profile(self, profile_id: str, timeout_secs: float = 5.0) -> Optional[Profile]:
        if self.fail_fetch is not None:
            raise self.fail_fetch
        raw = self.stored.get(profile_id)
        return Profile.parse_bytes(raw) if raw is not None else None

    def put_profile(self, profile: Profile, timeout_secs: float = 10.0) -> None:
        if self.fail_put is not None:
            raise self.fail_put
        self.stored[profile.profile_id] = profile.to_bytes()
        self.puts.append(json.loads(profile.to_bytes()))

    def is_valid(self, timeout_secs: float = 5.0) -> bool:
        return True

    def revoke(self, timeout_secs: float = 10.0) -> None:
        self.stored.clear()
