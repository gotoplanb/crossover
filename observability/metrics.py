"""Prometheus metrics, scraped from /metrics by Watchtower's Prometheus.

The set is chosen around the questions this project actually has, not around
what is easy to count:

- **`crossover_linkable_issues` / `crossover_unlinkable_issues`** — the Gate B
  health number. This is the single most important operational metric here: it
  says how much of the curated catalog can actually be tapped through to Marvel
  Unlimited. It going *down* after a sync means Marvel dropped digital editions;
  it staying at zero means nobody has run a sync.
- **`crossover_reference_resolutions_total`** — by outcome. How often loose
  spoken input ("the Namor one") resolves, needs a follow-up question, or finds
  nothing. Directly measures whether the capture-without-friction premise in
  SPEC §2 is holding up.
- **`crossover_tool_calls_total`** — which of the six tools get used, and
  whether the read path or the write path dominates.
- **`crossover_marvel_requests_total`** — Marvel is rate-limited and is the one
  external dependency.

No label carries a user id, an email, or a credential; metrics are shared and
long-lived. `user`-scoped counts would also blow up cardinality for two people.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TOOL_CALLS_TOTAL = Counter(
    "crossover_tool_calls_total",
    "MCP tool invocations by tool and outcome",
    ["tool", "outcome"],
)

TOOL_DURATION_SECONDS = Histogram(
    "crossover_tool_duration_seconds",
    "MCP tool latency. Mid-read capture has to feel instant, so the buckets "
    "are tight at the bottom",
    ["tool"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

REFERENCE_RESOLUTIONS_TOTAL = Counter(
    "crossover_reference_resolutions_total",
    "Loose issue_ref resolutions by outcome (resolved / ambiguous / not_found)",
    ["outcome"],
)

BOOKMARKS_TOTAL = Counter(
    "crossover_bookmarks_total",
    "Bookmarks created by origin (mid_read / shelf_photo / shelf_typed) and status",
    ["origin", "status"],
)

MARVEL_REQUESTS_TOTAL = Counter(
    "crossover_marvel_requests_total",
    "Requests to gateway.marvel.com by endpoint family and outcome",
    ["endpoint", "outcome"],
)

MARVEL_CACHE_TOTAL = Counter(
    "crossover_marvel_cache_total",
    "Raw-response cache lookups by result",
    ["result"],
)

MIRROR_REQUESTS_TOTAL = Counter(
    "crossover_mirror_requests_total",
    "Metadata-mirror lookups by path family and outcome, cache hits included",
    ["path", "outcome"],
)

LINKABLE_ISSUES = Gauge(
    "crossover_linkable_issues",
    "Curated issues with a confirmed Marvel Unlimited digital_id, by event",
    ["event"],
)

UNLINKABLE_ISSUES = Gauge(
    "crossover_unlinkable_issues",
    'Curated issues that render as "not on Marvel Unlimited", by event and reason',
    ["event", "reason"],
)

DIGITAL_ID_COVERAGE = Gauge(
    "crossover_digital_id_coverage_ratio",
    "Fraction of a synced event roster carrying a digital id. SPEC §0 treats a "
    "low value as a signal to rethink the linking premise",
    ["event"],
)


def record_tool_call(tool: str, outcome: str, duration_s: float | None = None) -> None:
    TOOL_CALLS_TOTAL.labels(tool=tool, outcome=outcome).inc()
    if duration_s is not None:
        TOOL_DURATION_SECONDS.labels(tool=tool).observe(duration_s)


def record_resolution(outcome: str) -> None:
    REFERENCE_RESOLUTIONS_TOTAL.labels(outcome=outcome).inc()


def record_bookmark(origin: str, status: str) -> None:
    BOOKMARKS_TOTAL.labels(origin=origin, status=status).inc()


def record_marvel_request(endpoint: str, outcome: str) -> None:
    # Collapse the id out of the path so /events/860/comics and /events/238/comics
    # share a series instead of creating one per event.
    family = _endpoint_family(endpoint)
    MARVEL_REQUESTS_TOTAL.labels(endpoint=family, outcome=outcome).inc()


def _endpoint_family(endpoint: str) -> str:
    parts = [("{id}" if p.isdigit() else p) for p in endpoint.strip("/").split("/")]
    return "/".join(parts)


def record_mirror_request(path: str, outcome: str) -> None:
    """Outcomes: `cached`, `ok`, `rate_limited`, `error`.

    These labels answer a question we could not answer before: how often a
    reader's shelf lookup fails for reasons that have nothing to do with their
    comic. The mirror's 60/min budget is shared with whatever else uses the same
    outbound address, so `rate_limited` is not ours to control and `cached` is
    the only lever we have on it.
    """
    MIRROR_REQUESTS_TOTAL.labels(path=_endpoint_family(path), outcome=outcome).inc()


def record_cache_lookup(result: str) -> None:
    MARVEL_CACHE_TOTAL.labels(result=result).inc()


def set_catalog_gauges(event: str, *, linkable: int, unavailable: int, unconfirmed: int) -> None:
    """Publish the Gate B health numbers for one event.

    `unavailable` and `unconfirmed` are kept apart because they mean different
    things: one is a curator saying "this genuinely isn't on Marvel Unlimited",
    the other is "nobody has checked". Collapsing them would hide the second.
    """
    LINKABLE_ISSUES.labels(event=event).set(linkable)
    UNLINKABLE_ISSUES.labels(event=event, reason="unavailable").set(unavailable)
    UNLINKABLE_ISSUES.labels(event=event, reason="unconfirmed").set(unconfirmed)
    total = linkable + unavailable + unconfirmed
    DIGITAL_ID_COVERAGE.labels(event=event).set(linkable / total if total else 0.0)
