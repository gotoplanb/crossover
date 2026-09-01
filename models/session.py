"""Web sessions — a credential that can be revoked without deleting the reader.

Replaces the previous scheme, where the session cookie held `users.id` directly
(#17). A primary key and a session token have opposite requirements: a key must
be stable and is referenced by other rows, while a token must be rotatable,
expirable and revocable. Using one as the other meant a leaked cookie could only
be invalidated by deleting the reader — and three tables cascade off that id, so
the remedy destroyed the reading list it was meant to protect.

Same discipline as `oauth_tokens`: only the SHA-256 of the token is stored, so
the database never holds anything that could be replayed as a cookie.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # SHA-256 of the raw token. The raw value exists in the cookie and nowhere
    # else, so a database dump cannot be replayed as a login.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on sign-out, or when revoking every session for a reader. Kept rather
    # than deleted so "was this cookie still live when it was used?" stays
    # answerable after the fact.
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free-text hint about where the session came from, for a future "sign out
    # my other devices". Truncated and never parsed — it is attacker-controlled.
    user_agent: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
