"""Admin cookie comparison."""

from __future__ import annotations

from auth import admin_cookie_valid
from config.settings import get_settings


def test_the_configured_key_validates() -> None:
    assert admin_cookie_valid(get_settings().admin_key) is True


def test_a_wrong_key_does_not() -> None:
    assert admin_cookie_valid("nope") is False


def test_a_missing_cookie_does_not() -> None:
    assert admin_cookie_valid(None) is False
    assert admin_cookie_valid("") is False


def test_a_prefix_of_the_real_key_does_not_validate() -> None:
    """Guards the comparison itself: `==` would short-circuit and leak the key
    one character at a time, which is why this uses hmac.compare_digest."""
    real = get_settings().admin_key
    assert admin_cookie_valid(real[:-1]) is False
    assert admin_cookie_valid(real + "x") is False
