# Observability

Crossover reports to a locally-running [Watchtower](https://github.com/gotoplanb/watchtower)
stack. Traces and logs are **pushed** over OTLP to Grafana Alloy; metrics are
**pulled** by Alloy from `/metrics`.

```
crossover ──OTLP/gRPC :4317──▶ Alloy ──▶ Tempo   (traces)
          │                          └──▶ Loki    (logs)
          └──scraped :8020/metrics──▶ Alloy ──▶ Prometheus (metrics)
```

Everything is off with `OTEL_ENABLED=false` (which the test suite sets), and
nothing here can take the app down: a failed exporter logs a warning and the
process carries on serving.

## Setup

Traces and logs need no configuration beyond the endpoint — the defaults in
`.env.example` point at a local Watchtower.

Metrics need a scrape target, because `prometheus_client` gauges are pull-only.
Watchtower keeps app-specific targets in a **gitignored** local override file,
which is where Crossover registers:

```
# watchtower/docker/alloy-config.d/local-scrapes.alloy
prometheus.scrape "crossover" {
  targets         = [{ "__address__" = "host.docker.internal:8020", "job" = "crossover" }]
  metrics_path    = "/metrics"
  scrape_interval = "15s"
  forward_to      = [prometheus.remote_write.local.receiver]
}
```

Port 8020 rather than 8000, which conduct-api holds locally. `make run` uses the
same port; override with `make run port=NNNN` (and update the scrape target).

After editing that file: `docker restart watchtower-alloy`.

## Querying

Note the label names — they are not what you would guess, and guessing wrong
looks identical to "no telemetry is arriving".

```bash
# Traces. resource.service.name, not service.name.
curl -s -G 'http://localhost:3200/api/search' \
  --data-urlencode 'q={ resource.service.name="crossover" }' --data-urlencode 'limit=10' | jq .

# One tool call, end to end.
curl -s -G 'http://localhost:3200/api/search' \
  --data-urlencode 'q={ name="tool.whats_next" }' | jq .

# Logs. Alloy labels them job="<namespace>/<service>", NOT service_name.
curl -s -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={job="crossover/crossover"}' --data-urlencode 'limit=20' | jq .

# Metrics.
curl -s -G 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=crossover_digital_id_coverage_ratio' | jq .
```

Every log record carries `traceid` and `spanid`, so a line in Loki links
straight to its trace in Tempo. That link is the whole reason logs go through
OTLP rather than stdout scraping.

## What is traced

A tool call nests as:

```
POST                       ← FastAPI server span (/mcp)
└── tools/call whats_next  ← emitted by the MCP SDK
    └── tool.whats_next    ← ours: carries mcp.tool and mcp.outcome
        └── SELECT ...     ← SQLAlchemy
```

Two things are deliberately kept out of Tempo:

- **`/healthz` and `/metrics`** — excluded in `main.py`. `/metrics` additionally
  *suppresses* instrumentation inside the handler, because excluding the server
  span still left the SQLAlchemy query tracing as an orphan root span on every
  15-second scrape.
- **Credentials.** No span attribute carries a token, an admin key, or the
  Marvel private key. `marvel.get` records the endpoint and the *auth-stripped*
  parameters only. Traces leave the process and land in a shared Grafana.

## What is measured

The set is chosen around the questions this project has:

| Metric | Why it exists |
|---|---|
| `crossover_digital_id_coverage_ratio` | **The one to put on a dashboard.** The fraction of a curated event roster with a confirmed Marvel Unlimited id. At 0, nothing is tappable — either no sync has run, or Marvel's event tagging is too poor to build on (SPEC §0's go/no-go). |
| `crossover_linkable_issues` / `crossover_unlinkable_issues` | The same picture split by reason. `unavailable` ("checked, isn't there") and `unconfirmed` ("nobody checked") are kept apart, because collapsing them hides the second. |
| `crossover_reference_resolutions_total` | Loose `issue_ref` resolutions by outcome. Measures whether the capture-without-friction premise is holding: a rising `ambiguous` rate means the resolver is asking too often. |
| `crossover_tool_calls_total` | Which of the six tools get used, and whether reads or writes dominate. `outcome` separates `clarify` from `ok` and `error`. |
| `crossover_tool_duration_seconds` | Mid-read capture has to feel instant, so the buckets are tight at the bottom. |
| `crossover_marvel_requests_total` | Marvel is rate-limited and is the only external dependency. Endpoint ids are collapsed (`events/{id}/comics`) so one series doesn't fork per event. |
| `crossover_marvel_cache_total` | Cache hit / miss / stale. |
| `crossover_bookmarks_total` | By origin and status. |

No label carries a user id or an email. Beyond the privacy point, per-user
labels would be pure cardinality for a two-person deployment.
