"""Password hashing, in isolation from the database.

argon2id, at the library's defaults. The behaviour worth pinning is what happens
to input that is not a password at all — `verify_password` is called on stored
values that may be empty (an OAuth-only account) or, if something has gone
wrong, not a hash.
"""

from __future__ import annotations

import pytest

from auth import MIN_PASSWORD_LENGTH, hash_password, verify_password


def test_a_hash_is_argon2id_and_does_not_contain_the_password() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert "correct horse battery staple" not in stored


def test_the_same_password_hashes_differently_each_time() -> None:
    """Salted, so two readers who pick the same password do not look alike in
    the table."""
    a = hash_password("a-shared-favourite")
    b = hash_password("a-shared-favourite")
    assert a != b
    assert verify_password(a, "a-shared-favourite")
    assert verify_password(b, "a-shared-favourite")


def test_verification_rejects_the_wrong_password() -> None:
    assert not verify_password(hash_password("right"), "wrong")


@pytest.mark.parametrize(
    ("stored", "raw"),
    [
        ("", "anything"),  # an OAuth-only account has no hash
        (None, "anything"),
        ("$argon2id$v=19$broken", "anything"),
        ("not a hash at all", "anything"),
    ],
)
def test_an_unusable_stored_value_is_false_not_an_exception(stored, raw) -> None:
    """An empty hash is a real state, and a corrupt one must fail closed rather
    than 500 the login form."""
    assert verify_password(stored, raw) is False


@pytest.mark.parametrize("raw", ["", None])
def test_an_empty_password_never_verifies(raw) -> None:
    assert verify_password(hash_password("real"), raw) is False


def test_the_minimum_length_is_a_length_and_nothing_else() -> None:
    """Composition rules are what push people towards "Password1!"."""
    assert MIN_PASSWORD_LENGTH >= 12
