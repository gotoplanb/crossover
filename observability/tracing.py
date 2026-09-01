"""OpenTelemetry tracer setup. Exports OTLP/gRPC to Watchtower's Alloy.

Alloy listens on :4317 and fans traces out to Tempo. The payoff for this
project specifically: a `whats_next` call spans the loose-reference resolution,
the event lookup, and the reference-graph query, so "why was that slow" and
"why did that resolve to the wrong issue" are answerable after the fact instead
of by re-running it.

Every span attribute here is catalog data or a decision outcome. **No
credential, bearer token, or admin key is ever put on a span** — traces leave
the process and land in a shared Grafana.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)

SERVICE_NAMESPACE = "crossover"

_initialized = False


def init_tracing(
    *, service_name: str, otlp_endpoint: str, role: str = "web", enabled: bool = True
) -> None:
    """Idempotent tracer setup. Call once per process at startup.

    `role` separates the web process from a CLI sync run in Tempo, which
    matters because a sync is the only thing that talks to Marvel.
    """
    global _initialized
    if _initialized:
        return
    if not enabled:
        log.info("OTel tracing disabled (OTEL_ENABLED=false)")
        _initialized = True
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            "service.namespace": SERVICE_NAMESPACE,
            "service.instance.role": role,
        }
    )
    provider = TracerProvider(resource=resource)
    try:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
        )
    except Exception as e:  # pragma: no cover — exporter init rarely fails
        # Telemetry must never be able to stop the app serving.
        log.warning("OTLP exporter setup failed (%s) — traces will not export", e)

    trace.set_tracer_provider(provider)
    _initialized = True
    log.info(
        "OTel tracing initialized: service=%s role=%s endpoint=%s pid=%s",
        service_name, role, otlp_endpoint, os.getpid(),
    )


def reset_for_tests() -> None:
    """Let a test re-run init_tracing. Not used at runtime."""
    global _initialized
    _initialized = False


def get_tracer(name: str = "crossover"):
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes) -> Iterator[trace.Span]:
    """Open a span with attributes, recording exceptions on the way out.

    Used around the operations whose *outcome* is interesting rather than just
    their duration — reference resolution, link building, a Marvel fetch.
    """
    with get_tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
