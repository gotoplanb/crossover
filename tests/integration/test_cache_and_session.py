"""The raw cache's lookup paths, and the FastAPI session dependency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel import cache
from marvel.client import BASE_URL, MarvelClient
from models.cache import MarvelResponseCache
from observability import metrics


def _counter(result: str) -> float:
    return metrics.MARVEL_CACHE_TOTAL.labels(result=result)._value.get()


async def test_a_miss_is_recorded_as_a_miss(session) -> None:
    before = _counter("miss")
    assert await cache.load(session, "events/999/comics", {"limit": 100}) is None
    assert _counter("miss") == before + 1


@respx.mock
async def test_a_hit_with_no_freshness_window(session, event_comics_payload) -> None:
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        response = await MarvelClient("pub", "priv", client=http).event_comics(860)
    await cache.store(session, response)
    await session.commit()

    before = _counter("hit")
    row = await cache.load(session, "events/860/comics", response.params)
    assert row is not None
    assert _counter("hit") == before + 1


@respx.mock
async def test_a_stale_row_is_recorded_separately_from_a_miss(
    session, event_comics_payload
) -> None:
    """"we have it but it's old" and "we never had it" are different facts, and
    a cache whose staleness is invisible looks like one that never fills."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        response = await MarvelClient("pub", "priv", client=http).event_comics(860)
    await cache.store(session, response)
    await session.commit()

    row = await session.scalar(select(MarvelResponseCache))
    row.fetched_at = datetime.now(UTC) - timedelta(days=90)
    await session.commit()

    before = _counter("stale")
    assert await cache.load(session, "events/860/comics", response.params,
                            max_age_hours=1) is None
    assert _counter("stale") == before + 1


@respx.mock
async def test_a_naive_timestamp_is_treated_as_utc(session, event_comics_payload) -> None:
    """Postgres can hand back a naive datetime depending on the column type; a
    naive/aware comparison would raise TypeError inside the freshness check."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        response = await MarvelClient("pub", "priv", client=http).event_comics(860)
    row = await cache.store(session, response)
    row.fetched_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()

    assert await cache.load(
        session, "events/860/comics", response.params, max_age_hours=24
    ) is not None


@respx.mock
async def test_record_index_can_be_scoped_to_an_endpoint_prefix(
    session, event_comics_payload
) -> None:
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        await cache.store(session, await client.event_comics(860))
        await cache.store(session, await client.search_comics(title="Venom"))
    await session.commit()

    everything = await cache.cached_record_index(session)
    events_only = await cache.cached_record_index(session, endpoint_prefix="events/")
    assert "king-in-black-1" in everything
    assert "king-in-black-1" in events_only

    nothing = await cache.cached_record_index(session, endpoint_prefix="series/")
    assert nothing == {}


@respx.mock
async def test_attribution_comes_from_marvels_own_response(
    session, event_comics_payload
) -> None:
    """Their terms tie the required notice to the data, so it is captured rather
    than hardcoded."""
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        await cache.store(
            session, await MarvelClient("pub", "priv", client=http).event_comics(860)
        )
    await session.commit()
    assert await cache.attribution_from_cache(session) == (
        "Data provided by Marvel. © 2026 MARVEL"
    )


async def test_get_session_yields_a_working_session(db_conn) -> None:
    """The FastAPI dependency itself. Overridden in every route test, so without
    this the real implementation is never executed."""
    from db.session import get_session

    generator = get_session()
    produced = await anext(generator)
    assert isinstance(produced, AsyncSession)
    await generator.aclose()
