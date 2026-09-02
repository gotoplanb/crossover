"""Read-only guide logic: list events, render a guide, answer "what's next".

Shared by the MCP tools and the web views so the two surfaces can never drift
on what an issue's link or role is (SPEC §7: they divide by *moment*, not by
capability).

Every payload here renders links through `marvel.links`, so the Gate A and
Gate B rules are structural rather than remembered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.links import NOT_ON_MU, attribution, build_link
from marvel.records import cover_url
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.types import IssueRole, RelationType

#: How each edge type reads as a sentence to a reader mid-chapter. The whole
#: point of the reference graph is that it explains *why* to go read something,
#: so the phrasing matters as much as the edge.
_RELATION_PHRASING = {
    RelationType.EXPANDED_IN: "expanded in",
    RelationType.CONTINUES_IN: "continues in",
    RelationType.FLASHBACK_IN: "the flashback is in",
    RelationType.PARALLEL_TO: "happening in parallel in",
    RelationType.EPILOGUE_IN: "the epilogue is in",
    RelationType.PRELUDE_TO: "sets up",
    RelationType.REFERENCES: "referenced in",
}


def relation_phrase(relation_type: str) -> str:
    """Render an edge type as a phrase. Never raises.

    A missing phrasing is a cosmetic gap, and a page that 500s because someone
    added an enum member is not. tests/unit/test_relation_phrasing.py keeps the
    map complete so the fallback stays theoretical.
    """
    try:
        return _RELATION_PHRASING[RelationType(relation_type)]
    except (ValueError, KeyError):
        return "related to"


@dataclass
class GuideEntry:
    """One issue in one event's order, with everything a surface needs."""

    key: str
    position: int
    series_name: str
    issue_number: int
    title: str
    published_on: date | None
    role: str
    narrative_role: str
    franchise: str
    note: str
    availability: str
    provisional: bool
    digital_id: int | None
    source_id: int | None
    #: When Marvel Unlimited releases it, when that is known and still ahead.
    #: `build_link` reads this to refuse a link that would not open yet.
    unlimited_on: date | None = None
    issue_id: UUID | None = None
    thumbnail_path: str | None = None
    thumbnail_extension: str | None = None
    characters: list[str] = field(default_factory=list)
    unavailable_note: str = ""

    @property
    def display(self) -> str:
        return f"{self.series_name} #{self.issue_number}"

    @property
    def is_core(self) -> bool:
        return self.role == IssueRole.CORE.value

    @property
    def cover_url(self) -> str | None:
        return cover_url(self.thumbnail_path, self.thumbnail_extension)

    def link_markdown(self) -> str:
        """A tappable markdown link, or the literal "not on Marvel Unlimited".

        Never a bare URL and never a code span — see marvel/links.py.
        """
        return build_link(self, self.display).markdown

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "position": self.position,
            "issue": self.display,
            "series": self.series_name,
            "number": self.issue_number,
            "published_on": self.published_on.isoformat() if self.published_on else None,
            "role": self.role,
            "narrative_role": self.narrative_role,
            "franchise": self.franchise,
            "link": self.link_markdown(),
        }
        # Only when it is still ahead: a release date that has passed explains
        # nothing, and the link speaks for itself. Present, the caller can say
        # "on Marvel Unlimited from 12 March" instead of just "not available" —
        # which is the whole reason the date is stored.
        if self.unlimited_on and self.unlimited_on > date.today():
            payload["on_marvel_unlimited_from"] = self.unlimited_on.isoformat()
        if self.note:
            payload["note"] = self.note
        if self.unavailable_note:
            payload["why_unavailable"] = self.unavailable_note
        if self.provisional:
            # Surfaced rather than hidden: a reader deserves to know the entry
            # has not been reconciled against Marvel's own guide yet.
            payload["provisional"] = True
        return payload


@dataclass
class EventSummary:
    slug: str
    display_name: str
    curation_status: str
    started_on: date | None
    ended_on: date | None
    summary: str
    issue_count: int
    core_count: int
    linkable_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.display_name,
            "curation": self.curation_status,
            "window": " to ".join(
                d.isoformat() for d in (self.started_on, self.ended_on) if d
            ),
            "summary": self.summary,
            "issues": self.issue_count,
            "core_issues": self.core_count,
            "linkable_on_marvel_unlimited": self.linkable_count,
        }


def _entry_from_rows(issue: Issue, membership: EventIssue) -> GuideEntry:
    return GuideEntry(
        key=issue.key,
        position=membership.position,
        series_name=issue.series_name,
        issue_number=issue.issue_number,
        title=issue.title,
        published_on=issue.published_on,
        role=membership.role,
        narrative_role=membership.narrative_role,
        franchise=membership.franchise,
        note=membership.note,
        availability=issue.availability,
        provisional=issue.provisional,
        digital_id=issue.digital_id,
        source_id=issue.source_id,
        unlimited_on=issue.unlimited_on,
        issue_id=issue.id,
        thumbnail_path=issue.thumbnail_path,
        thumbnail_extension=issue.thumbnail_extension,
        characters=list(issue.characters or []),
        unavailable_note=issue.unavailable_note,
    )


async def list_events(session: AsyncSession) -> list[EventSummary]:
    events = (await session.scalars(select(Event).order_by(Event.display_name))).all()
    summaries: list[EventSummary] = []
    for event in events:
        rows = (
            await session.execute(
                select(EventIssue, Issue)
                .join(Issue, Issue.id == EventIssue.issue_id)
                .where(EventIssue.event_id == event.id)
            )
        ).all()
        summaries.append(
            EventSummary(
                slug=event.slug,
                display_name=event.display_name,
                curation_status=event.curation_status,
                started_on=event.started_on,
                ended_on=event.ended_on,
                summary=event.summary,
                issue_count=len(rows),
                core_count=sum(1 for m, _ in rows if m.role == IssueRole.CORE.value),
                linkable_count=sum(1 for _, i in rows if i.digital_id),
            )
        )
    return summaries


async def event_entries(session: AsyncSession, event_slug: str) -> tuple[Event, list[GuideEntry]]:
    event = await session.scalar(select(Event).where(Event.slug == event_slug))
    if event is None:
        raise ValueError(f"no event with slug {event_slug!r}")
    rows = (
        await session.execute(
            select(EventIssue, Issue)
            .join(Issue, Issue.id == EventIssue.issue_id)
            .where(EventIssue.event_id == event.id)
            .order_by(EventIssue.position)
        )
    ).all()
    return event, [_entry_from_rows(issue, membership) for membership, issue in rows]


async def all_entries(session: AsyncSession) -> list[GuideEntry]:
    """Every curated issue across every event — the resolution pool for a
    spoken `issue_ref`, which is not scoped to an event."""
    rows = (
        await session.execute(
            select(EventIssue, Issue)
            .join(Issue, Issue.id == EventIssue.issue_id)
            .order_by(EventIssue.position)
        )
    ).all()
    return [_entry_from_rows(issue, membership) for membership, issue in rows]


async def get_event_guide(session: AsyncSession, event_slug: str) -> dict[str, Any]:
    event, entries = await event_entries(session, event_slug)
    unavailable = sum(1 for e in entries if not e.digital_id)
    payload = {
        "event": event.display_name,
        "slug": event.slug,
        "curation": event.curation_status,
        "summary": event.summary,
        "reading_order": [e.to_payload() for e in entries],
        "attribution": attribution(),
    }
    if unavailable:
        payload["note"] = (
            f"{unavailable} of {len(entries)} issues have no confirmed Marvel Unlimited "
            f"id yet and show as \"{NOT_ON_MU}\". They are not necessarily absent from "
            "Marvel Unlimited — they are unverified, and an unverified id opens the "
            "wrong comic rather than failing."
        )
    return payload


async def references_from(
    session: AsyncSession, issue_id: UUID
) -> list[tuple[IssueReference, Issue]]:
    rows = (
        await session.execute(
            select(IssueReference, Issue)
            .join(Issue, Issue.id == IssueReference.to_issue_id)
            .where(IssueReference.from_issue_id == issue_id)
            .order_by(IssueReference.id)
        )
    ).all()
    return [(ref, issue) for ref, issue in rows]


async def whats_next(session: AsyncSession, entry: GuideEntry) -> dict[str, Any]:
    """Given "we just finished X": the next core issue, what sits between, and
    which tie-ins expand on X.

    **This is the reason the project is an MCP and not a static webpage**
    (SPEC §6): the answer depends on where the reader is, and the interesting
    half of it — the reference edges — is hand-curated data that no sort order
    could produce.
    """
    membership_rows = (
        await session.execute(
            select(EventIssue, Event)
            .join(Event, Event.id == EventIssue.event_id)
            .where(EventIssue.issue_id == entry.issue_id)
        )
    ).all()
    if not membership_rows:
        return {
            "you_just_finished": entry.to_payload(),
            "next": None,
            "note": "This issue isn't part of a curated event, so there's no reading order "
                    "to advance through.",
            "attribution": attribution(),
        }

    _, event = membership_rows[0]
    _, entries = await event_entries(session, event.slug)
    after = [e for e in entries if e.position > entry.position]
    next_core = next((e for e in after if e.is_core), None)
    between = [e for e in after if next_core is None or e.position < next_core.position]

    expands: list[dict[str, Any]] = []
    if entry.issue_id is not None:
        for ref, target in await references_from(session, entry.issue_id):
            label = f"{target.series_name} #{target.issue_number}"
            target_membership = await session.scalar(
                select(EventIssue).where(EventIssue.issue_id == target.id)
            )
            # An edge may point at an issue that belongs to no event (a
            # reference out of the curated set); it still gets a link, built
            # from the issue row directly.
            link = (
                _entry_from_rows(target, target_membership).link_markdown()
                if target_membership is not None
                else build_link(target, label).markdown
            )
            expands.append(
                {
                    "issue": label,
                    "relation": relation_phrase(ref.relation_type),
                    "why": ref.note,
                    "omnibus_page": ref.omnibus_page,
                    "link": link,
                }
            )

    return {
        "you_just_finished": entry.to_payload(),
        "event": event.display_name,
        "next_core_issue": next_core.to_payload() if next_core else None,
        "read_before_next_core": [e.to_payload() for e in between],
        "expands_on_what_you_just_read": expands,
        "at_the_end": next_core is None and not between,
        "attribution": attribution(),
    }
