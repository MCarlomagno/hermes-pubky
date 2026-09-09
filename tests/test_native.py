"""The compiled extension's Python-facing surface.

The deep parsing rules are covered by the Rust unit tests; these confirm the
boundary — that the policy is actually enforced on the Python side and that
failures arrive as the documented exception types.
"""

from __future__ import annotations

import hashlib

import pytest

from hermes_pubky.remote import native_available, sha256_hex

pytestmark = pytest.mark.skipif(
    not native_available(), reason="native extension not built"
)

PK = "8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo"


@pytest.fixture
def native():
    from hermes_pubky import _native

    return _native


class TestConstants:
    def test_requests_only_the_private_profile_capability(self, native):
        assert native.REQUIRED_CAPABILITY == "/priv/hermes.pubky.app/v1/profiles/:rw"

    def test_the_document_cap_is_64_kib(self, native):
        assert native.MAX_DOCUMENT_BYTES == 64 * 1024

    def test_the_client_id_is_the_app_namespace(self, native):
        assert native.client_id() == "hermes.pubky.app"
        assert native.APP_NAMESPACE == "hermes.pubky.app"


class TestHashing:
    def test_matches_hashlib(self, native):
        data = b"the quick brown fox"
        assert native.sha256_hex(data) == hashlib.sha256(data).hexdigest()

    def test_is_sensitive_to_formatting(self, native):
        # Pinning hashes raw bytes, so whitespace changes must register.
        assert native.sha256_hex(b'{"a":1}') != native.sha256_hex(b'{"a": 1}')

    def test_the_python_helper_agrees(self):
        assert sha256_hex(b"x") == hashlib.sha256(b"x").hexdigest()

    def test_empty_input(self, native):
        assert native.sha256_hex(b"") == hashlib.sha256(b"").hexdigest()


class TestContextUrlPolicy:
    def test_accepts_a_public_json_document(self, native):
        author, path, normalized = native.parse_context_url(
            f"pubky://{PK}/pub/hermes.pubky.app/v1/contexts/researcher.json"
        )
        assert author == PK
        assert path == "/pub/hermes.pubky.app/v1/contexts/researcher.json"
        assert normalized.startswith("pubky://")

    @pytest.mark.parametrize(
        "url",
        [
            f"pubky://{PK}/priv/hermes.pubky.app/v1/profiles/default.json",
            f"pubky://{PK}/pub/../priv/secrets.json",
            f"pubky://{PK}/pub//double.json",
            f"pubky://{PK}/pub/notes.md",
            f"pubky://{PK}/pub/contexts/",
            "pubky://not-a-valid-key/pub/x.json",
            "https://example.com/x.json",
            "",
            "pubky://",
        ],
    )
    def test_rejects_everything_outside_policy(self, native, url):
        with pytest.raises(native.PubkyValidationError):
            native.parse_context_url(url)

    def test_a_very_long_url_is_rejected(self, native):
        with pytest.raises(native.PubkyValidationError):
            native.parse_context_url(f"pubky://{PK}/pub/{'a' * 3000}.json")

    def test_validation_errors_are_pubky_errors(self, native):
        with pytest.raises(native.PubkyError):
            native.parse_context_url("nonsense")


class TestProfileIdPolicy:
    @pytest.mark.parametrize("pid", ["default", "work-laptop", "a", "p1_2", "0"])
    def test_accepts_reasonable_ids(self, native, pid):
        native.validate_profile_id(pid)
        assert native.profile_path(pid) == f"/priv/hermes.pubky.app/v1/profiles/{pid}.json"

    @pytest.mark.parametrize(
        "pid", ["", "../escape", "a/b", "with space", "-lead", "_lead", "sü", "x" * 65]
    )
    def test_rejects_hostile_ids(self, native, pid):
        with pytest.raises(native.PubkyValidationError):
            native.validate_profile_id(pid)

    def test_a_rejected_id_never_produces_a_path(self, native):
        with pytest.raises(native.PubkyValidationError):
            native.profile_path("../../etc/passwd")


class TestExceptionHierarchy:
    def test_every_error_derives_from_the_base(self, native):
        for name in (
            "PubkyAuthError",
            "PubkyNetworkError",
            "PubkyNotFoundError",
            "PubkyTooLargeError",
            "PubkyValidationError",
        ):
            assert issubclass(getattr(native, name), native.PubkyError), name

    def test_timeout_is_a_network_error(self, native):
        # Callers retry on network errors; a timeout must be caught by that.
        assert issubclass(native.PubkyTimeoutError, native.PubkyNetworkError)


class TestAuthFlow:
    def test_rejects_malformed_capabilities(self, native):
        with pytest.raises(native.PubkyValidationError):
            native.AuthFlow("not-a-capability", "hermes.pubky.app")

    def test_rejects_an_empty_client_id(self, native):
        with pytest.raises(native.PubkyValidationError):
            native.AuthFlow(native.REQUIRED_CAPABILITY, "")
