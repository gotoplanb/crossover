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

## Where the catalog data comes from

**Marvel discontinued their developer API.** `developer.marvel.com` redirects
every path away, and `gateway.marvel.com` returns a 500 with an empty body on
every endpoint — including with fabricated credentials, where the documented
response is a 409. An API key would not help; there is nothing behind the
gateway. Full evidence in `docs/gates.md`.

What replaced it is a **vendored snapshot**: catalog data captured once from a
third-party mirror and committed to the repo.

```
curation/snapshots/king-in-black.json     # 40 issues, ids + covers + dates
scripts/fetch_snapshot.py                 # rebuilds it
```

Vendored rather than fetched at runtime for two reasons. A single-operator
mirror can disappear exactly as Marvel's did, and a reading guide that stops
linking because someone else's side project went down is a bad guide. And the
data is not volatile — an issue's digital id does not change.

It is written in **Marvel's own response envelope**, so `marvel/records.py`,
the raw cache, and the Gate B traceability check all work on it unchanged. The
only thing that differs is the source stamped on each id.

`make load` applies it, and so does every boot. Marvel's image CDN
(`i.annihil.us`) is still serving cover art, so covers needed no replacement at
all.

To rebuild or add an event:

```bash
python -m scripts.fetch_snapshot king-in-black
make load
```

## The two rules everything else hangs off

**Gate B — a wrong id is worse than no id.** A fabricated `digitalId` doesn't
error; Marvel serves a *different, unrelated comic*. So an id must trace to a
record that is demonstrably *this* issue — never derived, inferred, incremented,
or guessed. Enforced in three places: the curation loader refuses an id it can't
trace, the tests fail on one, and an entry without a confirmed id renders as
`not on Marvel Unlimited`. There is no third state in tool output.

The rule was amended when Marvel's API went away, since "must come from a Marvel
API response" became unsatisfiable. It now reads *"or from a Marvel-derived
source whose id has been verified to resolve to that same issue, with the source
recorded"* — `issue.digital_id_source` is the recording half. The reason is
unchanged, and the identity check is unchanged; only the set of acceptable
sources widened. Sampled ids were loaded in a browser and confirmed to open the
expected issue; see `docs/gates.md`.

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

- **Marvel's API is gone**, so the live client in `marvel/client.py` has no
  server to talk to. It is kept, and still tested against fixtures, because the
  snapshot path reuses its parsing and because a future replacement API would
  slot into the same place.
- **King in Black's roster is seeded by hand** and still marked `provisional`,
  because marvel.com returns 403 to programmatic requests so the reading guide
  could not be consulted. All 40 issues now resolve against catalog data and are
  linkable; what remains is reconciling *order and roles* against Marvel's own
  guide before promoting the event to `curated`.
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
