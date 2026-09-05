"""One-time password reset links, issued by an admin.

Nothing here sends email, so there is no self-service reset. An admin mints a
link and hands it over by whatever channel they already use to talk to that
person — which is a *better* property than an emailed link, not a worse one:
the channel is out of band and chosen deliberately rather than being whatever
address happens to be on file.

Held as a SHA-256 hash for the same reason `UserSession` is: the table is not a
place a working credential should sit. A guessable value here is a full account
takeover, which is sharper than a guessable invite code, so the lifetime is
short and every token is single-use.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    #: Which admin issued it. Kept because "who let this person back in" is a
    #: question worth being able to answer later.
    issued_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set the moment it is spent. Rows are kept rather than deleted so a
    #: replayed link is distinguishable from one that never existed.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
