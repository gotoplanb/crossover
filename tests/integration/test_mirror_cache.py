"""Reading the mirror through Postgres, so a lookup does not race a shared quota.

The failure this prevents was observed in production: two identical
`add_to_shelf` calls seconds apart, the first returning nothing and the second
returning the right comic, because the mirror's 60/min budget is per-IP and a
Heroku dyno shares its outbound address with other tenants (#28).

The two rules that matter most here are the ones that fail quietly if broken: a
cache problem must never become a lookup problem, and a transient must never be
stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.mirror import DEFAULT_BASE_URL, MirrorClient
from marvel.mirror_cache import (
    CachingTransport,
    load,
    purge,
    request_key,
    store,
    ttl_for,
)
from marvel.mirror_cache import default_sessions as _real_default_sessions
from models.mirror_cache import MirrorResponseCache

BODY = {"id": 8164, "digitalId": 1672, "title": "Daredevil (1964) #181"}
URL = f"{DEFAULT_BASE_URL}/issues/8164"


@pytest.fixture
def session_factory(db_conn):
    """Sessions on the test's connection, so the transport's own session sees
    the test's data and rolls back with it."""

    def make() -> AsyncSession:
        return AsyncSession(
            bind=db_conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    return make


# --- keys and freshness ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_days"),
    [("issues/8164", 30), ("series/2002/issues", 7), ("search/issues", 0), ("other", 0)],
)
def test_ttl_matches_how_often_the_thing_actually_changes(path, expected_days) -> None:
    """A published issue's record does not change; a search ranking does."""
    ttl = ttl_for(path)
    assert ttl.days == expected_days


def test_the_key_is_order_independent() -> None:
    """Two callers building the same query differently must share a row."""
    a = httpx.Request("GET", f"{DEFAULT_BASE_URL}/search/issues?q=Venom&limit=100")
    b = httpx.Request("GET", f"{DEFAULT_BASE_URL}/search/issues?limit=100&q=Venom")
    assert request_key(a) == request_key(b)
    assert request_key(a)[0] == "search/issues", "the /v1 prefix is not part of the key"


# --- the point of the whole exercise ----------------------------------------


@respx.mock
async def test_a_repeated_lookup_costs_no_request(session_factory) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    transport = CachingTransport(session_factory=session_factory)

    async with httpx.AsyncClient(transport=transport) as http:
        first, _ = await MirrorClient(client=http).record(8164)
        second, _ = await MirrorClient(client=http).record(8164)

    assert route.call_count == 1, "the second lookup must not reach the mirror"
    assert first.digital_id == second.digital_id == 1672


@respx.mock
async def test_a_stale_entry_is_refetched(session, session_factory) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await store(session, "issues/8164", "", BODY)
    row = await session.scalar(select(MirrorResponseCache))
    row.fetched_at = datetime.now(UTC) - timedelta(days=90)
    await session.commit()

    transport = CachingTransport(session_factory=session_factory)
    async with httpx.AsyncClient(transport=transport) as http:
        assert (await MirrorClient(client=http).record(8164)).record is not None
    assert respx.calls.call_count == 1, "an expired entry must not be served"


@respx.mock
async def test_a_rate_limit_is_never_cached(session, session_factory) -> None:
    """A 429 is a fact about the moment, not the resource. Storing one would
    replay a rate limit for its whole TTL — turning a transient into an outage."""
    respx.get(URL).mock(return_value=httpx.Response(429))
    transport = CachingTransport(session_factory=session_factory)
    async with httpx.AsyncClient(transport=transport) as http:
        assert (await MirrorClient(client=http).record(8164)).record is None
    assert await session.scalar(select(MirrorResponseCache)) is None


@respx.mock
async def test_an_error_response_is_never_cached(session, session_factory) -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient(
        transport=CachingTransport(session_factory=session_factory)
    ) as http:
        await MirrorClient(client=http).record(8164)
    assert await session.scalar(select(MirrorResponseCache)) is None


@respx.mock
async def test_a_non_json_body_is_not_cached(session, session_factory) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    async with httpx.AsyncClient(
        transport=CachingTransport(session_factory=session_factory)
    ) as http:
        await MirrorClient(client=http).record(8164)
    assert await session.scalar(select(MirrorResponseCache)) is None


# --- a cache problem is never a lookup problem -------------------------------


@respx.mock
async def test_a_dead_cache_still_answers_from_the_mirror() -> None:
    """The rule that matters most. If the database is unreachable the reader
    should never find out — the lookup just costs a request."""

    def exploding_factory():
        raise RuntimeError("database is on fire")

    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    async with httpx.AsyncClient(
        transport=CachingTransport(session_factory=exploding_factory)
    ) as http:
        record, _ = await MirrorClient(client=http).record(8164)

    assert route.called
    assert record is not None and record.digital_id == 1672


@respx.mock
async def test_a_failed_write_still_returns_the_response(monkeypatch, session_factory) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr("marvel.mirror_cache.store", boom)
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    async with httpx.AsyncClient(
        transport=CachingTransport(session_factory=session_factory)
    ) as http:
        assert (await MirrorClient(client=http).record(8164)).record.digital_id == 1672


# --- storage -----------------------------------------------------------------


async def test_store_keeps_one_current_row_per_key(session) -> None:
    await store(session, "issues/8164", "", {"v": 1})
    await store(session, "issues/8164", "", {"v": 2})
    rows = (await session.scalars(select(MirrorResponseCache))).all()
    assert len(rows) == 1 and rows[0].body == {"v": 2}


async def test_load_returns_none_when_absent_or_stale(session) -> None:
    assert await load(session, "issues/1", "") is None
    await store(session, "issues/1", "", BODY)
    assert await load(session, "issues/1", "") == BODY
    assert await load(session, "issues/1", "", ttl=timedelta(seconds=0)) is None


async def test_purge_drops_only_what_is_past_it(session) -> None:
    await store(session, "issues/1", "", BODY)
    await store(session, "issues/2", "", BODY)
    row = await session.scalar(
        select(MirrorResponseCache).where(MirrorResponseCache.path == "issues/1")
    )
    row.fetched_at = datetime.now(UTC) - timedelta(days=400)
    await session.commit()

    assert await purge(session, timedelta(days=365)) == 1
    remaining = (await session.scalars(select(MirrorResponseCache))).all()
    assert [r.path for r in remaining] == ["issues/2"]


@respx.mock
async def test_a_json_array_body_is_not_cached(session, session_factory) -> None:
    """The table stores an object per row. A bare array would round-trip as
    something the parser cannot read back."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    async with httpx.AsyncClient(
        transport=CachingTransport(session_factory=session_factory)
    ) as http:
        await MirrorClient(client=http).record(8164)
    assert await session.scalar(select(MirrorResponseCache)) is None


def test_the_default_session_factory_reaches_for_the_app_session(monkeypatch) -> None:
    """With no factory injected it must open a real application session, or
    production would silently run with no cache at all.

    The real function is captured at import time: the autouse fixture rebinds
    the module attribute to the test connection, which is what every other test
    here wants and what this one needs to see past.
    """
    sentinel = object()
    monkeypatch.setattr("db.session.SessionLocal", lambda: sentinel)
    assert _real_default_sessions() is sentinel


@respx.mock
async def test_cached_mirror_is_a_working_client(session_factory, monkeypatch) -> None:
    """The runtime entry point, exercised as callers use it."""
    from marvel import mirror_cache

    monkeypatch.setattr(
        mirror_cache,
        "CachingTransport",
        lambda: CachingTransport(session_factory=session_factory),
    )
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    async with mirror_cache.cached_mirror() as mirror:
        first, _ = await mirror.record(8164)
        second, _ = await mirror.record(8164)

    assert first.digital_id == second.digital_id == 1672
    assert route.call_count == 1, "cached_mirror must read through the cache"
