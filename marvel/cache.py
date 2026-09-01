"""Raw-response cache (SPEC §3) — disposable, refetchable, never hand-edited.

Beyond sparing Marvel's rate limit, this table is the **evidence store for
Gate B**: a `digital_id` is only legitimate if a real API response for that
same issue contained it. `record_index` builds the lookup the curation tests
and the sync path both check against.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marvel.client import MarvelResponse, params_key
from marvel.records import ComicRecord, parse_comics
from models.cache import MarvelResponseCache
from observability import metrics


async def store(session: AsyncSession, response: MarvelResponse) -> MarvelResponseCache:
    """Upsert one response verbatim. Overwrites in place so the cache holds
    exactly one current body per (endpoint, params)."""
    row = await session.scalar(
        select(MarvelResponseCache).where(
            MarvelResponseCache.endpoint == response.endpoint,
            MarvelResponseCache.params_key == response.params_key,
        )
    )
    if row is None:
        row = MarvelResponseCache(
            endpoint=response.endpoint, params_key=response.params_key, body={}
        )
        session.add(row)
    row.body = response.body
    row.etag = response.etag
    row.attribution_text = response.attribution_text
    row.fetched_at = datetime.now(UTC)
    return row


async def load(
    session: AsyncSession, endpoint: str, params: dict, *, max_age_hours: int | None = None
) -> MarvelResponseCache | None:
    """Read one cached body back. `max_age_hours` is a freshness filter, not a
    correctness one — the cache is disposable by design."""
    row = await session.scalar(
        select(MarvelResponseCache).where(
            MarvelResponseCache.endpoint == endpoint.strip("/"),
            MarvelResponseCache.params_key == params_key(params),
        )
    )
    if row is None:
        metrics.record_cache_lookup("miss")
        return None
    if max_age_hours is None:
        metrics.record_cache_lookup("hit")
        return row
    fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
    if fetched < datetime.now(UTC) - timedelta(hours=max_age_hours):
        metrics.record_cache_lookup("stale")
        return None
    metrics.record_cache_lookup("hit")
    return row


def record_index(payloads: Iterable[dict]) -> dict[str, ComicRecord]:
    """Index every comic in the given response bodies by its issue key.

    Later pages win on collision, which is what you want after a refetch: the
    freshest observation of an issue is the one to trust.
    """
    index: dict[str, ComicRecord] = {}
    for payload in payloads:
        for record in parse_comics(payload):
            index[record.key] = record
    return index


async def cached_record_index(
    session: AsyncSession, *, endpoint_prefix: str = ""
) -> dict[str, ComicRecord]:
    """The Gate B evidence index, built from everything currently cached."""
    stmt = select(MarvelResponseCache).order_by(MarvelResponseCache.fetched_at)
    if endpoint_prefix:
        stmt = stmt.where(MarvelResponseCache.endpoint.startswith(endpoint_prefix))
    rows = (await session.scalars(stmt)).all()
    return record_index(row.body for row in rows)


async def attribution_from_cache(session: AsyncSession) -> str | None:
    """Marvel's own attribution string, as they most recently sent it."""
    return await session.scalar(
        select(MarvelResponseCache.attribution_text)
        .where(MarvelResponseCache.attribution_text != "")
        .order_by(MarvelResponseCache.fetched_at.desc())
        .limit(1)
    )
