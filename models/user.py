"""The human principal.

The one deliberate divergence from conduct (SPEC §1): conduct attributes work
to a ClientApp, this attributes it to a person, because reading lists are
per-person and two people share one deployment. The OAuth machinery ports
as-is; only the entity it resolves to changes.
"""

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

#: A handle has to be a legal environment-variable suffix, because that is how
#: its password is supplied: `CROSSOVER_PASSWORD_{HANDLE}`. Enforced at the
#: seam where users are created rather than trusted, so a handle can never be
#: created that has no way to authenticate.
HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def valid_handle(handle: str) -> bool:
    return bool(HANDLE_PATTERN.match(handle or ""))


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Short lowercase identifier — "dave", "tabitha". Names the reader in the
    # login form and, more importantly, names their password's environment
    # variable: CROSSOVER_PASSWORD_DAVE.
    handle: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True, default=""
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # Admins reach the curation views and can approve an OAuth grant. A reader
    # without it gets their own rack and nothing else — which is the point of
    # two people sharing one deployment.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Subject claim from whatever identity provider approved the OAuth grant.
    # Empty for users created by the seed script and authorized by admin consent.
    oauth_subject: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
