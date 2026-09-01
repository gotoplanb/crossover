"""Vendored catalog snapshots — the replacement for the discontinued Marvel API.

Marvel shut down `developer.marvel.com` and `gateway.marvel.com` (docs/gates.md),
so there is no live source for `digital_id`, the only input to a Marvel Unlimited
reader URL. A third-party mirror still carries the data; `scripts/fetch_snapshot.py`
captures it once into `curation/snapshots/<slug>.json` and that file is committed.

**Vendored rather than fetched at runtime**, for two reasons. A single-operator
mirror can disappear exactly as Marvel's did, and a reading guide that stops
linking because someone else's side project went down is a bad guide. And the
data is not volatile — an issue's digital id does not change — so a snapshot is
the honest shape for it.

Snapshots are written in Marvel's response envelope, so `marvel.records` parses
them and `marvel.sync.apply_record` applies them with no special casing. The
only thing that differs is the `source` stamped on each `digital_id`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.cache import record_index, store
from marvel.client import MarvelResponse
from marvel.records import ComicRecord
from marvel.sync import SyncReport, apply_record, promote_availability
from models.catalog import Event, EventIssue, Issue

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "curation" / "snapshots"

#: Endpoint key snapshots are cached under. Deliberately not a real Marvel path:
#: the raw cache doubles as the Gate B evidence store, so a reader of that table
#: must be able to tell at a glance that a row came from a snapshot rather than
#: from Marvel's own API.
CACHE_ENDPOINT_PREFIX = "snapshot"


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    slug: str
    payload: dict[str, Any]

    @property
    def provenance(self) -> dict[str, Any]:
        return self.payload.get("_provenance") or {}

    @property
    def source_label(self) -> str:
        """What gets stamped on every `digital_id` this snapshot supplies."""
        return f"{CACHE_ENDPOINT_PREFIX}:{self.slug}"

    @property
    def cache_endpoint(self) -> str:
        return f"{CACHE_ENDPOINT_PREFIX}:{self.slug}"

    @property
    def records(self) -> dict[str, ComicRecord]:
        return record_index([self.payload])

    @property
    def attribution(self) -> str:
        return self.payload.get("attributionText", "")


def load_snapshot(slug: str, directory: Path | None = None) -> Snapshot:
    path = (directory or SNAPSHOT_DIR) / f"{slug}.json"
    if not path.exists():
        raise SnapshotError(
            f"no snapshot for {slug!r} at {path}. "
            "Build one with `python -m scripts.fetch_snapshot <slug>`."
        )
    with path.open() as fh:
        return Snapshot(slug=slug, payload=json.load(fh))


def available(directory: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (directory or SNAPSHOT_DIR).glob("*.json"))


def load_all(directory: Path | None = None) -> list[Snapshot]:
    return [load_snapshot(slug, directory) for slug in available(directory)]


def combined_record_index(directory: Path | None = None) -> dict[str, ComicRecord]:
    """Every record across every snapshot, for the Gate B traceability check."""
    return record_index(s.payload for s in load_all(directory))


async def apply(session: AsyncSession, snapshot: Snapshot) -> SyncReport:
    """Write a snapshot's API-derived fields onto the matching issue rows.

    Only touches issues that curation already knows about — a snapshot record
    with no curated issue is counted and skipped, never used to invent a catalog
    row with no place in any reading order.
    """
    # Keep the raw payload in the cache too, so the evidence store stays the
    # single place to answer "where did this digital_id come from".
    await store(
        session,
        MarvelResponse(
            endpoint=snapshot.cache_endpoint,
            params={},
            body=snapshot.payload,
            etag="",
        ),
    )

    # Scoped to the event's own issues, not every issue in the database. An
    # unscoped query made each snapshot report every *other* event's issues as
    # "unmatched" — noise that would grow with each event added, and that also
    # swept up orphan rows left behind when curation renames an issue.
    rows = (
        await session.execute(
            select(Issue)
            .join(EventIssue, EventIssue.issue_id == Issue.id)
            .join(Event, Event.id == EventIssue.event_id)
            .where(Event.slug == snapshot.slug)
        )
    ).scalars().all()
    by_key = {issue.key: issue for issue in rows}
    index = snapshot.records

    matched = newly_linkable = 0
    for key, record in index.items():
        issue = by_key.get(key)
        if issue is None:
            continue
        matched += 1
        apply_record(issue, record, source=snapshot.source_label)
        if promote_availability(issue):
            newly_linkable += 1

    await session.commit()
    return SyncReport(
        event_slug=snapshot.slug,
        pages_fetched=1,
        records_seen=len(index),
        issues_matched=matched,
        issues_unmatched=[k for k in by_key if k not in index],
        digital_ids_confirmed=sum(1 for r in index.values() if r.digital_id),
        newly_linkable=newly_linkable,
    )


async def apply_all(
    session: AsyncSession, directory: Path | None = None
) -> list[SyncReport]:
    return [await apply(session, snap) for snap in load_all(directory)]
