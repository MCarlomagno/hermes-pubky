"""Thin wrapper over the native Pubky bindings.

Every network call the plugin makes goes through this module. Keeping it
narrow means the pure-Python logic (schema, outbox, sync decisions) can be
unit-tested against a fake with no homeserver and no compiled extension.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from .schema import Profile, SchemaError


class NativeUnavailable(RuntimeError):
    """The compiled extension could not be imported."""


def native() -> Any:
    """Import the compiled extension, with an actionable error if it is absent."""
    try:
        from . import _native  # type: ignore

        return _native
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise NativeUnavailable(
            "the hermes-pubky native extension is not available "
            f"({exc}). Reinstall with 'uv pip install --force-reinstall hermes-pubky'."
        ) from exc


def native_available() -> bool:
    try:
        native()
        return True
    except NativeUnavailable:
        return False


def sha256_hex(data: bytes) -> str:
    """Hash raw bytes, falling back to hashlib when the extension is absent."""
    try:
        return native().sha256_hex(data)
    except NativeUnavailable:
        import hashlib

        return hashlib.sha256(data).hexdigest()


def parse_context_url(url: str) -> Tuple[str, str, str]:
    """Return ``(author, path, normalized_url)`` for a base-context address."""
    return native().parse_context_url(url)


class Remote:
    """An authenticated connection to the user's homeserver."""

    def __init__(self, session: Any) -> None:
        self._session = session

    @staticmethod
    def connect(secret: str, timeout_secs: float = 5.0) -> "Remote":
        """Restore a session from a stored grant secret."""
        if not secret:
            raise ValueError("no grant secret available; run 'hermes pubky login'")
        session = native().Session.restore(secret, timeout_secs)
        return Remote(session)

    @property
    def public_key(self) -> str:
        return self._session.public_key

    @property
    def capabilities(self) -> list:
        return list(self._session.capabilities)

    def fetch_profile(self, profile_id: str, timeout_secs: float = 5.0) -> Optional[Profile]:
        """Load the private profile, or None when it does not exist yet."""
        raw = self._session.get_profile(profile_id, timeout_secs)
        if raw is None:
            return None
        return Profile.parse_bytes(bytes(raw))

    def put_profile(self, profile: Profile, timeout_secs: float = 10.0) -> None:
        self._session.put_profile(profile.profile_id, profile.to_bytes(), timeout_secs)

    def delete_profile(self, profile_id: str, timeout_secs: float = 10.0) -> None:
        self._session.delete_profile(profile_id, timeout_secs)

    def is_valid(self, timeout_secs: float = 5.0) -> bool:
        return bool(self._session.is_valid(timeout_secs))

    def revoke(self, timeout_secs: float = 10.0) -> None:
        self._session.revoke(timeout_secs)


def fetch_public_context(url: str, timeout_secs: float = 5.0) -> Tuple[bytes, str]:
    """Download a public base context. Returns ``(raw_bytes, sha256_hex)``.

    The hash is taken over the raw bytes rather than a re-serialized form, so
    any change to the document — including formatting — invalidates the pin.
    """
    raw = bytes(native().public_get(url, timeout_secs))
    return raw, sha256_hex(raw)


def errors() -> Any:
    """The native exception namespace, for callers that catch by type."""
    return native()


def is_transient(exc: BaseException) -> bool:
    """True when retrying later could plausibly succeed.

    Network and timeout failures are transient. Auth failures, validation
    errors and schema errors are not — retrying those just burns the backoff
    budget and hides a problem the user has to fix.
    """
    if isinstance(exc, (SchemaError, ValueError)):
        return False
    try:
        mod = native()
    except NativeUnavailable:
        return False
    network = getattr(mod, "PubkyNetworkError", None)
    if network is not None and isinstance(exc, network):
        return True
    return False
