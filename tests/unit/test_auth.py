"""Password comparison and session-token hashing.

The session lifecycle itself needs a database and lives in
tests/integration/test_sessions.py.
"""

from __future__ import annotations

import hashlib

from auth import SESSION_TOKEN_PREFIX, SESSION_TTL, _hash


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
