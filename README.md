# Crossover

A Marvel event tie-in guide as an MCP service. It answers, mid-read:

> we just finished this issue — what's next, and which tie-ins expand on it?

Each answer carries a link that lands the reader in Marvel Unlimited.

Curated deeply for a small number of events (starting with King in Black),
best-effort for everything else. Completeness across the Marvel catalog is an
explicit non-goal.

See `SPEC.md` for why it exists; this file is how to run it.

---

## What's here

| | |
|---|---|
| **MCP tool surface** | Six tools at `/mcp`, OAuth-gated. Mid-read, spoken, one-handed. |
| **Web surface** | The reading-list rack at `/ui/rack`. Later, deliberate, visual. |
| **Curation** | Git-tracked YAML in `curation/events/`. The part no API can provide. |
| **Marvel client** | `gateway.marvel.com` + a disposable raw-response cache. |

The two surfaces divide by *moment*, not capability: capture in conversation,
consume on the rack. Same user identity across both, so a bookmark made by voice
mid-chapter shows up on the rack with no sync step.

## Quick start

```bash
make install                       # venv + deps
make up                            # local Postgres on :5433 (loopback, trust auth)
cp .env.example .env
make admin-key                     # generate CROSSOVER_ADMIN_KEY, paste into .env
make migrate
make seed email=you@example.com    # create a reader
make install-hooks                 # pre-commit / pre-push quality gates
make run                           # http://localhost:8020
```

**`CROSSOVER_ADMIN_KEY` has no default and the app will not start without it.**
That is deliberate: this repo is public, so a default would be a published
credential for the curation views and the OAuth consent screen.

`make test` runs the suite: 448 tests, 100% statement coverage. **The unit tests
need no database** — the curation gates, link rules, record parsing and
clustering are all pure, which is what lets the data-quality suite run before any
serving code is involved. The integration tests create and use a separate
`crossover_test` database (never the development one) and skip cleanly if
Postgres isn't up.

## Connecting Claude

```bash
make connector name="Claude iOS" email=you@example.com \
               redirect=https://claude.ai/api/mcp/auth_callback
```

Prints a `client_id` / `client_secret` pair (the secret once — it's stored
hashed). Point a custom connector at `https://<host>/mcp`; discovery, authorize,
and token endpoints are advertised at `/.well-known/oauth-authorization-server`.
Approving the grant means being signed in with the admin key.

## Getting real links

**Nothing links until a Marvel sync runs.** Out of the box every issue renders
as `not on Marvel Unlimited`, which is honest rather than broken — see Gate B
below.

```bash
# 1. Free key from https://developer.marvel.com, then into .env
make check-api-key                 # verifies auth AND prints digital-id coverage
make list-events q="King in Black" # find the numeric event id
#    put it in curation/events/king-in-black.yaml as marvel_event_id
make sync-event slug=king-in-black # fetch the roster, confirm digital ids
```

`check-api-key` prints the coverage number deliberately. SPEC §0 makes it the
go/no-go: *if digital IDs come back mostly null, the whole linking premise needs
rethinking before anything else gets built.* Event tagging is good for modern
events and poor for early-90s ones.

## The two rules everything else hangs off

**Gate B — a wrong id is worse than no id.** A fabricated `digitalId` doesn't
error; Marvel serves a *different, unrelated comic*. So a `digital_id` only ever
originates from a Marvel API response for that specific issue — never derived,
inferred, incremented, or guessed. Enforced in three places: the curation loader
refuses to load an id it can't trace to a cached response, the tests fail on one,
and an entry without a confirmed id renders as `not on Marvel Unlimited`. There
is no third state in tool output.

**Gate A — links must be tappable.** Tools return markdown links
(`[label](url)`). A URL in backticks renders as a non-tappable code span, which
is the entire delivery mechanism broken. `marvel.links.assert_tappable` is
asserted over real tool output, not hand-picked strings.

Gate C (one-tap Branch links) resolved in the affirmative but ships disabled —
`config/links.yaml`, and `docs/gates.md` for the probe and the caveat.

## Quality gates

`make install-hooks` symlinks two gates into `.git/hooks`. Both can be skipped
once with `--no-verify`; neither should need to be.

**pre-commit** — fast enough that nobody is tempted to bypass it: a secret scan
over the staged diff, a check that `.env` is not staged, ruff, and the unit
suite (no database, well under a second).

**pre-push** — the full bar: ruff over the tree, a secret scan over the tree, the
whole suite with coverage enforced against `fail_under` in `pyproject.toml`, then
a SonarQube scan and its quality gate. Sonar is *skipped with a warning* if
Watchtower isn't running or `SONAR_TOKEN` is unset — a local observability stack
being down should not block a push. Everything else is mandatory.

Run the pieces individually with `make secrets`, `make coverage`, `make lint`,
`make sonar-scan`.

### Secrets

`scripts/check_secrets.py` is a small, dependency-free scanner tuned to the
credentials this project actually handles — Marvel keys, the admin key, OAuth
tokens, connection strings with embedded passwords, AWS keys, private key
blocks. It is deliberately biased toward false positives, since the cost of one
is a comment and the cost of a miss on a public repo is rotating a credential
and rewriting history.

Mark a deliberate placeholder or fixture with `# pragma: allowlist secret`, on
the line or the line above it.

### SonarQube

Scans against Watchtower's local SonarQube (`http://localhost:9000`), project
key `crossover`. `sonar.sources` is an explicit list rather than `.`, because
Sonar refuses to index a file as both source and test — and
`tests/unit/test_sonar_config.py` fails if that list drifts from what is on
disk, so a new module cannot silently escape analysis.

## Observability

Traces and logs are pushed over OTLP to Watchtower's Grafana Alloy; metrics are
scraped from `/metrics`. See **`docs/observability.md`** for the setup, the
non-obvious query labels, and what each metric is for.

The number worth watching is `crossover_digital_id_coverage_ratio` — the
fraction of a curated event roster with a confirmed Marvel Unlimited id. At 0,
nothing is tappable.

Turn it all off with `OTEL_ENABLED=false`. No span attribute ever carries a
token, an admin key, or the Marvel private key.

## Layout

```
mcp_server.py        the six tools + the OAuth bearer gate
observability/       OTel tracing, OTLP logs, Prometheus metrics
oauth_provider.py    authorization-code grant, mandatory PKCE S256 (from conduct)
service/             guide, bookmarks, sequencing, shelf — shared by MCP and web
  sequencing.py      clustering: why this is a tool and not an ORDER BY
curation/            YAML schema, the §8 gates, loader, YAML export
  events/*.yaml      the source of truth for order, roles, and reference edges
marvel/              API client, raw cache, record parsing, link rules
routes/ui.py         the rack, the event browser, the curation admin
scripts/hooks/       the pre-commit and pre-push gates
scripts/check_secrets.py   the scanner both hooks run
```

Two data layers stay strictly apart (SPEC §3), and it is enforced structurally
rather than by discipline: `marvel/sync.py` may write only the columns in
`API_OWNED_COLUMNS`, the curation loader writes only the rest, and
`tests/unit/test_ownership.py` fails if the sets ever overlap. **A refetch cannot
clobber hand-curation.**

## Curating

Edit `curation/events/<slug>.yaml` and deploy — the loader runs on every boot and
is idempotent. `make test` runs the SPEC §8 gates over every event file:
contiguous positions, core issues present and ordered, digital ids traceable to a
Marvel response, reference edges resolving, and every issue either linkable or
explicitly flagged unavailable.

The `references:` block is the hand-authored part, and the reason the project
exists: the omnibus footnotes are a citation graph sitting in print, unlinked.
Marvel's API has nothing like it.

The curation view at `/ui/curate/<slug>` reorders issues and adds edges in the
browser — but **it edits the database, and the database is not the source of
truth.** Heroku's filesystem is ephemeral, so export the YAML from that page and
commit it, or the work is lost on the next deploy.

## Deploying

Heroku eco dyno + Postgres. `Procfile` runs `alembic upgrade head` on release.
Set `DATABASE_URL` (injected), `CROSSOVER_PUBLIC_URL`, `CROSSOVER_ADMIN_KEY`,
`UI_COOKIE_SECURE=true`, and the two Marvel keys.

## Current state

Steps 1–10 of SPEC §9 are built. Known gaps, all recorded in `docs/gates.md`:

- **No Marvel API key was available**, so `/events/{id}/comics` has never been
  called for real. The client is complete and tested against fixtures; the
  fixtures are hand-authored to the documented response shape rather than
  recorded. The open question — whether King in Black has usable digital ids —
  is still open, and `make check-api-key` is how you close it.
- **King in Black's roster is seeded by hand** and marked `provisional`, because
  marvel.com returns 403 to programmatic requests so neither the reading guide
  nor the API could be consulted. The event is `best_effort` until reconciled;
  promoting it to `curated` turns the strict gates on.
- **`omnibus_page` is null throughout.** Those numbers have to be read off the
  physical book, and inventing them would defeat the point of the whole graph.

Marvel's attribution notice appears wherever their data or art is displayed, per
their terms.

## Notes for a public repo

- No credential of any kind is committed. `CROSSOVER_ADMIN_KEY` has no default;
  the local Postgres uses trust auth bound to `127.0.0.1` so there is no password
  to commit; `.env` and every `.env.*` except the template are gitignored.
- `/healthz` reports `admin_key_weak`, and the app logs an error on boot if the
  configured key is an example value or shorter than 16 characters. A weak key is
  otherwise invisible until it is exploited.
