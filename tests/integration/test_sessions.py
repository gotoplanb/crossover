"""Revocable, expiring web sessions — the durable fix for #17.

The old scheme put `users.id` in the cookie. A primary key and a session token
want opposite things: the key must be stable and is referenced by other rows,
while the token must be rotatable, expirable and revocable. Using one as the
other meant a leaked cookie could only be invalidated by deleting the reader —
and three tables cascade off that id, so the remedy destroyed the reading list
it was meant to protect.

These are the properties that were impossible before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from auth import (
    SESSION_COOKIE,
    SESSION_TOKEN_PREFIX,
    SESSION_TTL,
    _hash,
    create_session,
    purge_expired_sessions,
    resolve_session,
    revoke_all_for_user,
    revoke_session,
)
from models.session import UserSession

# --- the properties #17 was about ---


async def test_a_session_resolves_to_its_reader(session, user) -> None:
    token = await create_session(session, user)
    resolved = await resolve_session(session, token)
    assert resolved is not None
    assert resolved.id == user.id


async def test_the_readers_id_is_not_a_valid_token(session, user) -> None:
    """The core of #17. Learning a reader's id used to be enough to be them."""
    await create_session(session, user)
    assert await resolve_session(session, str(user.id)) is None


async def test_only_the_hash_is_stored(session, user) -> None:
    """A database dump must not contain anything replayable as a cookie."""
    token = await create_session(session, user)
    row = await session.scalar(select(UserSession))
    assert row.token_hash == _hash(token)
    assert token not in row.token_hash
    # And the raw token is nowhere else in the row either.
    assert token not in str(row.__dict__)


async def test_a_session_can_be_revoked_without_touching_the_reader(
    session, user
) -> None:
    """The remedy that did not exist before: previously the only way to
    invalidate a cookie was to delete the reader, cascading away their
    bookmarks."""
    token = await create_session(session, user)
    assert await resolve_session(session, token) is not None

    await revoke_session(session, token)
    assert await resolve_session(session, token) is None

    # The reader, and everything hanging off them, is untouched.
    from models.user import User

    assert await session.get(User, user.id) is not None


async def test_a_session_expires(session, user) -> None:
    """A cookie captured once used to be valid forever."""
    token = await create_session(session, user)
    row = await session.scalar(select(UserSession))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert await resolve_session(session, token) is None


async def test_sessions_are_rotated_on_each_sign_in(session, user) -> None:
    """Signing in again must not extend the life of a token captured earlier —
    it mints a new one, and the old one keeps its own expiry."""
    first = await create_session(session, user)
    second = await create_session(session, user)
    assert first != second
    assert await session.scalar(select(func.count()).select_from(UserSession)) == 2


async def test_revoking_everything_signs_a_reader_out_everywhere(session, user) -> None:
    tokens = [await create_session(session, user) for _ in range(3)]
    for token in tokens:
        assert await resolve_session(session, token) is not None

    revoked = await revoke_all_for_user(session, user.id)
    assert revoked == 3
    for token in tokens:
        assert await resolve_session(session, token) is None


async def test_revoking_everything_does_not_touch_another_reader(
    session, user, other_user
) -> None:
    mine = await create_session(session, user)
    theirs = await create_session(session, other_user)
    await revoke_all_for_user(session, user.id)
    assert await resolve_session(session, mine) is None
    assert await resolve_session(session, theirs) is not None


# --- rejection paths ---


async def test_nothing_invalid_authenticates(session, user) -> None:
    for token in (None, "", "garbage", str(user.id), f"{SESSION_TOKEN_PREFIX}nope"):
        assert await resolve_session(session, token) is None


async def test_a_deactivated_reader_cannot_use_a_live_session(session, user) -> None:
    """Deactivating has to take effect immediately, not at session expiry."""
    token = await create_session(session, user)
    user.is_active = False
    await session.commit()
    assert await resolve_session(session, token) is None


async def test_revoking_an_unknown_token_is_silent(session) -> None:
    """A double sign-out, or a stale cookie, must not raise."""
    await revoke_session(session, None)
    await revoke_session(session, "not-a-real-token")


# --- housekeeping ---


async def test_last_used_is_recorded_but_not_on_every_request(session, user) -> None:
    """Coarse on purpose: a page view should not be a write just to keep a
    timestamp current."""
    token = await create_session(session, user)
    await resolve_session(session, token)
    row = await session.scalar(select(UserSession))
    first_seen = row.last_used_at
    assert first_seen is not None

    await resolve_session(session, token)
    await session.refresh(row)
    assert row.last_used_at == first_seen


async def test_purge_removes_long_dead_sessions_only(session, user) -> None:
    live = await create_session(session, user)
    stale = await create_session(session, user)

    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _hash(stale))
    )
    row.expires_at = datetime.now(UTC) - timedelta(days=200)
    await session.commit()

    assert await purge_expired_sessions(session) == 1
    assert await resolve_session(session, live) is not None
    assert await session.scalar(select(func.count()).select_from(UserSession)) == 1


async def test_a_recently_expired_session_is_kept_for_audit(session, user) -> None:
    """Expired but recent rows stay, so "was this cookie live when it was used?"
    remains answerable."""
    token = await create_session(session, user)
    row = await session.scalar(select(UserSession))
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await session.commit()

    assert await purge_expired_sessions(session) == 0
    assert await resolve_session(session, token) is None  # still not usable


async def test_the_user_agent_is_recorded_and_bounded(session, user) -> None:
    """Attacker-controlled, so it is truncated and never parsed."""
    await create_session(session, user, user_agent="x" * 5000)
    row = await session.scalar(select(UserSession))
    assert len(row.user_agent) == 400


async def test_deleting_a_reader_removes_their_sessions(session, user) -> None:
    """The FK cascade still holds — sessions must not outlive their reader."""
    await create_session(session, user)
    await session.delete(user)
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(UserSession)) == 0


# --- through the web surface ---


async def test_signing_out_revokes_rather_than_only_clearing(
    client, sign_in, user, reader_password
) -> None:
    """Clearing the cookie alone would leave a token that still authenticates if
    it had been captured — precisely the weakness this replaced."""
    await sign_in(user.handle, reader_password)
    token = client.cookies.get(SESSION_COOKIE)
    assert token and token.startswith(SESSION_TOKEN_PREFIX)

    await client.post("/ui/logout")

    # Replay the captured cookie: it must be dead, not merely absent.
    client.cookies.set(SESSION_COOKIE, token)
    response = await client.get("/ui/rack", follow_redirects=False)
    assert response.status_code == 303


async def test_the_cookie_carries_an_expiry(sign_in, user, reader_password) -> None:
    response = await sign_in(user.handle, reader_password)
    header = next(
        c for c in response.headers.get_list("set-cookie") if SESSION_COOKIE in c
    )
    assert f"Max-Age={int(SESSION_TTL.total_seconds())}" in header
    assert "HttpOnly" in header
