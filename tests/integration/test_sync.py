"""The refetch path: Marvel API → raw cache → API-owned columns.

This is how a `digital_id` legitimately comes into existence, so it is also
where Gate B is either upheld or broken. Driven through respx, so the suite
never touches Marvel's servers.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import func, select

from marvel import cache
from marvel.client import BASE_URL, MarvelClient
from marvel.sync import sync_event
from models.cache import MarvelResponseCache
from models.catalog import Event, Issue
from models.types import Availability


@pytest.fixture
async def kib(session, loaded_event):
    """King in Black with a Marvel event id, which the YAML deliberately omits."""
    event = await session.scalar(select(Event).where(Event.slug == "king-in-black"))
    event.marvel_event_id = 860
    await session.commit()
    return event


@respx.mock
async def test_sync_caches_responses_verbatim(session, kib, event_comics_payload) -> None:
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload, headers={"etag": "e1"})
    )
    async with httpx.AsyncClient() as http:
        await sync_event(session, MarvelClient("pub", "priv", client=http), "king-in-black")

    row = await session.scalar(
        select(MarvelResponseCache).where(MarvelResponseCache.endpoint == "events/860/comics")
    )
    assert row is not None
    assert row.body == event_comics_payload
    assert row.etag == "e1"
    # Attribution captured from the response, not hardcoded — their terms tie
    # the required notice to the data.
    assert row.attribution_text == "Data provided by Marvel. © 2026 MARVEL"
    # The auth triple must not be in the cache key or it would never hit.
    assert "hash=" not in row.params_key


@respx.mock
async def test_sync_populates_digital_ids_and_promotes_availability(
    session, kib, event_comics_payload
) -> None:
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        report = await sync_event(
            session, MarvelClient("pub", "priv", client=http), "king-in-black"
        )

    first = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert first.digital_id == 55901
    assert first.marvel_com_issue_id == 86133
    assert first.source_id == 86133
    assert first.availability == Availability.LINKABLE.value
    assert first.characters == ["Venom", "Knull"]
    assert first.thumbnail_path

    # digitalId 0 means no digital edition — it must not become a link.
    second = await session.scalar(select(Issue).where(Issue.key == "king-in-black-2"))
    assert second.digital_id is None
    assert second.availability == Availability.UNCONFIRMED.value

    assert report.issues_matched == 3
    assert report.digital_ids_confirmed == 2
    assert report.newly_linkable == 2


@respx.mock
async def test_the_report_names_curated_issues_marvel_never_tagged(
    session, kib, event_comics_payload
) -> None:
    """Event tagging quality varies. A curated issue Marvel doesn't tag is a
    fact worth seeing, not an error — and definitely not an excuse to invent
    an id for it."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        report = await sync_event(
            session, MarvelClient("pub", "priv", client=http), "king-in-black"
        )
    assert "venom-31" in report.issues_unmatched
    assert len(report.issues_unmatched) == 37


@respx.mock
async def test_the_summary_warns_on_poor_digital_id_coverage(
    session, kib, event_comics_payload
) -> None:
    """SPEC §0: if digital ids come back mostly null, the linking premise needs
    rethinking before more curation effort goes in."""
    thin = {
        **event_comics_payload,
        "data": {
            **event_comics_payload["data"],
            "results": [
                {**r, "digitalId": 0} for r in event_comics_payload["data"]["results"]
            ],
        },
    }
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=thin)
    )
    async with httpx.AsyncClient() as http:
        report = await sync_event(
            session, MarvelClient("pub", "priv", client=http), "king-in-black"
        )
    assert report.digital_id_coverage == 0.0
    assert "rethink the linking premise" in report.summary()


@respx.mock
async def test_resyncing_overwrites_in_place(session, kib, event_comics_payload) -> None:
    """The cache holds one current body per (endpoint, params), not a log."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        await sync_event(session, client, "king-in-black")
        await sync_event(session, client, "king-in-black")

    rows = await session.scalar(
        select(func.count()).select_from(MarvelResponseCache).where(
            MarvelResponseCache.endpoint == "events/860/comics"
        )
    )
    assert rows == 1


async def test_syncing_an_event_with_no_marvel_id_says_what_to_fix(
    session, loaded_event
) -> None:
    """A wrong or missing event id would sync an entirely wrong roster."""
    with pytest.raises(ValueError, match="curation YAML"):
        await sync_event(session, MarvelClient("pub", "priv"), "king-in-black")


async def test_syncing_an_unknown_event(session) -> None:
    with pytest.raises(ValueError, match="no event with slug"):
        await sync_event(session, MarvelClient("pub", "priv"), "secret-wars")


@respx.mock
async def test_the_cache_becomes_the_gate_b_evidence_index(
    session, kib, event_comics_payload
) -> None:
    """After a sync, curation can legitimately carry the ids the cache proves."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        await sync_event(session, MarvelClient("pub", "priv", client=http), "king-in-black")

    index = await cache.cached_record_index(session)
    assert index["king-in-black-1"].digital_id == 55901

    from dataclasses import replace

    from curation.schema import load_all_events
    from curation.validate import check_digital_ids_traceable

    event = load_all_events()[0]
    verified = replace(
        event,
        issues=[
            replace(i, digital_id=55901) if i.key == "king-in-black-1" else i
            for i in event.ordered
        ],
    )
    assert check_digital_ids_traceable(verified, index) == []


@respx.mock
async def test_cache_load_honors_a_freshness_window(
    session, kib, event_comics_payload
) -> None:
    from datetime import UTC, datetime, timedelta

    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        await sync_event(session, MarvelClient("pub", "priv", client=http), "king-in-black")

    params = {"limit": 100, "offset": 0, "orderBy": "onsaleDate"}
    assert await cache.load(session, "events/860/comics", params) is not None

    row = await session.scalar(select(MarvelResponseCache))
    row.fetched_at = datetime.now(UTC) - timedelta(days=30)
    await session.commit()
    assert await cache.load(session, "events/860/comics", params, max_age_hours=24) is None
    # The row is still there — freshness is a politeness knob, not correctness.
    assert await cache.load(session, "events/860/comics", params) is not None


async def test_attribution_from_cache_when_empty(session) -> None:
    assert await cache.attribution_from_cache(session) is None


@respx.mock
async def test_marvel_records_for_uncurated_issues_are_skipped(
    session, kib, event_comics_payload
) -> None:
    """Marvel's roster contains issues nobody has curated. They are counted in
    `records_seen` but must not create catalog rows: an issue with no curated
    position has no place in a reading order, and inventing one would put
    unordered entries in front of a reader."""
    from models.catalog import Issue

    extra = {
        **event_comics_payload,
        "data": {
            **event_comics_payload["data"],
            "total": 4,
            "results": [
                *event_comics_payload["data"]["results"],
                {
                    "id": 99999,
                    "digitalId": 70001,
                    "title": "Nobody Curated This (2021) #1",
                    "issueNumber": 1,
                    "series": {"name": "Nobody Curated This (2021)"},
                    "dates": [],
                    "urls": [],
                    "thumbnail": {},
                },
            ],
        },
    }
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=extra)
    )
    before = len((await session.scalars(select(Issue))).all())
    async with httpx.AsyncClient() as http:
        report = await sync_event(
            session, MarvelClient("pub", "priv", client=http), "king-in-black"
        )

    assert report.records_seen == 4
    assert report.issues_matched == 3
    after = len((await session.scalars(select(Issue))).all())
    assert after == before, "an uncurated Marvel record created a catalog row"
    assert (
        await session.scalar(select(Issue).where(Issue.key == "nobody-curated-this-1"))
        is None
    )
