"""Bookmarks and shelf entries — one table, because they are the same act.

A bookmark made mid-read and an issue photographed on a comic-shop rack differ
in *origin*, not in kind: both are "I might want this later", and both land on
the same rack. SPEC §6 forces the merge explicitly — the shelf is not scoped to
an event, so `event_id` and `position` must be nullable.

**Denormalization is a requirement, not an optimization.** SPEC §4: sequencing
a reading list must need zero follow-up API calls. Every column between
`series_name` and `characters` is a copy taken at write time for exactly that
reason. If ordering bookmarks ever requires a join back to `issues`, this record
has gone too thin.
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.types import BookmarkOrigin, BookmarkStatus

#: Deleting an issue or event must not delete the bookmark — the note and the
#: raw text are the reader's, not Marvel's, and losing them would be the worst
#: possible outcome of a catalog refetch.
_SET_NULL = "SET NULL"


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    # Nullable: a PENDING shelf candidate has raw text and no resolved issue yet.
    issue_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("issues.id", ondelete=_SET_NULL), nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BookmarkOrigin.MID_READ.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BookmarkStatus.CONFIRMED.value, index=True
    )
    # Preserved verbatim for PENDING shelf entries so the rack can show what was
    # actually said or read off a spine, not our best guess at it.
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- denormalized at write time: everything sequence_bookmarks needs ---
    event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id", ondelete=_SET_NULL), nullable=True
    )
    event_slug: Mapped[str] = mapped_column(String(100), nullable=False, default="",
                                            server_default="")
    event_name: Mapped[str] = mapped_column(String(200), nullable=False, default="",
                                            server_default="")
    # Position in the event's curated order. Null for off-event shelf finds —
    # which is exactly the rack's "ungrouped" section.
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="",
                                      server_default="")
    narrative_role: Mapped[str] = mapped_column(String(20), nullable=False, default="",
                                                server_default="")
    franchise: Mapped[str] = mapped_column(String(20), nullable=False, default="",
                                           server_default="")
    series_name: Mapped[str] = mapped_column(String(200), nullable=False, default="",
                                             server_default="")
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Copied so the rack renders covers without touching Marvel per card.
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_extension: Mapped[str | None] = mapped_column(String(10), nullable=True)
    characters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Copied so a link survives even if the issue row is later re-synced.
    digital_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability: Mapped[str] = mapped_column(String(20), nullable=False, default="unconfirmed")

    # --- provenance: why is this here? (SPEC §7) ---
    # The reference edge that prompted the save, when it came from one. Gives
    # the rack a citation — "Referenced in King in Black #3, p. 14" — instead of
    # an undated pile. A bookmark with no remembered reason is noise a week later.
    source_reference_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("issue_references.id", ondelete=_SET_NULL), nullable=True
    )
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The omnibus page the reader was on. The datum a QR-code insert would key on.
    omnibus_page: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ShelfCandidate(Base):
    """One unresolved candidate from an `add_to_shelf` phase-1 call.

    Phase 1 stores nothing *confirmed* (SPEC §6) but does record what was asked
    and what we offered back, so resolution quality can be reviewed later and so
    the rack can re-offer the choices for a candidate nobody answered about.
    """

    __tablename__ = "shelf_candidates"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    # The match candidates we returned, verbatim, so "I think that's Venom #87 —
    # right?" can be answered later from the rack with the same options.
    matches: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resolved_bookmark_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bookmarks.id", ondelete=_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
