"""Project curation YAML into the database.

SPEC §3 keeps curation in git-tracked YAML and merges it with cached Marvel
data at read time. This is that merge: the loader writes only curation-owned
columns, and `marvel/sync.py` writes only API-owned ones, so the two layers can
never overwrite each other regardless of which runs last.

Idempotent, and safe to run on every boot (see lifespan.py) — which is what
makes "edit YAML, deploy, done" the actual curation workflow.

**Heroku note.** The filesystem is ephemeral (SPEC §2), so the web curation view
cannot write these files. It edits the DB and offers a YAML export to commit
back to git; anything not exported is lost on the next deploy. That is the
honest cost of ephemeral storage, and the export is the answer to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from curation.schema import CuratedEvent, CuratedIssue, load_all_events
from curation.validate import check_digital_ids_traceable
from marvel.records import ComicRecord
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.types import Availability

log = logging.getLogger(__name__)


class UntraceableDigitalId(RuntimeError):
    """A curated digital_id could not be traced to a Marvel response.

    Raised rather than logged: Gate B says an unverified id is worse than no id,
    because it opens the wrong comic instead of failing. Refusing to load is the
    only safe response.
    """


@dataclass
class LoadReport:
    events: int = 0
    issues_created: int = 0
    issues_updated: int = 0
    memberships: int = 0
    references: int = 0
    #: Issue rows belonging to no event any more. Renaming an issue in the YAML
    #: changes its key, and the old row stays behind — deliberately, because a
    #: bookmark may still point at it and deleting would silently empty someone's
    #: rack card. Reported so a curator knows the residue exists.
    orphaned_issues: int = 0

    def summary(self) -> str:
        line = (
            f"loaded {self.events} event(s): {self.issues_created} new issues, "
            f"{self.issues_updated} updated, {self.memberships} memberships, "
            f"{self.references} reference edges"
        )
        if self.orphaned_issues:
            line += (
                f"\n  note: {self.orphaned_issues} issue row(s) belong to no event — "
                "usually left by a rename. Harmless, but a bookmark pointing at one "
                "will show a card no reading order can place."
            )
        return line


def _apply_curated(issue: Issue, curated: CuratedIssue) -> None:
    """Write the curation-owned columns. Never touches an API-owned column.

    Identity fields (series_name/number/published_on) are seeded here only
    while null, so a sync's canonical values win once they exist — curation
    should not be arguing with Marvel about a cover date.
    """
    issue.key = curated.key
    issue.issue_number = curated.issue_number
    issue.availability = curated.availability.value
    issue.unavailable_note = curated.unavailable_note
    issue.provisional = curated.provisional
    if not issue.series_name:
        issue.series_name = curated.series_name
        issue.series_slug = curated.series_slug
    if not issue.title:
        issue.title = curated.title or curated.display
    if issue.published_on is None:
        issue.published_on = curated.published_on
    if curated.marvel_com_issue_id and issue.marvel_com_issue_id is None:
        issue.marvel_com_issue_id = curated.marvel_com_issue_id
        issue.source_id = curated.marvel_com_issue_id
    # A digital_id only reaches the DB via a traceability check upstream.
    if curated.digital_id and issue.digital_id is None:
        issue.digital_id = curated.digital_id
    # Availability must never overstate what we can link.
    if issue.availability == Availability.LINKABLE.value and not issue.digital_id:
        issue.availability = Availability.UNCONFIRMED.value


async def load_event(
    session: AsyncSession,
    curated: CuratedEvent,
    *,
    record_index: dict[str, ComicRecord] | None = None,
    report: LoadReport | None = None,
) -> LoadReport:
    report = report or LoadReport()

    gate_b = check_digital_ids_traceable(curated, record_index)
    if gate_b:
        raise UntraceableDigitalId(
            f"refusing to load {curated.slug!r}:\n  "
            + "\n  ".join(str(p) for p in gate_b)
        )

    event = await session.scalar(select(Event).where(Event.slug == curated.slug))
    if event is None:
        event = Event(slug=curated.slug)
        session.add(event)
    event.display_name = curated.display_name
    event.curation_status = curated.curation_status.value
    event.started_on = curated.started_on
    event.ended_on = curated.ended_on
    event.summary = curated.summary
    event.updated_at = datetime.now(UTC)
    if curated.marvel_event_id is not None:
        event.marvel_event_id = curated.marvel_event_id
    await session.flush()

    issues_by_key: dict[str, Issue] = {}
    for curated_issue in curated.ordered:
        issue = await session.scalar(select(Issue).where(Issue.key == curated_issue.key))
        if issue is None:
            issue = Issue(key=curated_issue.key, series_name="", series_slug="", issue_number=0)
            session.add(issue)
            report.issues_created += 1
        else:
            report.issues_updated += 1
        _apply_curated(issue, curated_issue)
        issues_by_key[curated_issue.key] = issue
    await session.flush()

    # Membership and edges are rebuilt wholesale: the YAML is authoritative, so
    # a removed line has to actually disappear rather than linger as a stale row.
    await session.execute(delete(EventIssue).where(EventIssue.event_id == event.id))
    for curated_issue in curated.ordered:
        session.add(
            EventIssue(
                event_id=event.id,
                issue_id=issues_by_key[curated_issue.key].id,
                position=curated_issue.position,
                role=curated_issue.role.value,
                narrative_role=curated_issue.narrative_role.value,
                franchise=curated_issue.franchise.value,
                note=curated_issue.note,
            )
        )
        report.memberships += 1

    issue_ids = [issue.id for issue in issues_by_key.values()]
    if issue_ids:
        await session.execute(
            delete(IssueReference).where(IssueReference.from_issue_id.in_(issue_ids))
        )
    for ref in curated.references:
        source = issues_by_key.get(ref.from_key)
        target = issues_by_key.get(ref.to_key)
        if source is None or target is None:
            # validate.check_references_resolve turns this into a test failure;
            # at load time, skip rather than crash a boot on one bad edge.
            log.warning(
                "skipping reference %s -> %s in %s: unknown issue",
                ref.from_key, ref.to_key, curated.slug,
            )
            continue
        session.add(
            IssueReference(
                from_issue_id=source.id,
                to_issue_id=target.id,
                relation_type=ref.relation_type.value,
                note=ref.note,
                omnibus_page=ref.omnibus_page,
            )
        )
        report.references += 1

    report.events += 1
    await session.commit()
    return report


async def count_orphaned_issues(session: AsyncSession) -> int:
    """Issue rows with no event membership."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Issue)
            .outerjoin(EventIssue, EventIssue.issue_id == Issue.id)
            .where(EventIssue.id.is_(None))
        )
        or 0
    )


async def load_all(
    session: AsyncSession, *, record_index: dict[str, ComicRecord] | None = None
) -> LoadReport:
    report = LoadReport()
    for curated in load_all_events():
        await load_event(session, curated, record_index=record_index, report=report)
    report.orphaned_issues = await count_orphaned_issues(session)
    return report
