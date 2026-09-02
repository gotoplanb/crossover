"""Filling in issue rows that a reader actually cares about.

The mirror holds tens of thousands of issues; a two-person reading list will
touch a few dozen. So enrichment is demand-driven — an issue earns a lookup by
being on somebody's shelf, never by existing.

Two ways a row ends up thin:

- An **off-event find** confirmed through `add_to_shelf` before this existed, or
  confirmed while the mirror was unreachable.
- A **curated issue** whose snapshot predates a field, so the roster knows the
  series and number but has no cover art.

Both are repaired the same way, and both go through `apply_record`, so this can
no more overwrite curation than a sync can.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.mirror import MIRROR_SOURCE, MirrorClient, Outcome
from marvel.records import series_slug
from marvel.sync import apply_record, promote_availability
from models.bookmark import Bookmark
from models.catalog import Issue
from observability.tracing import span

#: A conservative default. The mirror allows 60 requests a minute and each issue
#: costs one, so a default pass stays well inside a single window even if
#: something else is talking to the mirror at the same time.
DEFAULT_LIMIT = 25


@dataclass
class EnrichReport:
    """What a pass did, in terms worth printing."""

    examined: int = 0
    enriched: int = 0
    newly_linkable: int = 0
    #: No id to look up by — these need a snapshot refresh, not a lookup.
    no_lookup_id: list[str] = field(default_factory=list)
    #: The fetched record was not this issue. See `_is_same_issue`.
    identity_mismatch: list[str] = field(default_factory=list)
    #: The mirror had nothing, or was unreachable.
    unresolved: list[str] = field(default_factory=list)
    #: Ran out of request budget. Distinct from `unresolved` because it means
    #: something different to whoever is reading: these are worth retrying in a
    #: minute, whereas an unresolved row will be unresolved again.
    rate_limited: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"examined:        {self.examined}",
            f"enriched:        {self.enriched}",
            f"newly linkable:  {self.newly_linkable}",
        ]
        if self.rate_limited:
            lines.append(
                f"stopped early:   rate limited after {self.enriched} enriched — "
                f"{len(self.rate_limited)} left, retry in a minute"
            )
        for label, keys in (
            ("no lookup id", self.no_lookup_id),
            ("identity mismatch", self.identity_mismatch),
            ("unresolved", self.unresolved),
        ):
            if keys:
                lines.append(f"{label + ':':16} {len(keys)} — {', '.join(sorted(keys))}")
        return "\n".join(lines)


async def thin_issues(session: AsyncSession, *, limit: int = DEFAULT_LIMIT) -> list[Issue]:
    """Issues on somebody's shelf that are missing something worth having.

    Bookmarked-only on purpose: enriching the whole catalog would spend the
    request budget on issues nobody has looked at.
    """
    return list(
        (
            await session.scalars(
                select(Issue)
                .join(Bookmark, Bookmark.issue_id == Issue.id)
                .where(
                    or_(
                        Issue.thumbnail_path.is_(None),
                        Issue.digital_id.is_(None),
                    )
                )
                .order_by(Issue.key)
                .distinct()
                .limit(limit)
            )
        ).all()
    )


def _lookup_id(issue: Issue) -> int | None:
    """The mirror keys issues by the same id marvel.com uses, which is what
    `source_id` holds."""
    return issue.source_id or issue.marvel_com_issue_id


def _is_same_issue(issue: Issue, record) -> bool:
    """Gate B at the write boundary.

    A wrong id does not error — it quietly opens a different comic — so a
    fetched record is only allowed to write here if it is demonstrably *this*
    issue by series and number. Anything else is reported, not stored.
    """
    return (
        record.issue_number == issue.issue_number
        and series_slug(record.series_name) == issue.series_slug
    )


async def enrich_bookmarked_issues(
    session: AsyncSession,
    mirror: MirrorClient,
    *,
    limit: int = DEFAULT_LIMIT,
) -> EnrichReport:
    """Fill in thin bookmarked issues from the mirror. Best-effort throughout."""
    report = EnrichReport()
    with span("enrich.bookmarked_issues") as current:
        issues = await thin_issues(session, limit=limit)
        report.examined = len(issues)
        for issue in issues:
            lookup = _lookup_id(issue)
            if not lookup:
                report.no_lookup_id.append(issue.key)
                continue
            record, outcome = await mirror.record(lookup)
            if outcome is Outcome.RATE_LIMITED:
                # Stop rather than grind through the rest. The budget is
                # per-IP and shared, so the remaining lookups in this pass
                # would fail the same way — and reporting them as
                # "unresolved" would say something untrue about the issues.
                report.rate_limited = [i.key for i in issues[issues.index(issue) :]]
                break
            if record is None:
                report.unresolved.append(issue.key)
                continue
            if not _is_same_issue(issue, record):
                report.identity_mismatch.append(issue.key)
                continue
            apply_record(issue, record, source=MIRROR_SOURCE)
            report.enriched += 1
            if promote_availability(issue):
                report.newly_linkable += 1
        await session.commit()
        current.set_attribute("enrich.examined", report.examined)
        current.set_attribute("enrich.enriched", report.enriched)
    return report
