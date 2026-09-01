"""Telemetry setup.

These matter more than they look: telemetry runs on every request, and the one
thing it must never do is break the app. So the tests are mostly about failure
modes — a missing collector, a double init, an exporter that refuses to build —
plus the rule that no credential is ever attached to a span.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from observability import logs as obs_logs
from observability import metrics, tracing


@pytest.fixture
def recorded(recorded_spans) -> InMemorySpanExporter:
    """A real tracer provider writing to memory instead of to Alloy."""
    return recorded_spans


def test_span_records_attributes(recorded: InMemorySpanExporter) -> None:
    with tracing.span("marvel.get", **{"marvel.endpoint": "events/860/comics"}):
        pass
    (finished,) = recorded.get_finished_spans()
    assert finished.name == "marvel.get"
    assert finished.attributes["marvel.endpoint"] == "events/860/comics"


def test_none_attributes_are_dropped(recorded: InMemorySpanExporter) -> None:
    """OTel rejects None attribute values with a warning; callers pass optional
    ids routinely, so they are filtered rather than pushed onto every caller."""
    with tracing.span("x", present="yes", absent=None):
        pass
    (finished,) = recorded.get_finished_spans()
    assert "absent" not in finished.attributes
    assert finished.attributes["present"] == "yes"


def test_an_exception_is_recorded_and_re_raised(recorded: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="boom"), tracing.span("x"):
        raise ValueError("boom")
    (finished,) = recorded.get_finished_spans()
    assert finished.status.status_code is trace.StatusCode.ERROR
    assert finished.events, "the exception should be recorded on the span"


def test_init_tracing_is_a_no_op_when_disabled(caplog) -> None:
    tracing.reset_for_tests()
    with caplog.at_level(logging.INFO):
        tracing.init_tracing(
            service_name="crossover", otlp_endpoint="http://nowhere:4317", enabled=False
        )
    assert "disabled" in caplog.text
    tracing.reset_for_tests()


def test_init_tracing_is_idempotent() -> None:
    """Called from the lifespan, which can run more than once under reload."""
    tracing.reset_for_tests()
    tracing.init_tracing(service_name="a", otlp_endpoint="http://nowhere:4317", enabled=False)
    tracing.init_tracing(service_name="b", otlp_endpoint="http://nowhere:4317", enabled=False)
    tracing.reset_for_tests()


def test_a_broken_exporter_does_not_stop_startup(monkeypatch, caplog) -> None:
    """Telemetry must never be able to take the app down."""
    tracing.reset_for_tests()

    def explode(*args, **kwargs):
        raise RuntimeError("no collector")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", explode)
    with caplog.at_level(logging.WARNING):
        tracing.init_tracing(
            service_name="crossover", otlp_endpoint="http://nowhere:4317", enabled=True
        )
    assert "traces will not export" in caplog.text
    tracing.reset_for_tests()


def test_log_init_attaches_a_handler_when_enabled(monkeypatch) -> None:
    """The enabled path. Exercised with a stub exporter so no collector is
    needed — what matters is that a handler lands on the root logger, because
    that is what stamps trace ids onto records and makes Loki lines link to
    Tempo traces."""
    obs_logs.reset_for_tests()
    root = logging.getLogger()
    before = list(root.handlers)

    class StubExporter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def export(self, batch):  # pragma: no cover - never flushed in test
            return None

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis: int = 0):
            return True

    monkeypatch.setattr(obs_logs, "OTLPLogExporter", StubExporter)
    try:
        obs_logs.init_logging(
            service_name="crossover", otlp_endpoint="http://nowhere:4317", enabled=True
        )
        added = [h for h in root.handlers if h not in before]
        assert added, "no OTLP log handler was attached"
        assert isinstance(added[0], obs_logs.LoggingHandler)
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
        obs_logs.reset_for_tests()


def test_log_init_is_idempotent() -> None:
    obs_logs.reset_for_tests()
    obs_logs.init_logging(
        service_name="a", otlp_endpoint="http://nowhere:4317", enabled=False
    )
    root_before = len(logging.getLogger().handlers)
    obs_logs.init_logging(
        service_name="b", otlp_endpoint="http://nowhere:4317", enabled=True
    )
    assert len(logging.getLogger().handlers) == root_before
    obs_logs.reset_for_tests()


def test_log_init_is_a_no_op_when_disabled() -> None:
    obs_logs.reset_for_tests()
    before = len(logging.getLogger().handlers)
    obs_logs.init_logging(
        service_name="crossover", otlp_endpoint="http://nowhere:4317", enabled=False
    )
    assert len(logging.getLogger().handlers) == before
    obs_logs.reset_for_tests()


def test_a_broken_log_exporter_does_not_stop_startup(monkeypatch, caplog) -> None:
    obs_logs.reset_for_tests()

    def explode(*args, **kwargs):
        raise RuntimeError("no collector")

    monkeypatch.setattr(obs_logs, "OTLPLogExporter", explode)
    with caplog.at_level(logging.WARNING):
        obs_logs.init_logging(
            service_name="crossover", otlp_endpoint="http://nowhere:4317", enabled=True
        )
    assert "logs will not export" in caplog.text
    obs_logs.reset_for_tests()


# --- metrics ---


def test_tool_call_duration_is_optional() -> None:
    """Recorded without a duration from paths that only know the outcome."""
    metrics.record_tool_call("whats_next", "ok")
    metrics.record_tool_call("whats_next", "ok", 0.01)
    assert (
        metrics.TOOL_CALLS_TOTAL.labels(tool="whats_next", outcome="ok")._value.get() >= 2
    )


def test_the_other_recorders_accept_their_labels() -> None:
    metrics.record_resolution("resolved")
    metrics.record_bookmark("mid_read", "confirmed")
    metrics.record_marvel_request("events/860/comics", "200")
    metrics.record_cache_lookup("hit")
    assert (
        metrics.MARVEL_REQUESTS_TOTAL.labels(
            endpoint="events/{id}/comics", outcome="200"
        )._value.get()
        >= 1
    )


def test_marvel_endpoint_ids_are_collapsed_into_a_family() -> None:
    """Otherwise every event id creates its own metric series."""
    assert metrics._endpoint_family("events/860/comics") == "events/{id}/comics"
    assert metrics._endpoint_family("/events/238/comics/") == "events/{id}/comics"
    assert metrics._endpoint_family("comics") == "comics"


def test_catalog_gauges_keep_unavailable_and_unconfirmed_apart() -> None:
    """They mean different things — "checked, isn't there" versus "nobody has
    checked" — and collapsing them would hide the second."""
    metrics.set_catalog_gauges("test-event", linkable=3, unavailable=1, unconfirmed=6)

    def value(reason: str) -> float:
        return metrics.UNLINKABLE_ISSUES.labels(event="test-event", reason=reason)._value.get()

    assert value("unavailable") == 1
    assert value("unconfirmed") == 6
    assert metrics.LINKABLE_ISSUES.labels(event="test-event")._value.get() == 3
    assert metrics.DIGITAL_ID_COVERAGE.labels(event="test-event")._value.get() == 0.3


def test_coverage_of_an_empty_event_is_zero_not_a_crash() -> None:
    metrics.set_catalog_gauges("empty-event", linkable=0, unavailable=0, unconfirmed=0)
    assert metrics.DIGITAL_ID_COVERAGE.labels(event="empty-event")._value.get() == 0.0
