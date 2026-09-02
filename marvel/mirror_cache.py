"""Caching the metadata mirror, so a reader's lookup does not race a shared quota.

The mirror allows 60 requests a minute **per IP**, and on Heroku that address is
shared with other tenants — so the budget is not ours. In production this showed
up as `add_to_shelf` returning no matches on one attempt and the right answer on
the next, for reasons unrelated to the comic being asked about (#28).

Caching is the only lever we have: we cannot raise the limit, and we cannot stop
other tenants spending it.

Written as an httpx transport, matching `marvel.cassette`. That keeps
`MirrorClient` ignorant of the database — where a response comes from stays a
transport concern — and it means the cache composes with replay rather than
competing with it.

Two rules this module must never break:

1. **A cache failure is not a lookup failure.** Every database interaction is
   best-effort. If the cache is unreachable the request goes to the mirror, and
   the reader never learns there was a cache.
2. **The cache never holds a transient.** A 429 is a fact about the moment, not
   about the resource; storing one would replay a rate limit for its whole TTL.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.mirror import MirrorClient
from models.mirror_cache import MirrorResponseCache
from observability import metrics

#: How long each kind of response stays usable. These are freshness choices, not
#: correctness ones — the table is disposable — so they are tuned to how often
#: the underlying thing actually changes.
TTLS: tuple[tuple[str, timedelta], ...] = (
    # A published issue's record does not change. Marvel may correct a credit,
    # but nothing that would make a month-old copy wrong.
    ("issues/", timedelta(days=30)),
    # A series grows while it is publishing and is then fixed forever.
    ("series/", timedelta(days=7)),
    # The volatile one. Short, but still long enough to collapse the case that
    # motivated all this: the same search retried a moment after it failed.
    ("search/", timedelta(hours=6)),
)
DEFAULT_TTL = timedelta(hours=6)

#: Socket timeout for a mirror request. A module constant rather than a
#: parameter: nothing ever passed one, and an async function taking a `timeout`
#: reads as an asyncio deadline the caller could compose with `asyncio.timeout`,
#: which this is not — it configures connect/read behaviour per request.
REQUEST_TIMEOUT_S = 10.0


def ttl_for(path: str) -> timedelta:
    for prefix, ttl in TTLS:
        if path.startswith(prefix):
            return ttl
    return DEFAULT_TTL


def request_key(request: httpx.Request) -> tuple[str, str]:
    """(path, params_key) for one request.

    Sorted so two callers building the same query in a different order share a
    row. No auth triple to strip, unlike the Marvel cache — the mirror takes no
    credentials.
    """
    path = request.url.path.removeprefix("/v1/").strip("/")
    params = sorted(request.url.params.multi_items())
    return path, urlencode(params)


async def load(
    session: AsyncSession, path: str, params_key: str, *, ttl: timedelta | None = None
) -> dict | None:
    """A cached body, if one is present and still fresh."""
    row = await session.scalar(
        select(MirrorResponseCache).where(
            MirrorResponseCache.path == path,
            MirrorResponseCache.params_key == params_key,
        )
    )
    if row is None:
        return None
    fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
    # `is not None`, not `or`: timedelta(0) is falsy, so `ttl or ttl_for(path)`
    # silently ignored an explicit zero and served a 30-day-old body to a caller
    # asking for a guaranteed-fresh one.
    window = ttl if ttl is not None else ttl_for(path)
    if fetched < datetime.now(UTC) - window:
        return None
    return row.body


async def store(session: AsyncSession, path: str, params_key: str, body: dict) -> None:
    """Upsert one response. One current row per (path, params)."""
    row = await session.scalar(
        select(MirrorResponseCache).where(
            MirrorResponseCache.path == path,
            MirrorResponseCache.params_key == params_key,
        )
    )
    if row is None:
        row = MirrorResponseCache(path=path, params_key=params_key, body=body)
        session.add(row)
    row.body = body
    row.fetched_at = datetime.now(UTC)
    await session.commit()


async def purge(session: AsyncSession, older_than: timedelta) -> int:
    """Drop rows past their usefulness. Nothing depends on them existing."""
    cutoff = datetime.now(UTC) - older_than
    rows = (
        await session.scalars(
            select(MirrorResponseCache).where(MirrorResponseCache.fetched_at < cutoff)
        )
    ).all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return len(rows)


def default_sessions():
    """The session factory used when a caller injects none.

    A module-level function rather than an inline import so the test suite can
    point it at its own connection. Without that, every test exercising the MCP
    shelf tool would commit cache rows to the developer's database, outside the
    per-test transaction and surviving the run.
    """
    from db.session import SessionLocal

    return SessionLocal()


class CachingTransport(httpx.AsyncBaseTransport):
    """Serves mirror responses from Postgres when they are fresh enough.

    Opens its **own** session per request rather than borrowing the caller's.
    The caller is mid-transaction with a session of its own, and an
    `AsyncSession` is not safe to use concurrently — reaching into it from
    inside an httpx call is the greenlet bug this design exists to avoid. A
    separate session also means a cache write commits on its own, so a caller
    that later rolls back does not discard a perfectly good cached response.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        session_factory=None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._session_factory = session_factory

    def _sessions(self):
        return (self._session_factory or default_sessions)()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path, params_key = request_key(request)

        cached = await self._try_load(path, params_key)
        if cached is not None:
            metrics.record_mirror_request(path, "cached")
            return httpx.Response(200, json=cached, request=request)

        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()

        metrics.record_mirror_request(path, _outcome(response.status_code))
        if response.status_code == 200:
            await self._try_store(path, params_key, body)

        return httpx.Response(
            status_code=response.status_code,
            # The body is already decoded, so the original Content-Encoding
            # must not ride along — see marvel/cassette.py for the same trap.
            headers=[
                (k, v)
                for k, v in response.headers.multi_items()
                if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            ],
            content=body,
            request=request,
        )

    async def _try_load(self, path: str, params_key: str) -> dict | None:
        try:
            async with self._sessions() as session:
                return await load(session, path, params_key)
        except Exception:  # noqa: BLE001 — a cache miss is always survivable
            return None

    async def _try_store(self, path: str, params_key: str, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        try:
            async with self._sessions() as session:
                await store(session, path, params_key, payload)
        except Exception:  # noqa: BLE001 — failing to cache must not fail the lookup
            return

    async def aclose(self) -> None:
        await self._inner.aclose()


def _outcome(status: int) -> str:
    if status == 429:
        return "rate_limited"
    return "ok" if status < 400 else "error"


@asynccontextmanager
async def cached_mirror():
    """A `MirrorClient` that reads through the cache. The runtime entry point.

    `marvel.mirror` deliberately does not import this module: the client stays
    unaware that a cache exists, exactly as it stays unaware of replay. Callers
    choose a transport; the client just makes requests.
    """
    async with httpx.AsyncClient(transport=CachingTransport(), timeout=REQUEST_TIMEOUT_S) as http:
        yield MirrorClient(client=http)
