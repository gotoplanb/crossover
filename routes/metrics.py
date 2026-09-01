"""Prometheus scrape endpoint.

Watchtower's Prometheus scrapes this. The catalog gauges are refreshed on
scrape rather than on write, because they describe the *state* of the curated
catalog (how much of it is linkable) rather than a stream of events — and that
state changes from a sync or a deploy, neither of which is a request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from opentelemetry.instrumentation.utils import suppress_instrumentation
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models.catalog import Event, EventIssue, Issue
from models.types import Availability
from observability.metrics import set_catalog_gauges

router = APIRouter(tags=["metrics"], include_in_schema=False)


async def refresh_catalog_gauges(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(Event.slug, Issue.availability, func.count())
            .join(EventIssue, EventIssue.event_id == Event.id)
            .join(Issue, Issue.id == EventIssue.issue_id)
            .group_by(Event.slug, Issue.availability)
        )
    ).all()

    per_event: dict[str, dict[str, int]] = {}
    for slug, availability, count in rows:
        per_event.setdefault(slug, {})[availability] = count

    for slug, counts in per_event.items():
        set_catalog_gauges(
            slug,
            linkable=counts.get(Availability.LINKABLE.value, 0),
            unavailable=counts.get(Availability.UNAVAILABLE.value, 0),
            unconfirmed=counts.get(Availability.UNCONFIRMED.value, 0),
        )


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(
    session: Annotated[AsyncSession, Depends(get_session)]
) -> PlainTextResponse:
    # Suppressed, not merely excluded. `excluded_urls` in main.py stops FastAPI
    # from opening a *server* span for the scrape, but the SQLAlchemy
    # instrumentation still traced the gauge refresh — so every 15s scrape left
    # an orphan `SELECT` root span in Tempo, which is exactly the noise the
    # exclusion existed to prevent. Suppressing here covers the whole handler.
    with suppress_instrumentation():
        await refresh_catalog_gauges(session)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
