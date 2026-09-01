"""FastAPI lifespan: curation load, then the MCP session manager."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

from config.settings import get_settings
from observability.logs import init_logging
from observability.tracing import init_tracing

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Telemetry first, so everything below is traced. Exports OTLP/gRPC to
    # Watchtower's Alloy, which fans out to Tempo and Loki.
    init_tracing(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_endpoint,
        role="web",
        enabled=settings.otel_enabled,
    )
    init_logging(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_endpoint,
        role="web",
        enabled=settings.otel_enabled,
    )
    if settings.otel_enabled:
        # FastAPI instrumentation lives in main.py at import time — Starlette
        # freezes the middleware stack before this body runs, so attaching it
        # here would leave OTel out of the request path entirely and emit zero
        # server spans. httpx and SQLAlchemy patching is library-level, so it
        # belongs here, after init_tracing.
        HTTPXClientInstrumentor().instrument()
        from db.session import engine

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


    # Curation YAML is the source of truth and the filesystem is ephemeral, so
    # the load runs on every boot: "edit the YAML, deploy, done" is the whole
    # curation workflow. Idempotent, and it never touches API-owned columns.
    try:
        from curation.loader import load_all, load_lock
        from db.session import SessionLocal, engine

        # One span around the whole load. Without a parent, each of the ~80
        # statements the loader runs becomes its own root trace in Tempo — the
        # boot alone floods a search that is supposed to surface requests. As one
        # span it also answers "how long does the boot load take", which is the
        # question worth asking about it.
        from marvel import snapshot as snapshots
        from observability.tracing import span

        # `load_lock` serialises across processes: Heroku overlaps the old and
        # new dyno on a deploy, so two lifespans can reach the load at once.
        with span("curation.load_all") as loading:
            async with load_lock(engine), SessionLocal() as session:
                report = await load_all(
                    session, record_index=snapshots.combined_record_index() or None
                )
                # Vendored catalog data — ids, covers, dates. Applied on every
                # boot for the same reason curation is: the filesystem is
                # ephemeral, so the repo is the source and the database is
                # rebuilt from it.
                applied = await snapshots.apply_all(session)
            loading.set_attribute("curation.events", report.events)
            loading.set_attribute("curation.issues", report.issues_created + report.issues_updated)
            loading.set_attribute("curation.references", report.references)
            loading.set_attribute("curation.snapshots", len(applied))
        log.warning("curation: %s", report.summary())
        for one in applied:
            log.warning(
                "snapshot %s: %s issues matched, %s digital ids",
                one.event_slug, one.issues_matched, one.digital_ids_confirmed,
            )
    except Exception:  # noqa: BLE001 — a curation problem must not block boot
        # Serving a stale-but-working guide beats refusing to start. The
        # data-quality suite is the place this is supposed to be caught.
        log.exception("curation load failed; serving whatever is already in the DB")

    if not settings.has_marvel_credentials:
        log.warning(
            "MARVEL_PUBLIC_KEY / MARVEL_PRIVATE_KEY not set — no syncing, and every "
            "issue will render as 'not on Marvel Unlimited'. Get a key at "
            "https://developer.marvel.com"
        )

    # The /mcp app is mounted in main.py, but its session manager only runs
    # while this context is open, so it lives for the lifetime of the app.
    # build_mcp_app() is memoized, so this is the manager main.py's mount is
    # actually serving — in mcp 2.x the manager doesn't exist until the app is
    # built, so asking for it first would raise.
    from mcp_server import build_mcp_app, mcp

    build_mcp_app()
    async with mcp.session_manager.run():
        yield

    if settings.otel_enabled:
        HTTPXClientInstrumentor().uninstrument()
