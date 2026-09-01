"""Vendored catalog snapshots — the replacement for the discontinued Marvel API.

The snapshot is now the *only* source of `digital_id`, which makes it the only
thing standing between a reader and Gate B's failure mode. So these tests care
about two things above all: that an id is only ever applied to the issue it
actually belongs to, and that its provenance is recorded.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from curation.validate import check_digital_ids_traceable
from marvel import snapshot as snapshots
from models.catalog import Issue
from models.types import Availability


def test_the_shipped_snapshot_covers_the_curated_roster(curated_events) -> None:
    """A snapshot that silently drops issues would leave them unlinkable with no
    signal that anything was missing."""
    snap = snapshots.load_snapshot("king-in-black")
    curated = {i.key for i in curated_events[0].issues}
    covered = set(snap.records)
    assert not (curated - covered), f"no snapshot record for: {sorted(curated - covered)}"


def test_every_shipped_record_has_a_digital_id() -> None:
    snap = snapshots.load_snapshot("king-in-black")
    missing = [k for k, r in snap.records.items() if not r.digital_id]
    assert missing == [], f"records with no digital id: {missing}"


def test_the_snapshot_records_its_own_provenance() -> None:
    """Gate B's amended rule requires the source to be recorded. A snapshot that
    does not say where it came from cannot satisfy it."""
    provenance = snapshots.load_snapshot("king-in-black").provenance
    for field in ("source", "source_name", "captured_at", "why", "verification"):
        assert provenance.get(field), f"provenance is missing {field}"
    assert provenance["unresolved"] == []


def test_the_snapshot_carries_marvels_attribution() -> None:
    """Their terms attach to the data, wherever it is served from."""
    assert "Marvel" in snapshots.load_snapshot("king-in-black").attribution


def test_a_missing_snapshot_says_how_to_build_one(tmp_path) -> None:
    with pytest.raises(snapshots.SnapshotError, match="fetch_snapshot"):
        snapshots.load_snapshot("secret-wars", tmp_path)


async def test_applying_the_snapshot_makes_the_roster_linkable(
    session, loaded_event
) -> None:
    report = await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))

    assert report.issues_matched == 40
    assert report.digital_ids_confirmed == 40
    assert report.newly_linkable == 40
    assert report.issues_unmatched == []

    issues = (await session.scalars(select(Issue))).all()
    assert all(i.availability == Availability.LINKABLE.value for i in issues)
    assert all(i.digital_id for i in issues)


async def test_every_applied_id_records_where_it_came_from(session, loaded_event) -> None:
    """Without this, "verified" is a claim in a commit message rather than a
    property of the row."""
    await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))
    sources = {
        i.digital_id_source for i in (await session.scalars(select(Issue))).all()
    }
    assert sources == {"snapshot:king-in-black"}


async def test_ids_land_on_the_issue_they_belong_to(session, loaded_event) -> None:
    """The Gate B check, end to end: the ids verified in a browser must be the
    ones that actually reach those rows."""
    await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))

    verified = {
        "king-in-black-1": (55807, 85649),
        "the-union-1": (54937, 82509),
        "venom-34": (56429, 89766),
        "king-in-black-namor-1": (55812, 91747),
    }
    for key, (digital_id, marvel_com_id) in verified.items():
        issue = await session.scalar(select(Issue).where(Issue.key == key))
        assert issue.digital_id == digital_id, key
        assert issue.marvel_com_issue_id == marvel_com_id, key
        # Gate C: the Branch sourceId is the marvel.com issue id.
        assert issue.source_id == marvel_com_id, key


async def test_covers_arrive_and_build_a_real_url(session, loaded_event) -> None:
    from marvel.records import cover_url

    await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))
    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    url = cover_url(issue.thumbnail_path, issue.thumbnail_extension)
    assert url and url.startswith("https://i.annihil.us/")
    assert url.endswith("/portrait_incredible.jpg")


async def test_the_snapshot_never_overrides_a_curated_unavailable(
    session, curated_events
) -> None:
    """A curator saying "this genuinely isn't on Marvel Unlimited" outranks a
    snapshot that happens to carry an id."""
    from dataclasses import replace

    from curation.loader import load_event

    event = curated_events[0]
    await load_event(
        session,
        replace(
            event,
            issues=[
                replace(
                    i,
                    availability=Availability.UNAVAILABLE,
                    unavailable_note="print only, checked by hand",
                )
                if i.key == "king-in-black-1"
                else i
                for i in event.ordered
            ],
        ),
    )
    await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert issue.digital_id == 55807, "API-owned fields should still be filled in"
    assert issue.availability == Availability.UNAVAILABLE.value
    assert issue.unavailable_note == "print only, checked by hand"


async def test_the_snapshot_is_cached_as_gate_b_evidence(session, loaded_event) -> None:
    """The raw cache is the evidence store; a snapshot-sourced id must be
    traceable there, and visibly labelled as snapshot-derived rather than as
    something Marvel returned."""
    from marvel.cache import cached_record_index
    from models.cache import MarvelResponseCache

    await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))

    row = await session.scalar(
        select(MarvelResponseCache).where(
            MarvelResponseCache.endpoint == "snapshot:king-in-black"
        )
    )
    assert row is not None
    assert row.endpoint.startswith("snapshot:")
    assert "Marvel" in row.attribution_text

    index = await cached_record_index(session)
    assert index["king-in-black-1"].digital_id == 55807


async def test_a_record_for_an_uncurated_issue_is_ignored(session, loaded_event) -> None:
    """A snapshot must not invent catalog rows with no place in a reading order."""
    before = len((await session.scalars(select(Issue))).all())
    snap = snapshots.load_snapshot("king-in-black")
    snap.payload["data"]["results"].append(
        {
            "id": 999999,
            "digitalId": 70001,
            "title": "Not Curated (2021) #1",
            "issueNumber": "1",
            "series": {"name": "Not Curated (2021)"},
            "dates": [],
            "urls": [],
            "thumbnail": {},
        }
    )
    report = await snapshots.apply(session, snap)
    assert report.records_seen == 41
    assert report.issues_matched == 40
    assert len((await session.scalars(select(Issue))).all()) == before


async def test_unmatched_is_scoped_to_this_event(session, loaded_event) -> None:
    """Guards a real bug: an unscoped query reported every *other* event's
    issues as unmatched, noise that would grow with each event added."""
    session.add(
        Issue(key="unrelated-1", series_name="Unrelated", series_slug="unrelated",
              issue_number=1)
    )
    await session.commit()
    report = await snapshots.apply(session, snapshots.load_snapshot("king-in-black"))
    assert "unrelated-1" not in report.issues_unmatched
    assert report.issues_unmatched == []


def test_curated_ids_would_be_traceable_against_the_snapshot(curated_events) -> None:
    """The Gate B validator, run against the shipped snapshot rather than a
    test fixture — which is what `make load` actually does."""
    from dataclasses import replace

    index = snapshots.combined_record_index()
    event = curated_events[0]
    with_ids = replace(
        event,
        issues=[
            replace(i, digital_id=index[i.key].digital_id) for i in event.ordered
        ],
    )
    assert check_digital_ids_traceable(with_ids, index) == []


def test_a_swapped_id_is_still_caught(curated_events) -> None:
    """The gate has to survive the source change: an id belonging to a different
    issue must fail even though both ids are genuine."""
    from dataclasses import replace

    index = snapshots.combined_record_index()
    event = curated_events[0]
    swapped = replace(
        event,
        issues=[
            replace(i, digital_id=index["venom-34"].digital_id)
            if i.key == "king-in-black-1"
            else i
            for i in event.ordered
        ],
    )
    assert check_digital_ids_traceable(swapped, index)


def test_the_snapshot_file_is_valid_json_with_the_expected_envelope() -> None:
    path = snapshots.SNAPSHOT_DIR / "king-in-black.json"
    payload = json.loads(path.read_text())
    assert payload["data"]["results"]
    assert payload["data"]["count"] == len(payload["data"]["results"])


async def test_apply_all_applies_every_shipped_snapshot(session, loaded_event) -> None:
    """The entry point `make load` and the boot lifespan both call."""
    reports = await snapshots.apply_all(session)
    assert [r.event_slug for r in reports] == snapshots.available()
    assert all(r.issues_matched for r in reports)


async def test_the_orphan_note_appears_when_a_rename_leaves_residue(
    session, curated_events
) -> None:
    """Renaming an issue changes its key and leaves the old row behind — on
    purpose, since a bookmark may still point at it. The curator should be told
    rather than discovering a card no reading order can place."""
    from dataclasses import replace

    from curation.loader import load_all, load_event

    event = curated_events[0]
    await load_event(session, event)

    # Rename one issue's series, which changes its key.
    renamed = replace(
        event,
        issues=[
            replace(i, key="renamed-under-a-new-name-1", series_name="Renamed Under A New Name")
            if i.key == "king-in-black-marauders-1"
            else i
            for i in event.ordered
        ],
    )
    await load_event(session, renamed)

    report = await load_all(session)
    assert report.orphaned_issues >= 1
    summary = report.summary()
    assert "belong to no event" in summary
    assert "bookmark pointing at one" in summary


def test_a_clean_load_report_has_no_orphan_note() -> None:
    from curation.loader import LoadReport

    assert "belong to no event" not in LoadReport(events=1, memberships=40).summary()
