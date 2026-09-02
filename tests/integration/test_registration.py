"""Self-service registration, gated by an invite code.

Passwords used to be one env var per reader, which meant admitting a person was
a deploy. They now live in `User.password_hash` as argon2id, and anyone with the
invite code can create their own account.

The tests that matter most here are the ones guarding against a stranger getting
more than a rack: registration must never grant admin, must never open by
accident, and must never become a way to find out who already has an account.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from auth import authenticate, hash_password, verify_password
from config.settings import get_settings
from csrf import CSRF_FIELD
from models.user import User

CODE = "open-sesame-friend"  # pragma: allowlist secret
GOOD_PASSWORD = "a-long-enough-passphrase"  # pragma: allowlist secret


@pytest.fixture
def invite_code(monkeypatch):
    monkeypatch.setenv("CROSSOVER_INVITE_CODE", CODE)
    get_settings.cache_clear()
    yield CODE
    get_settings.cache_clear()


@pytest.fixture
def registration_closed(monkeypatch):
    monkeypatch.delenv("CROSSOVER_INVITE_CODE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _register(client, **fields):
    """Submit the form the way a browser does, token scraped from the page."""
    page = (await client.get("/ui/register")).text
    token = re.search(rf'name="{CSRF_FIELD}" value="([^"]*)"', page).group(1)
    payload = {
        "invite_code": CODE,
        "handle": "newreader",
        "email": "new@example.com",
        "display_name": "",
        "password": GOOD_PASSWORD,
        "password_confirm": GOOD_PASSWORD,
        CSRF_FIELD: token,
    }
    payload.update(fields)
    return await client.post("/ui/register", data=payload, follow_redirects=False)


# --- the gate ----------------------------------------------------------------


async def test_registration_is_closed_without_an_invite_code(client, registration_closed) -> None:
    """Fail closed. This app writes to a database and spends a rate-limited
    third-party quota on every shelf lookup, so forgetting to configure the gate
    must not be the mistake that opens the door."""
    assert (await client.get("/ui/register")).status_code == 404
    assert (await client.post("/ui/register", data={})).status_code == 404


async def test_a_wrong_invite_code_creates_nothing(client, session, invite_code) -> None:
    response = await _register(client, invite_code="not-the-code")
    assert response.status_code == 400
    assert await session.scalar(select(User).where(User.handle == "newreader")) is None


# --- what a stranger must not get -------------------------------------------


async def test_registration_never_grants_admin(client, session, invite_code) -> None:
    """The invite code is a shared secret that will end up in a chat message.
    It must buy a rack and nothing else — admin reaches the curation views and
    can approve an OAuth grant."""
    await _register(client, handle="sneaky", email="sneaky@example.com", is_admin="true")
    user = await session.scalar(select(User).where(User.handle == "sneaky"))
    assert user is not None
    assert user.is_admin is False


async def test_registration_does_not_reveal_who_exists(client, session, user, invite_code) -> None:
    """ "That handle or email is already registered" is unavoidable — the form
    cannot accept a duplicate. But it must be the *same* message for a taken
    handle and a taken email, so it cannot be used to test whether one
    particular person has an account."""
    by_handle = await _register(client, handle=user.handle, email="fresh@example.com")
    by_email = await _register(client, handle="freshhandle", email=user.email)
    assert by_handle.status_code == by_email.status_code == 400

    def message(html: str) -> str:
        return re.search(r'text-red-400">([^<]*)<', html).group(1).strip()

    assert message(by_handle.text) == message(by_email.text)


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("handle", "9leading-digit"),
        ("handle", "has spaces"),
        ("handle", ""),
        ("email", "not-an-email"),
        ("email", ""),
        ("password", "tooshort"),
    ],
)
async def test_bad_input_is_refused(client, session, invite_code, field, value) -> None:
    response = await _register(
        client,
        **{field: value, "password_confirm": value if field == "password" else GOOD_PASSWORD},
    )
    assert response.status_code == 400
    assert await session.scalar(select(User).where(User.handle == "newreader")) is None


async def test_mismatched_passwords_are_refused(client, invite_code) -> None:
    response = await _register(client, password_confirm="something-else-entirely")
    assert response.status_code == 400
    assert "match" in response.text


async def test_what_was_typed_survives_an_error(client, invite_code) -> None:
    """Retyping an email because a password was short is the kind of small
    insult that makes people give up."""
    response = await _register(client, handle="keepme", email="keep@example.com", password="short")
    assert 'value="keepme"' in response.text
    assert 'value="keep@example.com"' in response.text
    assert "short" not in response.text, "a password must never be echoed back"


# --- the happy path ----------------------------------------------------------


async def test_registering_creates_a_hashed_password_and_signs_in(
    client, session, invite_code
) -> None:
    response = await _register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/rack"

    user = await session.scalar(select(User).where(User.handle == "newreader"))
    assert user is not None
    assert user.display_name == "Newreader", "a blank display name falls back to the handle"
    assert user.password_hash and user.password_hash.startswith("$argon2id$")
    assert GOOD_PASSWORD not in user.password_hash
    assert verify_password(user.password_hash, GOOD_PASSWORD)

    # The redirect carried a session, so the rack is reachable immediately.
    assert (await client.get("/ui/rack")).status_code == 200


async def test_a_registered_reader_can_sign_in(client, session, invite_code) -> None:
    await _register(client)
    user = await authenticate(session, "newreader", GOOD_PASSWORD)
    assert user is not None and user.handle == "newreader"
    assert await authenticate(session, "newreader", "wrong-password-entirely") is None


# --- the migration path ------------------------------------------------------


async def test_a_legacy_env_password_is_accepted_once_then_hashed(
    session, user, monkeypatch
) -> None:
    """Nobody should have to be told a new password. The env var is honoured on
    the next sign-in, hashed into the database, and never consulted again."""
    legacy = "the-old-env-password"  # pragma: allowlist secret
    monkeypatch.setenv(f"CROSSOVER_PASSWORD_{user.handle.upper()}", legacy)
    get_settings.cache_clear()
    user.password_hash = ""
    await session.commit()

    assert await authenticate(session, user.handle, legacy) is not None
    await session.refresh(user)
    assert user.password_hash.startswith("$argon2id$")

    # Now independent of the environment.
    monkeypatch.delenv(f"CROSSOVER_PASSWORD_{user.handle.upper()}")
    get_settings.cache_clear()
    assert await authenticate(session, user.handle, legacy) is not None
    get_settings.cache_clear()


async def test_an_account_with_no_password_cannot_sign_in(session, user) -> None:
    """The `claude` reader authenticates only by OAuth token. An empty hash is a
    real state, and it must never authenticate — including against an empty
    password."""
    user.password_hash = ""
    await session.commit()
    assert await authenticate(session, user.handle, "") is None
    assert await authenticate(session, user.handle, "anything-at-all") is None


async def test_an_unknown_handle_and_a_wrong_password_are_indistinguishable(session, user) -> None:
    await session.refresh(user)
    user.password_hash = hash_password(GOOD_PASSWORD)
    await session.commit()
    assert await authenticate(session, "nobody-by-that-name", GOOD_PASSWORD) is None
    assert await authenticate(session, user.handle, "wrong") is None


async def test_a_deactivated_reader_cannot_sign_in(session, user) -> None:
    user.password_hash = hash_password(GOOD_PASSWORD)
    user.is_active = False
    await session.commit()
    assert await authenticate(session, user.handle, GOOD_PASSWORD) is None


async def test_a_handle_is_case_insensitive_end_to_end(client, session, invite_code) -> None:
    """Registration lowercases what was typed, so login must too — otherwise
    someone who signed up as "Dave" can never sign in again."""
    response = await _register(client, handle="MixedCase", email="mixed@example.com")
    assert response.status_code == 303
    assert await session.scalar(select(User).where(User.handle == "mixedcase")) is not None

    for typed in ("mixedcase", "MixedCase", "  MIXEDCASE  "):
        assert await authenticate(session, typed, GOOD_PASSWORD) is not None, typed


async def test_a_hash_from_weaker_parameters_is_upgraded_on_sign_in(session, user) -> None:
    """argon2's defaults move as hardware does. A reader signing in with a hash
    written under older parameters should have it rewritten under the current
    ones, without being asked anything."""
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    user.password_hash = weak.hash(GOOD_PASSWORD)
    await session.commit()
    stale = user.password_hash

    assert await authenticate(session, user.handle, GOOD_PASSWORD) is not None
    await session.refresh(user)
    assert user.password_hash != stale, "the stored hash should have been upgraded"
    assert verify_password(user.password_hash, GOOD_PASSWORD)


async def test_a_simultaneous_duplicate_is_caught_by_the_constraint(
    client, session, invite_code, monkeypatch
) -> None:
    """Two people claiming one handle at the same moment pass the availability
    check together; the unique constraint is what actually decides. The loser
    should see the ordinary message, not a 500."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    calls = {"n": 0}

    async def commit_once_then_collide(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", commit_once_then_collide)
    response = await _register(client, handle="raced", email="raced@example.com")
    assert response.status_code == 400
    assert "already registered" in response.text
