"""Web-surface authentication: per-reader passwords and revocable sessions.

The MCP surface authenticates with OAuth bearer tokens (oauth_provider.py). The
web surface is a household sharing one deployment, so it has two pieces:

- **Each reader has their own password**, argon2id-hashed into
  `User.password_hash`. This is what separates one person's rack from
  another's; a shared key could not.
- **Signing in mints a session token.** The cookie carries a random token, not
  the reader's database id — see models/session.py for why that distinction
  matters. Admin is a property of the reader the session resolves to, so there
  is one cookie and one credential, not two.

Passwords used to be plaintext in config, one env var per reader
(`CROSSOVER_PASSWORD_{HANDLE}`). That was proportionate for a household and did
not scale past one: admitting a person meant a deploy. Nothing reads the
environment for a password any more — a reader either has a hash or has no way
in, and the way in is minted by an admin (`crossover set-password`,
`crossover reset-link`) or chosen at registration.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.reset import PasswordResetToken
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

#: argon2id at the library's defaults, which track OWASP guidance. Deliberately
#: not hand-tuned: a badly chosen parameter is worse than a maintained default,
#: and `check_needs_rehash` upgrades stored hashes for free when those defaults
#: move.
_hasher = PasswordHasher()

#: Verified against when there is nothing real to verify — an unknown handle, a
#: deactivated reader, an account with no password set. A failed login then
#: costs the same argon2 work as a successful one, so response time does not say
#: which handles exist. Computed once at import; hashing is slow by design.
_TIMING_EQUALIZER = _hasher.hash(secrets.token_urlsafe(32))

#: Length only, no composition rules — current NIST guidance, and composition
#: rules are what push people towards "Password1!".
MIN_PASSWORD_LENGTH = 12

#: Prefixed like every other credential this project mints, so a leaked one is
#: recognisable and `scripts/check_secrets.py` can match on it.
RESET_TOKEN_PREFIX = "xo_rst_"

#: Short on purpose. A working reset link is a full account takeover, which is
#: sharper than anything else handed around here, and an admin issuing one is
#: about to send it immediately — there is no reason for it to outlive that.
RESET_TTL = timedelta(hours=1)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored: str, raw: str) -> bool:
    """False for anything unusable, rather than raising."""
    if not stored or not raw:
        return False
    try:
        return _hasher.verify(stored, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def set_password(session: AsyncSession, user: User, raw: str) -> None:
    """The only writer of `password_hash`."""
    user.password_hash = hash_password(raw)
    await session.commit()


async def authenticate(session: AsyncSession, handle: str, password: str) -> User | None:
    """The single place a password decides anything.

    Returns the reader, or None for every kind of failure — unknown handle,
    wrong password, deactivated account, no password set. Callers must not
    distinguish those to the person at the form: a login page that says "no such
    reader" is a list of who exists.

Two paths:

    1. A stored argon2 hash, the only way in. Rehashed in place if the
       library's parameters have moved on since it was written.
    2. Anything else fails, after an equalizing verification so a missing
       account costs what a wrong password costs.
    """
    # Normalized here rather than at each call site: registration lowercases a
    # chosen handle, so login has to agree or someone who signed up as "dave"
    # cannot get in by typing "Dave".
    handle = (handle or "").strip().lower()
    user = await session.scalar(select(User).where(User.handle == handle))

    if user is None or not user.is_active or not password:
        verify_password(_TIMING_EQUALIZER, password or "x")
        return None

    if user.password_hash:
        if not verify_password(user.password_hash, password):
            return None
        if _hasher.check_needs_rehash(user.password_hash):
            await set_password(session, user, password)
        return user

    # No hash means no way in. That is a real state — the `claude` reader
    # authenticates only by OAuth token — and it must never authenticate.
    verify_password(_TIMING_EQUALIZER, password)
    return None


async def create_reset_token(
    session: AsyncSession, user: User, *, issued_by: User | None = None
) -> str:
    """Mint a one-time reset link for `user`. Returns the raw token, shown once.

    Any unspent tokens for that reader are invalidated first. Two live links
    for one account means the older one keeps working after the newer has been
    handed to somebody, which is the kind of thing nobody notices until it
    matters.
    """
    await session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    raw = f"{RESET_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    session.add(
        PasswordResetToken(
            token_hash=_hash(raw),
            user_id=user.id,
            issued_by=issued_by.id if issued_by else None,
            expires_at=datetime.now(UTC) + RESET_TTL,
        )
    )
    await session.commit()
    return raw


async def resolve_reset_token(session: AsyncSession, raw: str | None) -> User | None:
    """The reader a live reset link belongs to, or None.

    Looked up by hash, so the raw value is never compared against anything and
    there is no prefix to time. Expired and already-spent rows resolve to None
    but are kept, so a replayed link is distinguishable in the table from one
    that never existed.
    """
    if not raw:
        return None
    row = await session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash(raw))
    )
    if row is None or row.used_at is not None:
        return None
    if _aware(row.expires_at) < datetime.now(UTC):
        return None
    user = await session.get(User, row.user_id)
    return user if user is not None and user.is_active else None


async def consume_reset_token(session: AsyncSession, raw: str, new_password: str) -> User | None:
    """Spend a reset link: set the password, retire the link, sign them out.

    The sign-out is the part worth insisting on. A reset that leaves existing
    cookies working is not a reset — whoever prompted it may be holding one.
    """
    user = await resolve_reset_token(session, raw)
    if user is None or len(new_password) < MIN_PASSWORD_LENGTH:
        return None

    user.password_hash = hash_password(new_password)
    await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _hash(raw))
        .values(used_at=datetime.now(UTC))
    )
    await session.commit()
    # After the commit, so a failure above cannot sign somebody out without
    # having changed anything.
    await revoke_all_for_user(session, user.id)
    return user


async def create_session(session: AsyncSession, user: User, *, user_agent: str = "") -> str:
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
    row = await session.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
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
    row = await session.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
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
    result = await session.execute(delete(UserSession).where(UserSession.expires_at < cutoff))
    await session.commit()
    return int(result.rowcount or 0)
