"""PKCE verification — the pure half of the OAuth tests.

The flow-level tests (replay, expiry, refresh rotation) need a DB and live in
tests/integration/test_oauth_flow.py.
"""

from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode

import pytest

from oauth_provider import verify_pkce


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_s256_happy_path() -> None:
    verifier = "a" * 64
    assert verify_pkce(verifier, challenge_for(verifier), "S256") is True


def test_wrong_verifier_fails() -> None:
    assert verify_pkce("b" * 64, challenge_for("a" * 64), "S256") is False


def test_plain_method_is_refused_outright() -> None:
    """Downgrade to `plain` defeats the point of PKCE, so it is not merely
    discouraged — it is unsupported."""
    verifier = "a" * 64
    assert verify_pkce(verifier, verifier, "plain") is False


@pytest.mark.parametrize(
    ("verifier", "challenge"),
    [("", "x"), ("x", ""), ("", "")],
)
def test_empty_inputs_fail_closed(verifier, challenge) -> None:
    assert verify_pkce(verifier, challenge, "S256") is False


def test_challenge_has_no_base64_padding() -> None:
    """RFC 7636 uses base64url without padding; a padded challenge from a
    client must not accidentally match."""
    verifier = "a" * 64
    padded = challenge_for(verifier) + "=="
    assert verify_pkce(verifier, padded, "S256") is False
