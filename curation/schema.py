"""The curation layer: git-tracked YAML, one file per event (SPEC §3).

This is the half of the system that cannot come from any API. The reference
edges in particular are the omnibus-footnote citation graph, which exists in
print and nowhere else — digitizing it is the whole point of the project.

Everything here is pure: no DB, no network. That is deliberate, because the
data-quality suite (SPEC §8) runs over these files before any serving code is
involved, so curation is trustworthy from the start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from marvel.records import issue_key, series_slug
from models.types import (
    Availability,
    CurationStatus,
    Franchise,
    IssueRole,
    NarrativeRole,
    RelationType,
)

EVENTS_DIR = Path(__file__).resolve().parent / "events"


class CurationError(ValueError):
    """A YAML file that cannot be loaded at all (as opposed to one that loads
    but fails a quality gate — those come back as Problems from validate.py)."""


@dataclass(frozen=True)
class CuratedIssue:
    key: str
    position: int
    series_name: str
    issue_number: int
    title: str
    published_on: date | None
    role: IssueRole
    narrative_role: NarrativeRole
    franchise: Franchise
    availability: Availability
    #: Only ever set from a Marvel API response for this exact issue (Gate B).
    #: Present in YAML so curation is diffable, verified by test against cache.
    digital_id: int | None = None
    marvel_com_issue_id: int | None = None
    unavailable_note: str = ""
    #: Seeded by hand, not yet reconciled against Marvel's guide and API.
    provisional: bool = False
    note: str = ""

    @property
    def series_slug(self) -> str:
        return series_slug(self.series_name)

    @property
    def display(self) -> str:
        return f"{self.series_name} #{self.issue_number}"


@dataclass(frozen=True)
class CuratedReference:
    """One edge of the omnibus-footnote graph."""

    from_key: str
    to_key: str
    relation_type: RelationType
    note: str = ""
    omnibus_page: int | None = None


@dataclass(frozen=True)
class CuratedEvent:
    slug: str
    display_name: str
    curation_status: CurationStatus
    issues: list[CuratedIssue]
    references: list[CuratedReference]
    marvel_event_id: int | None = None
    started_on: date | None = None
    ended_on: date | None = None
    summary: str = ""
    #: The spine of the event: which series, and how many issues it runs.
    #: Lets the "every core issue is present and ordered" gate be a real check
    #: rather than a tautology over whatever happens to be in the file.
    core_series: str = ""
    core_count: int = 0
    source_path: Path | None = field(default=None, compare=False)

    @property
    def issues_by_key(self) -> dict[str, CuratedIssue]:
        return {issue.key: issue for issue in self.issues}

    @property
    def ordered(self) -> list[CuratedIssue]:
        return sorted(self.issues, key=lambda i: i.position)

    @property
    def core_issues(self) -> list[CuratedIssue]:
        return [i for i in self.ordered if i.role is IssueRole.CORE]

    @property
    def is_curated(self) -> bool:
        return self.curation_status is CurationStatus.CURATED


def _enum(cls, raw: Any, field_name: str, where: str):
    try:
        return cls(raw)
    except ValueError as exc:
        allowed = ", ".join(sorted(m.value for m in cls))
        raise CurationError(
            f"{where}: {field_name}={raw!r} is not one of: {allowed}"
        ) from exc


def _date(raw: Any, field_name: str, where: str) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise CurationError(f"{where}: {field_name}={raw!r} is not an ISO date") from exc


def _parse_issue(raw: dict, where: str) -> CuratedIssue:
    series_name = raw.get("series")
    number = raw.get("number")
    if not series_name or number is None:
        raise CurationError(f"{where}: every issue needs `series` and `number`")
    derived_key = issue_key(series_name, number)
    key = raw.get("key") or derived_key
    if key != derived_key:
        # A hand-written key that disagrees with series+number would make
        # reference edges point somewhere the issue itself doesn't live.
        raise CurationError(
            f"{where}: key {key!r} does not match series+number (expected {derived_key!r})"
        )
    if "position" not in raw:
        raise CurationError(f"{where}: issue {key!r} has no `position`")

    availability_raw = raw.get("availability")
    if availability_raw is None:
        # A digital_id is the only thing that makes an issue linkable, so the
        # default follows from the data rather than needing to be restated.
        availability_raw = (
            Availability.LINKABLE.value if raw.get("digital_id") else
            Availability.UNCONFIRMED.value
        )
    return CuratedIssue(
        key=key,
        position=int(raw["position"]),
        series_name=series_name,
        issue_number=int(number),
        title=raw.get("title") or "",
        published_on=_date(raw.get("published_on"), "published_on", f"{where}:{key}"),
        role=_enum(IssueRole, raw.get("role", "optional_tie_in"), "role", f"{where}:{key}"),
        narrative_role=_enum(
            NarrativeRole, raw.get("narrative_role", "parallel"), "narrative_role",
            f"{where}:{key}",
        ),
        franchise=_enum(
            Franchise, raw.get("franchise", "other"), "franchise", f"{where}:{key}"
        ),
        availability=_enum(
            Availability, availability_raw, "availability", f"{where}:{key}"
        ),
        digital_id=raw.get("digital_id"),
        marvel_com_issue_id=raw.get("marvel_com_issue_id"),
        unavailable_note=raw.get("unavailable_note") or "",
        provisional=bool(raw.get("provisional", False)),
        note=raw.get("note") or "",
    )


def _parse_reference(raw: dict, where: str) -> CuratedReference:
    if not raw.get("from") or not raw.get("to"):
        raise CurationError(f"{where}: every reference needs `from` and `to`")
    return CuratedReference(
        from_key=raw["from"],
        to_key=raw["to"],
        relation_type=_enum(
            RelationType, raw.get("type", "references"), "type",
            f"{where}:{raw['from']}->{raw['to']}",
        ),
        note=raw.get("note") or "",
        omnibus_page=raw.get("omnibus_page"),
    )


def parse_event(data: dict, *, source_path: Path | None = None) -> CuratedEvent:
    where = str(source_path or "<inline>")
    if not data.get("slug"):
        raise CurationError(f"{where}: missing `slug`")
    core = data.get("core") or {}
    return CuratedEvent(
        slug=data["slug"],
        display_name=data.get("display_name") or data["slug"],
        curation_status=_enum(
            CurationStatus, data.get("curation_status", "best_effort"),
            "curation_status", where,
        ),
        marvel_event_id=data.get("marvel_event_id"),
        started_on=_date(data.get("started_on"), "started_on", where),
        ended_on=_date(data.get("ended_on"), "ended_on", where),
        summary=data.get("summary") or "",
        core_series=core.get("series") or "",
        core_count=int(core.get("count") or 0),
        issues=[_parse_issue(i, where) for i in data.get("issues") or []],
        references=[_parse_reference(r, where) for r in data.get("references") or []],
        source_path=source_path,
    )


def load_event_file(path: Path) -> CuratedEvent:
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    return parse_event(data, source_path=path)


def load_all_events(events_dir: Path | None = None) -> list[CuratedEvent]:
    directory = events_dir or EVENTS_DIR
    events = [load_event_file(p) for p in sorted(directory.glob("*.yaml"))]
    slugs = [e.slug for e in events]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        raise CurationError(f"duplicate event slugs across files: {sorted(duplicates)}")
    return events
