"""The Prometheus scrape endpoint.

Watchtower's Alloy scrapes this and remote-writes it to Prometheus, so the
series names here are a published interface — a rename silently breaks a
dashboard rather than failing anything.
"""

from __future__ import annotations

from routes.metrics import refresh_catalog_gauges


async def test_metrics_endpoint_serves_prometheus_text(client, loaded_event) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "# HELP" in response.text


async def test_the_gate_b_health_gauges_are_published(client, loaded_event) -> None:
    """The most important operational number here: how much of the curated
    catalog can actually be tapped through to Marvel Unlimited."""
    body = (await client.get("/metrics")).text
    assert 'crossover_unlinkable_issues{event="king-in-black",reason="unconfirmed"} 40.0' in body
    assert 'crossover_linkable_issues{event="king-in-black"} 0.0' in body
    assert 'crossover_digital_id_coverage_ratio{event="king-in-black"} 0.0' in body


async def test_gauges_follow_a_sync(session, loaded_event, record_index, client) -> None:
    """After confirming one issue's digital id, coverage must move."""
    from sqlalchemy import select

    from marvel.sync import apply_record, promote_availability
    from models.catalog import Issue

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    promote_availability(issue)
    await session.commit()

    body = (await client.get("/metrics")).text
    assert 'crossover_linkable_issues{event="king-in-black"} 1.0' in body
    assert 'crossover_unlinkable_issues{event="king-in-black",reason="unconfirmed"} 39.0' in body


async def test_refresh_is_safe_with_no_events(session) -> None:
    """A fresh database must not make the scrape 500 — Prometheus would then
    show the app as down for an unrelated reason."""
    await refresh_catalog_gauges(session)


async def test_the_scrape_does_not_emit_trace_spans(
    client, loaded_event, recorded_spans
) -> None:
    """Prometheus scrapes this every 15 seconds.

    `excluded_urls` in main.py stops the FastAPI server span, but the SQLAlchemy
    instrumentation still traced the gauge refresh, leaving an orphan `SELECT`
    root span in Tempo on every scrape — thousands a day, burying the traces
    anyone actually wants. The handler suppresses instrumentation for exactly
    that reason.
    """
    assert (await client.get("/metrics")).status_code == 200
    assert recorded_spans.get_finished_spans() == ()
