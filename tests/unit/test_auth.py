"""Password comparison and session-token hashing.

The session lifecycle itself needs a database and lives in
tests/integration/test_sessions.py.
"""

from __future__ import annotations

import hashlib

import pytest

from auth import SESSION_TOKEN_PREFIX, SESSION_TTL, _hash, verify_reader_password
from config.settings import get_settings

PASSWORD = "unit-test-reader-password"  # pragma: allowlist secret


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("CROSSOVER_PASSWORD_UNITREADER", PASSWORD)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_right_password_verifies(configured) -> None:
    assert verify_reader_password("unitreader", PASSWORD) is True


def test_a_wrong_password_does_not(configured) -> None:
    assert verify_reader_password("unitreader", "nope") is False


def test_an_empty_password_never_verifies(configured) -> None:
    assert verify_reader_password("unitreader", "") is False


def test_an_unconfigured_reader_never_verifies(configured) -> None:
    """A seeded reader with no config var must not be loginable with anything."""
    assert verify_reader_password("nobodyhere", "anything") is False
    assert verify_reader_password("nobodyhere", "") is False


def test_a_prefix_of_the_password_does_not_verify(configured) -> None:
    """Guards the comparison: `==` would short-circuit and leak the password one
    character at a time, which is why this uses hmac.compare_digest."""
    assert verify_reader_password("unitreader", PASSWORD[:-1]) is False
    assert verify_reader_password("unitreader", PASSWORD + "x") is False


def test_only_the_hash_of_a_token_is_ever_stored() -> None:
    """A database dump must not contain anything replayable as a cookie."""
    token = f"{SESSION_TOKEN_PREFIX}abc123"
    assert _hash(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in _hash(token)
    assert len(_hash(token)) == 64


def test_the_token_prefix_is_greppable() -> None:
    """So a leaked session token is recognisable — which a bare UUID was not.
    scripts/check_secrets.py matches on this prefix."""
    from scripts.check_secrets import scan_text

    leaked = f'cookie = "{SESSION_TOKEN_PREFIX}' + "x" * 32 + '"'
    findings = scan_text(leaked, "probe.py")
    assert findings, "a leaked session token should be caught by the secret scanner"


def test_sessions_expire() -> None:
    """An unrotatable, non-expiring cookie was the whole problem (#17)."""
    assert SESSION_TTL.days == 30
