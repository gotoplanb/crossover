"""Events, issues, membership, and the curated reference graph.

**Ownership split — the single most important rule in this module.**

SPEC §3 keeps two data layers apart: refetchable Marvel data and hand
curation. These tables are where they meet, so every column below is labelled
with who writes it:

  API   — written by `marvel.sync`, overwritten wholesale on every refetch.
  CUR   — written by `curation.load` from the git-tracked YAML. A refetch must
          never touch these; that is what makes curation safe to hand-edit.

`marvel.sync.API_OWNED_COLUMNS` is the enforced list, and
`tests/unit/test_ownership.py` fails if a curated column leaks into it.
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.types import Availability, CurationStatus, IssueRole

_ISSUES_ID = "issues.id"
_EVENTS_ID = "events.id"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # CUR — the YAML filename stem, and the handle every tool takes.
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # API — the numeric id on gateway.marvel.com/v1/public/events/{id}.
    marvel_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CUR — publication window of the event as a whole.
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    curation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CurationStatus.BEST_EFFORT.value
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class Issue(Base):
    """One comic issue.

    The four identifier columns are four *separate ID spaces* (SPEC §4). None
    converts to another; each is populated only when confirmed. Nullable by
    design — a null here is the honest answer, and Gate B says a guess is worse
    than a null.
    """

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("series_slug", "issue_number", name="uq_issue_series_number"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)

    # CUR — the stable handle curation YAML and reference edges point at, e.g.
    # "king-in-black-3". Independent of every Marvel id so curation can name an
    # issue before the API has ever been called.
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)

    # CUR/API — identity. Curation seeds these; a sync corrects them from the
    # canonical Marvel record.
    series_name: Mapped[str] = mapped_column(String(200), nullable=False)
    series_slug: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- four distinct identifier spaces; populate only what is confirmed ---
    # API — marvel.com's issue id. Parsed out of the record's `urls` array.
    # Doubles as the Branch `sourceId` (see docs/gates.md, Gate C).
    marvel_com_issue_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # API — the Marvel Unlimited reader id. The *only* input to a reader URL.
    digital_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # API — kept separate from marvel_com_issue_id even though Gate C found
    # them equal for the links probed, because "equal today" is not "the same
    # thing", and conflating them is how Gate B bugs get born.
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Not available from public endpoints; column exists so a hand-captured one
    # has somewhere to live.
    unison_drn: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # API — where `digital_id` came from, e.g. "marvel-api" or
    # "snapshot:king-in-black". Gate B originally required every id to come from
    # a Marvel API response; that API no longer exists (docs/gates.md), so the
    # rule became "from a Marvel-derived source, verified, with the source
    # recorded". This column is the recording half — without it, "verified"
    # would be a claim in a commit message rather than a property of the row.
    digital_id_source: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )

    # API — when Marvel Unlimited actually makes it readable, which lags print
    # by roughly three months. A different question from `availability`: that
    # says whether we have evidence of a digital id at all, while this says
    # whether the id works *yet*. An issue can be LINKABLE and still not
    # readable, which is exactly the case a reader following a current event
    # hits, and the case a bare link would lie about.
    unlimited_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # API — cover art. Stored split exactly as Marvel returns it so the
    # variant suffix can be chosen at render time (SPEC §7).
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_extension: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # API — character names, for franchise clustering cross-checks.
    characters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    creators: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # CUR — Gate B gate. LINKABLE requires digital_id; the loader refuses
    # otherwise, so this can never claim a link that does not exist.
    availability: Mapped[str] = mapped_column(
        String(20), nullable=False, default=Availability.UNCONFIRMED.value
    )
    unavailable_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    # CUR — true while the entry is seeded by hand and unreconciled against
    # Marvel's guide + API. Surfaced in tool output so nobody mistakes a
    # provisional roster for a verified one.
    provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventIssue(Base):
    """Membership of an issue in an event's curated reading order. All CUR."""

    __tablename__ = "event_issues"
    __table_args__ = (
        UniqueConstraint("event_id", "issue_id", name="uq_event_issue"),
        UniqueConstraint("event_id", "position", name="uq_event_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(_EVENTS_ID, ondelete="CASCADE"), nullable=False,
        index=True,
    )
    issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(_ISSUES_ID, ondelete="CASCADE"), nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IssueRole.OPTIONAL_TIE_IN.value
    )
    # Clustering axes for sequence_bookmarks. Curated because neither is
    # derivable: publication order does not tell you a book is a flashback.
    narrative_role: Mapped[str] = mapped_column(String(20), nullable=False, default="parallel")
    franchise: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    # Free-text "why this is here", shown as provenance on the rack.
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


class IssueReference(Base):
    """A curated edge in the omnibus-footnote graph (SPEC §4).

    Plain adjacency table. Marvel's API has nothing like this; it is the whole
    hand-authored contribution of the project. Recursive CTE if "everything
    downstream of this issue" is ever needed — no graph database.
    """

    __tablename__ = "issue_references"
    __table_args__ = (
        UniqueConstraint(
            "from_issue_id", "to_issue_id", "relation_type", name="uq_reference_edge"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(_ISSUES_ID, ondelete="CASCADE"), nullable=False,
        index=True,
    )
    to_issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(_ISSUES_ID, ondelete="CASCADE"), nullable=False,
        index=True,
    )
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The omnibus page the footnote sits on, when known. This is the datum that
    # makes provenance on the rack read like a citation instead of a guess, and
    # the key a future QR-code insert would be generated against.
    omnibus_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
