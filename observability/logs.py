"""OTLP log export to Alloy, which forwards to Loki.

The OTel LoggingHandler stamps each record with the active span's trace_id and
span_id, so a log line in Loki links straight to its trace in Tempo. That link
is the whole reason to route logs through OTel rather than scraping stdout.
"""

from __future__ import annotations

import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

from observability.tracing import SERVICE_NAMESPACE

log = logging.getLogger(__name__)

_initialized = False


def init_logging(
    *,
    service_name: str,
    otlp_endpoint: str,
    role: str = "web",
    level: int = logging.INFO,
    enabled: bool = True,
) -> None:
    """Idempotent. Adds an OTLP handler to the root logger alongside stdout."""
    global _initialized
    if _initialized:
        return
    if not enabled:
        _initialized = True
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            "service.namespace": SERVICE_NAMESPACE,
            "service.instance.role": role,
        }
    )
    provider = LoggerProvider(resource=resource)
    try:
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True))
        )
    except Exception as e:  # pragma: no cover — exporter init rarely fails
        log.warning("OTLP log exporter setup failed (%s) — logs will not export", e)
        return

    set_logger_provider(provider)
    # LoggingHandler emits a DeprecationWarning. Left visible on purpose: the
    # suggested replacement only *injects* trace ids into records, it does not
    # export them, so there is no upgrade path yet. The warning documents that
    # this call site needs to move once the SDK offers somewhere to move to.
    logging.getLogger().addHandler(LoggingHandler(level=level, logger_provider=provider))
    _initialized = True


def reset_for_tests() -> None:
    global _initialized
    _initialized = False
