"""Secret storage in .env, and redaction of secrets from logs and errors."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hermes_pubky import config as cfg
from hermes_pubky.paths import GRANT_SECRET_ENV

# Shaped like the SDK's real stored-credential token.
SECRET = "pubky-grant-credential-v1:8pinxxgqs41n4aididenw5apqp1urfmzdztr8jt4abrkdn435ewo:AAAA_BBBB:eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2ln"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(GRANT_SECRET_ENV, raising=False)


class TestGrantSecretStorage:
    def test_write_then_read(self, tmp_path: Path):
        env = tmp_path / ".env"
        cfg.write_grant_secret(env, SECRET)
        assert cfg.read_grant_secret(env) == SECRET

    def test_the_env_file_is_owner_only(self, tmp_path: Path):
        env = tmp_path / ".env"
        cfg.write_grant_secret(env, SECRET)
        mode = stat.S_IMODE(env.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_unrelated_variables_are_preserved(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("OPENAI_API_KEY=sk-other\n# a comment\n", encoding="utf-8")
        cfg.write_grant_secret(env, SECRET)
        text = env.read_text()
        assert "OPENAI_API_KEY=sk-other" in text
        assert "# a comment" in text
        assert f"{GRANT_SECRET_ENV}={SECRET}" in text

    def test_rewriting_does_not_duplicate_the_key(self, tmp_path: Path):
        env = tmp_path / ".env"
        cfg.write_grant_secret(env, "first")
        cfg.write_grant_secret(env, "second")
        lines = [l for l in env.read_text().splitlines() if l.startswith(GRANT_SECRET_ENV)]
        assert lines == [f"{GRANT_SECRET_ENV}=second"]

    def test_clearing_removes_the_line_and_the_environment_value(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("KEEP=1\n", encoding="utf-8")
        cfg.write_grant_secret(env, SECRET)
        cfg.clear_grant_secret(env)
        assert GRANT_SECRET_ENV not in env.read_text()
        assert "KEEP=1" in env.read_text()
        assert GRANT_SECRET_ENV not in os.environ

    def test_the_live_environment_wins_over_the_file(self, tmp_path: Path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text(f"{GRANT_SECRET_ENV}=stale\n", encoding="utf-8")
        monkeypatch.setenv(GRANT_SECRET_ENV, "live")
        assert cfg.read_grant_secret(env) == "live"

    def test_quotes_are_stripped(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(f'{GRANT_SECRET_ENV}="quoted"\n', encoding="utf-8")
        assert cfg.read_grant_secret(env) == "quoted"

    def test_a_missing_file_reads_as_empty(self, tmp_path: Path):
        assert cfg.read_grant_secret(tmp_path / "nope.env") == ""


class TestRedaction:
    def test_scrubs_a_stored_credential_token(self):
        out = cfg.redact(f"failed to restore {SECRET} from disk")
        assert SECRET not in out
        assert "pubky-grant-credential-v1" not in out
        assert cfg.REDACTED in out

    def test_scrubs_a_bare_jws(self):
        jws = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhYmMifQ.Zm9vYmFy"
        assert jws not in cfg.redact(f"token={jws}")

    def test_scrubs_the_live_secret_even_in_an_unexpected_shape(self, monkeypatch):
        monkeypatch.setenv(GRANT_SECRET_ENV, "an-unusual-secret-value")
        out = cfg.redact("oops: an-unusual-secret-value leaked")
        assert "an-unusual-secret-value" not in out

    def test_scrubs_exceptions_too(self):
        out = cfg.redact(RuntimeError(f"bad grant {SECRET}"))
        assert SECRET not in out

    def test_leaves_ordinary_text_alone(self):
        assert cfg.redact("homeserver unreachable") == "homeserver unreachable"

    def test_none_becomes_empty(self):
        assert cfg.redact(None) == ""

    def test_a_very_short_live_secret_is_not_used_as_a_pattern(self, monkeypatch):
        # Replacing a 2-char value everywhere would mangle unrelated output.
        monkeypatch.setenv(GRANT_SECRET_ENV, "ab")
        assert cfg.redact("a table of absolutes") == "a table of absolutes"


class TestFingerprint:
    def test_is_stable_and_short(self):
        assert cfg.fingerprint(SECRET) == cfg.fingerprint(SECRET)
        assert len(cfg.fingerprint(SECRET)) == 8

    def test_differs_between_secrets(self):
        assert cfg.fingerprint("a") != cfg.fingerprint("b")

    def test_does_not_contain_the_secret(self):
        assert SECRET not in cfg.fingerprint(SECRET)

    def test_handles_no_secret(self):
        assert cfg.fingerprint("") == "(none)"


class TestProviderConfig:
    def test_profile_id_defaults(self):
        assert cfg.profile_id({}) == cfg.DEFAULT_PROFILE_ID

    def test_profile_id_is_read_from_the_memory_block(self):
        config = {"memory": {"pubky": {"profile_id": "work"}}}
        assert cfg.profile_id(config) == "work"

    def test_a_blank_profile_id_falls_back_to_the_default(self):
        config = {"memory": {"pubky": {"profile_id": "   "}}}
        assert cfg.profile_id(config) == cfg.DEFAULT_PROFILE_ID

    def test_malformed_config_does_not_raise(self):
        assert cfg.profile_id({"memory": "not a dict"}) == cfg.DEFAULT_PROFILE_ID
        assert cfg.provider_config({"memory": {"pubky": "nope"}}) == {}

    def test_set_and_activate(self):
        config: dict = {}
        cfg.set_provider_config(config, {"profile_id": "work"})
        cfg.activate(config)
        assert config["memory"]["pubky"]["profile_id"] == "work"
        assert config["memory"]["provider"] == "pubky"
        assert cfg.is_active(config) is True

    def test_activating_preserves_other_providers_config(self):
        config = {"memory": {"provider": "mem0", "mem0": {"k": "v"}}}
        cfg.activate(config)
        assert config["memory"]["mem0"] == {"k": "v"}
        assert config["memory"]["provider"] == "pubky"


class TestSetupPersistsTheGrant:
    """Setup must leave the grant on disk, however it was obtained.

    Regression: reusing a grant that came from the environment used to skip
    the .env write, so setup reported "saved to .env" while leaving no file
    behind and the next session started unauthorized.
    """

    def test_reusing_an_environment_grant_still_writes_the_env_file(
        self, tmp_path, monkeypatch
    ):
        from hermes_pubky import setup_flow
        from hermes_pubky.paths import Layout

        home = tmp_path / "hermes"
        home.mkdir()
        layout = Layout(home, "default")
        layout.ensure()
        monkeypatch.setenv(GRANT_SECRET_ENV, SECRET)
        assert not layout.env_file.exists()

        # Answers: profile id (blank -> the default), "reuse the grant?" -> yes.
        # The stub mirrors the real _ask, which falls back to the default on
        # an empty line.
        answers = iter(["", "y"])
        def fake_ask(label, default="", secret=False):
            return next(answers, "") or default
        monkeypatch.setattr(setup_flow, "_ask", fake_ask)
        # Fail right after the grant step so the test stays offline.
        monkeypatch.setattr(
            setup_flow.Remote, "connect",
            staticmethod(lambda secret, timeout: (_ for _ in ()).throw(ConnectionError("offline"))),
        )
        monkeypatch.setattr(setup_flow, "_out", lambda text="": None)

        setup_flow.run_setup(str(home), {})

        assert layout.env_file.exists(), "the grant was never persisted"
        assert cfg.read_grant_secret(layout.env_file) == SECRET
        assert stat.S_IMODE(layout.env_file.stat().st_mode) == 0o600
