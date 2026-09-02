"""Web-surface authentication: per-reader passwords and revocable sessions.

The MCP surface authenticates with OAuth bearer tokens (oauth_provider.py). The
web surface is a household sharing one deployment, so it has two pieces:

- **Each reader has their own password**, supplied as
  `CROSSOVER_PASSWORD_{HANDLE}`. This is what separates one person's rack from
  another's; a shared key could not.
- **Signing in mints a session token.** The cookie carries a random token, not
  the reader's database id — see models/session.py for why that distinction
  matters. Admin is a property of the reader the session resolves to, so there
  is one cookie and one credential, not two.

Passwords are compared against plaintext held in config rather than hashes. For
a household that is the same exposure a config var already carries; it is the
wrong answer for more people than that.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.session import UserSession
from models.user import User

#: Cookie holding the session token. Renamed from the old `crossover_user`
#: deliberately: that cookie held a user id, and any still in a browser must
#: fail to authenticate rather than being silently reinterpreted.
SESSION_COOKIE = "crossover_session"

#: Prefixed so a leaked token is recognisable — `scripts/check_secrets.py`
#: matches on it, which it could not do for a bare UUID.
SESSION_TOKEN_PREFIX = "xo_sess_"

#: Long enough that a reader is not re-authenticating mid-chapter, short enough
#: that an abandoned cookie stops working. Re-issued on every sign-in.
SESSION_TTL = timedelta(days=30)

#: Written at most this often, so reading the rack is not a write on every
#: request just to keep a timestamp current.
LAST_USED_RESOLUTION = timedelta(hours=1)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_reader_password(handle: str, password: str) -> bool:
    """Check a reader's password in constant time.

    Returns False for an unknown handle or an unset password, and does so after
    a comparison of the same shape, so a configured reader and an unconfigured
    one take the same path. Not a defence against a determined attacker — the
    login form is not rate limited — but it costs nothing and stops the response
    time from announcing which handles exist.
    """
    expected = get_settings().reader_password(handle)
    if not password:
        return False
    if expected is None:
        hmac.compare_digest(password, password)
        return False
    return hmac.compare_digest(password, expected)


async def create_session(
    session: AsyncSession, user: User, *, user_agent: str = ""
) -> str:
    """Mint a session and return the raw token, which is shown once.

    Only its hash is stored, so the database never holds a replayable cookie.
    """
    raw = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    session.add(
        UserSession(
            token_hash=_hash(raw),
            user_id=user.id,
            expires_at=datetime.now(UTC) + SESSION_TTL,
            user_agent=(user_agent or "")[:400],
            # Rotated with the session, so signing out invalidates the CSRF
            # token too rather than leaving a usable one behind.
            csrf_token=secrets.token_urlsafe(32),
        )
    )
    await session.commit()
    return raw


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def resolve_session(session: AsyncSession, token: str | None) -> User | None:
    """Map a session cookie to its reader, or None.

    None for anything not currently valid: missing, unknown, revoked, expired,
    or belonging to a deactivated reader. Deliberately one return value for all
    of them — the caller's only correct response is to treat the request as
    signed out.
    """
    if not token:
        return None
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _hash(token))
    )
    if row is None or row.revoked or _aware(row.expires_at) < datetime.now(UTC):
        return None

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    # Coarse, so a page view is not a write. Only useful for spotting a session
    # nobody has touched in months.
    now = datetime.now(UTC)
    if row.last_used_at is None or _aware(row.last_used_at) < now - LAST_USED_RESOLUTION:
        row.last_used_at = now
        await session.commit()
    return user


async def revoke_session(session: AsyncSession, token: str | None) -> None:
    """Sign out one session. Idempotent, and silent about unknown tokens."""
    if not token:
        return
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _hash(token))
    )
    if row is not None and not row.revoked:
        row.revoked = True
        await session.commit()


async def revoke_all_for_user(session: AsyncSession, user_id) -> int:
    """Sign a reader out everywhere — the remedy this whole table exists for.

    Previously the only way to invalidate a leaked cookie was to delete the
    reader, which cascaded away their bookmarks.
    """
    result = await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
        .values(revoked=True)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def purge_expired_sessions(session: AsyncSession, *, keep_days: int = 90) -> int:
    """Delete long-dead sessions. Revoked ones are kept for `keep_days` so a
    "was this cookie live when it was used?" question stays answerable."""
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    result = await session.execute(
        delete(UserSession).where(UserSession.expires_at < cutoff)
    )
    await session.commit()
    return int(result.rowcount or 0)
