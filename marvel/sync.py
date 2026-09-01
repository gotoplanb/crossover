"""Refetch path: Marvel API → raw cache → the API-owned columns on `issues`.

**The one invariant here** (SPEC §3): a refetch must never be able to clobber
hand-curation. That is enforced structurally — `apply_record` writes only the
columns in `API_OWNED_COLUMNS`, and a test asserts no curated column ever
appears in that set. Order, roles, narrative role, franchise, reference edges,
and availability flags are untouchable from this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel import cache
from marvel.client import MarvelClient
from marvel.records import ComicRecord
from models.catalog import Event, Issue
from models.types import Availability

#: Every column a refetch is permitted to write. Adding a curated column here
#: is the bug this list exists to prevent — see tests/unit/test_ownership.py.
API_OWNED_COLUMNS = frozenset(
    {
        "series_name",
        "series_slug",
        "title",
        "published_on",
        "marvel_com_issue_id",
        "digital_id",
        "source_id",
        "thumbnail_path",
        "thumbnail_extension",
        "characters",
        "creators",
        "synced_at",
    }
)

#: Columns the curation loader owns. Disjoint from the above, by test.
CURATION_OWNED_COLUMNS = frozenset(
    {"key", "issue_number", "availability", "unavailable_note", "provisional"}
)


def apply_record(issue: Issue, record: ComicRecord) -> None:
    """Copy the API-derived fields of `record` onto `issue`. Nothing else.

    `availability` is deliberately *not* set here even though this is the only
    place that learns a digital_id exists — promoting an issue to LINKABLE is
    curation's call, made by `promote_availability` below, so the decision has
    exactly one home.
    """
    values = {
        "series_name": record.series_name,
        "series_slug": record.series_slug,
        "title": record.title,
        "published_on": record.published_on,
        "marvel_com_issue_id": record.marvel_com_issue_id,
        "digital_id": record.digital_id,
        "source_id": record.source_id,
        "thumbnail_path": record.thumbnail_path,
        "thumbnail_extension": record.thumbnail_extension,
        "characters": record.characters,
        "creators": record.creators,
        "synced_at": datetime.now(UTC),
    }
    assert set(values) <= API_OWNED_COLUMNS, "apply_record tried to write a non-API column"
    for column, value in values.items():
        # Don't overwrite a known value with a null: a sparse record from a
        # search endpoint shouldn't erase what a richer event fetch established.
        if value is not None or getattr(issue, column) is None:
            setattr(issue, column, value)


def promote_availability(issue: Issue) -> bool:
    """Move an UNCONFIRMED issue to LINKABLE once a real digital_id landed.

    Returns True if it changed. UNAVAILABLE is never overridden — that is a
    curated statement of fact ("this genuinely isn't on Marvel Unlimited") and
    a sync has no standing to argue with it.
    """
    if issue.availability == Availability.UNAVAILABLE.value:
        return False
    target = (
        Availability.LINKABLE.value
        if issue.digital_id
        else Availability.UNCONFIRMED.value
    )
    if issue.availability == target:
        return False
    issue.availability = target
    return True


@dataclass
class SyncReport:
    """What a sync did, in the terms SPEC §0 asks about: how many issues came
    back with a usable digital id."""

    event_slug: str
    pages_fetched: int
    records_seen: int
    issues_matched: int
    issues_unmatched: list[str]
    digital_ids_confirmed: int
    newly_linkable: int

    @property
    def digital_id_coverage(self) -> float:
        if not self.records_seen:
            return 0.0
        return self.digital_ids_confirmed / self.records_seen

    def summary(self) -> str:
        pct = round(self.digital_id_coverage * 100)
        lines = [
            f"event:            {self.event_slug}",
            f"pages fetched:    {self.pages_fetched}",
            f"records seen:     {self.records_seen}",
            f"digital ids:      {self.digital_ids_confirmed} ({pct}% coverage)",
            f"curated matched:  {self.issues_matched}",
            f"newly linkable:   {self.newly_linkable}",
        ]
        if self.issues_unmatched:
            lines.append(
                "unmatched curated issues (no Marvel record found): "
                + ", ".join(sorted(self.issues_unmatched))
            )
        if self.digital_id_coverage < 0.5:
            lines.append(
                "WARNING: under half the roster has a digital id. SPEC §0 says to "
                "rethink the linking premise before curating further."
            )
        return "\n".join(lines)


async def sync_event(
    session: AsyncSession, client: MarvelClient, event_slug: str
) -> SyncReport:
    """Fetch an event's roster, cache it verbatim, and update API-owned columns.

    Issues present in curation but absent from Marvel's roster are reported,
    not invented: event tagging quality varies (SPEC §0), and a curated issue
    Marvel doesn't tag is a curation fact worth seeing, not an error.
    """
    event = await session.scalar(select(Event).where(Event.slug == event_slug))
    if event is None:
        raise ValueError(f"no event with slug {event_slug!r}")
    if event.marvel_event_id is None:
        raise ValueError(
            f"event {event_slug!r} has no marvel_event_id — set it in the curation YAML "
            "so /events/{id}/comics can be called"
        )

    pages = await client.all_event_comics(event.marvel_event_id)
    for page in pages:
        await cache.store(session, page)
    index = cache.record_index(page.body for page in pages)

    issues = (await session.scalars(select(Issue))).all()
    by_key = {issue.key: issue for issue in issues}

    matched = 0
    newly_linkable = 0
    for key, record in index.items():
        issue = by_key.get(key)
        if issue is None:
            continue
        matched += 1
        apply_record(issue, record)
        if promote_availability(issue):
            newly_linkable += 1

    await session.commit()
    return SyncReport(
        event_slug=event_slug,
        pages_fetched=len(pages),
        records_seen=len(index),
        issues_matched=matched,
        issues_unmatched=[k for k in by_key if k not in index],
        digital_ids_confirmed=sum(1 for r in index.values() if r.digital_id),
        newly_linkable=newly_linkable,
    )
