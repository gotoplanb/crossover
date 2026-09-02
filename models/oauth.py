"""OAuth 2.0 authorization-server tables.

Ported from conduct's `models/oauth.py` (SPEC §1) with one change: every code
and token binds to a `user_id` rather than a `client_app_id`. That is the
deliberate divergence — reading lists are per-person, and two people share this
deployment, so the principal has to be a human.

Secrets, codes, and tokens are only ever stored as SHA-256 hashes; raw values
exist in transit and are shown once.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class OAuthClient(Base):
    """A registered connector, e.g. Claude on someone's phone.

    `user_id` is who *registered* it, kept for audit and for `register-connector`
    to have an owner. It is deliberately **not** the principal its tokens act
    as — that is the reader who approves at the consent screen, recorded on each
    code and token. One connector therefore serves every reader in the
    deployment, each with their own separately-revocable tokens.
    """

    __tablename__ = "oauth_clients"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class OAuthAuthorizationCode(Base):
    """Short-lived code issued at /authorize, redeemed once at /token."""

    __tablename__ = "oauth_authorization_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(10), nullable=False, default="S256")
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class OAuthToken(Base):
    """An issued access token plus its refresh token, both stored hashed.
    Revoked/expired rows are kept for audit but never authenticate."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    access_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    refresh_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
