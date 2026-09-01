"""Bookmarks: the sticky note, and the pile it becomes.

SPEC §2 sets the bar for the write path: capturing a breadcrumb must cost less
attention than just looking the thing up, or the project has failed. In practice
that means `bookmark_issue` takes a loose spoken reference and one optional
sentence, does everything else itself, and never asks a follow-up question it
can avoid.

The read path (`sequence_bookmarks`) is the payoff, and it is deliberately not
a sort — see service/sequencing.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.links import attribution, build_link
from models.bookmark import Bookmark
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.types import BookmarkOrigin, BookmarkStatus, IssueRole
from observability import metrics
from service.guide import GuideEntry, relation_phrase
from service.sequencing import SeqItem, chronological, sequence_payload


async def _anchor_for(session: AsyncSession, event_id: UUID | None, position: int | None) -> str:
    """The nearest preceding core issue — the in-universe timing anchor.

    Computed at write time and stored, because SPEC §4 requires that ordering a
    reading list needs zero follow-up calls.
    """
    if event_id is None or position is None:
        return ""
    row = (
        await session.execute(
            select(Issue.series_name, Issue.issue_number)
            .join(EventIssue, EventIssue.issue_id == Issue.id)
            .where(
                EventIssue.event_id == event_id,
                EventIssue.role == IssueRole.CORE.value,
                EventIssue.position <= position,
            )
            .order_by(EventIssue.position.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return ""
    return f"around {row[0]} #{row[1]}"


async def create_bookmark(
    session: AsyncSession,
    *,
    user_id: UUID,
    entry: GuideEntry,
    note: str = "",
    omnibus_page: int | None = None,
    origin: BookmarkOrigin = BookmarkOrigin.MID_READ,
    status: BookmarkStatus = BookmarkStatus.CONFIRMED,
    raw_text: str = "",
    source_reference_id: int | None = None,
    provenance: str = "",
) -> Bookmark:
    """Save one issue, denormalizing everything sequencing will need."""
    membership_row = (
        await session.execute(
            select(EventIssue, Event)
            .join(Event, Event.id == EventIssue.event_id)
            .where(EventIssue.issue_id == entry.issue_id)
            .limit(1)
        )
    ).first()
    membership, event = membership_row if membership_row else (None, None)

    bookmark = Bookmark(
        user_id=user_id,
        issue_id=entry.issue_id,
        note=note or "",
        origin=origin.value,
        status=status.value,
        raw_text=raw_text,
        omnibus_page=omnibus_page,
        source_reference_id=source_reference_id,
        provenance=provenance,
        # denormalized
        event_id=event.id if event else None,
        event_slug=event.slug if event else "",
        event_name=event.display_name if event else "",
        position=membership.position if membership else None,
        role=membership.role if membership else "",
        narrative_role=membership.narrative_role if membership else "",
        franchise=membership.franchise if membership else "",
        series_name=entry.series_name,
        issue_number=entry.issue_number,
        published_on=entry.published_on,
        thumbnail_path=entry.thumbnail_path,
        thumbnail_extension=entry.thumbnail_extension,
        characters=entry.characters or None,
        digital_id=entry.digital_id,
        source_id=entry.source_id,
        availability=entry.availability,
    )
    session.add(bookmark)
    await session.commit()
    await session.refresh(bookmark)
    metrics.record_bookmark(origin.value, status.value)
    return bookmark


async def provenance_for(session: AsyncSession, entry: GuideEntry) -> tuple[int | None, str]:
    """Find the reference edge that most likely prompted this save.

    A bookmark with no remembered reason is noise a week later (SPEC §7), so
    when the saved issue is the target of exactly one curated footnote, that
    footnote becomes the card's provenance automatically — no extra question
    asked of someone holding a book.
    """
    if entry.issue_id is None:
        return None, ""
    rows = (
        await session.execute(
            select(IssueReference, Issue)
            .join(Issue, Issue.id == IssueReference.from_issue_id)
            .where(IssueReference.to_issue_id == entry.issue_id)
        )
    ).all()
    if len(rows) != 1:
        return None, ""
    ref, source = rows[0]
    page = f", p. {ref.omnibus_page}" if ref.omnibus_page else ""
    phrase = relation_phrase(ref.relation_type)
    return ref.id, f"{phrase.capitalize()} {source.series_name} #{source.issue_number}{page}"


def bookmark_link(bookmark: Bookmark) -> str:
    label = f"{bookmark.series_name} #{bookmark.issue_number}"
    if bookmark.status != BookmarkStatus.CONFIRMED.value:
        # An unconfirmed match must never be linkable (Gate B).
        return "unconfirmed — not linked"
    return build_link(bookmark, label).markdown


def to_seq_item(bookmark: Bookmark, anchor: str = "") -> SeqItem:
    return SeqItem(
        bookmark_id=str(bookmark.id),
        key=str(bookmark.issue_id or bookmark.id),
        display=f"{bookmark.series_name} #{bookmark.issue_number}"
        if bookmark.issue_number is not None
        else (bookmark.raw_text or "unidentified"),
        series_name=bookmark.series_name,
        issue_number=bookmark.issue_number,
        event_slug=bookmark.event_slug,
        event_name=bookmark.event_name,
        position=bookmark.position,
        role=bookmark.role,
        narrative_role=bookmark.narrative_role,
        franchise=bookmark.franchise,
        published_on=bookmark.published_on,
        note=bookmark.note,
        provenance=bookmark.provenance,
        link=bookmark_link(bookmark),
        status=bookmark.status,
        origin=bookmark.origin,
        read=bookmark.read_at is not None,
        anchor=anchor,
        characters=list(bookmark.characters or []),
        raw_text=bookmark.raw_text,
    )


async def user_bookmarks(session: AsyncSession, user_id: UUID) -> list[Bookmark]:
    """Every bookmark for one user. The `user_id` filter is the entire
    isolation boundary between the two people sharing this deployment, so it
    lives in one function rather than being repeated per caller."""
    return list(
        (
            await session.scalars(
                select(Bookmark)
                .where(Bookmark.user_id == user_id)
                .order_by(Bookmark.created_at.desc())
            )
        ).all()
    )


async def seq_items(session: AsyncSession, user_id: UUID) -> list[SeqItem]:
    bookmarks = await user_bookmarks(session, user_id)
    items: list[SeqItem] = []
    for bookmark in bookmarks:
        anchor = await _anchor_for(session, bookmark.event_id, bookmark.position)
        items.append(to_seq_item(bookmark, anchor))
    return items


async def sequence_bookmarks(
    session: AsyncSession, user_id: UUID, *, ordering: str = "clustered"
) -> dict[str, Any]:
    items = await seq_items(session, user_id)
    if not items:
        return {
            "count": 0,
            "groups": [],
            "note": "Nothing saved yet. Use bookmark_issue while reading to add something.",
            "attribution": attribution(),
        }
    if ordering == "chronological":
        return {
            "count": len(items),
            "ordering": "chronological",
            "how_to_read_this": (
                "Strict publication order, as asked. Note that tie-ins jump around: "
                "flashbacks and epilogues will be interleaved with the parallel books."
            ),
            "issues": [i.to_payload() for i in chronological(items)],
            "attribution": attribution(),
        }
    return {**sequence_payload(items), "attribution": attribution()}


async def mark_read(
    session: AsyncSession, user_id: UUID, bookmark_id: UUID, *, read: bool = True
) -> Bookmark | None:
    bookmark = await session.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.user_id != user_id:
        return None
    bookmark.read_at = datetime.now(UTC) if read else None
    await session.commit()
    return bookmark
