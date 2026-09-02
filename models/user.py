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

#: Lowercase, starts with a letter, no punctuation beyond an underscore.
#: This originally existed because a handle had to be a legal
#: environment-variable suffix — passwords were supplied as
#: `CROSSOVER_PASSWORD_{HANDLE}`. Passwords now live in `password_hash`, so the
#: constraint is no longer load-bearing, but it is kept: handles are typed at a
#: login form and read aloud, and the narrow character set keeps them
#: unambiguous.
HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def valid_handle(handle: str) -> bool:
    return bool(HANDLE_PATTERN.match(handle or ""))


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Short lowercase identifier — "dave", "tabitha". What a reader types at
    # the login form, and chosen by them at registration.
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
    # argon2id, from `auth.hash_password`. Empty means no password is set, which
    # is a real state rather than an error: the `claude` reader authenticates
    # only through an OAuth token, and an account created before passwords moved
    # into the database has none until its owner next signs in.
    #
    # Empty must therefore never authenticate. `auth.authenticate` is the single
    # place that decides, and it refuses an empty hash outright.
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
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
