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

## Deploy your own

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/gotoplanb/crossover)

Provisions a dyno and Postgres, generates a password for the reader you name,
and seeds them as an admin. **What you get immediately:** the King in Black reading guide,
40 issues with covers and working Marvel Unlimited links, and the MCP endpoint
ready for a Claude connector.

Two things worth knowing before you press it:

- **`CROSSOVER_PUBLIC_URL` cannot be known until the app is named**, so the
  template ships an obviously-wrong placeholder. Set it to
  `https://<your-app-name>.herokuapp.com`. It is the OAuth issuer and is checked
  by the MCP transport, so a Claude connector cannot attach while it is wrong —
  and it is correctable after deploy.
- **Links work, but a Marvel Unlimited subscription is what makes them useful.**
  Signed out, a reader URL lands on the issue's marvel.com page instead of
  opening the reader.

## Quick start

```bash
make install                       # venv + deps
make up                            # local Postgres on :5433 (loopback, trust auth)
cp .env.example .env
make migrate
make seed email=you@example.com handle=you admin=1   # create the first admin
make reader-password               # generate an invite code for CROSSOVER_INVITE_CODE
.venv/bin/python -m scripts.cli set-password you       # set your password
make install-hooks                 # pre-commit / pre-push quality gates
make run                           # http://localhost:8020
```

## Who can sign in

Readers register themselves at `/ui/register` with an invite code, and their
password is argon2id-hashed into the database. Each reader's password is what
separates one person's rack from another's — a shared key could not.

**Registration is closed unless `CROSSOVER_INVITE_CODE` is set.** Unset means
closed, not open: the app writes to your database and spends a rate-limited
third-party quota on every shelf lookup, so forgetting to configure the gate
should not be the mistake that opens the door. Rotate the code to stop admitting
people.

Registration never grants admin. Admins reach the curation views and can approve
an OAuth grant; `is_admin` is a flag set deliberately by someone who already has
it (`make seed ... admin=1`), so there is **no master admin key** to leak or
share, and the invite code buys a rack and nothing more.

Nothing here sends email, so there is no password reset. Recovery is
`python -m scripts.cli set-password <handle>`, run by whoever operates the
deployment.

Accounts created before passwords moved into the database accept their old
`CROSSOVER_PASSWORD_{HANDLE}` once, hash it, and never read the environment
again — so nobody has to be told a new password, and those config vars can be
deleted after one sign-in each.

Signing in mints a **revocable, expiring session token**. The cookie carries a
random token, never the reader's database id — see `models/session.py` for why
that distinction matters, and `make revoke-sessions handle=dave` for the remedy
it buys.

Every state-changing request needs a **CSRF token** bound to that session.
Enforced as a router-level dependency rather than per-route, so a form added
later cannot silently skip it; the two exemptions (`/oauth/token`, `/mcp`)
authenticate with credentials a cross-site page cannot supply, and a test keeps
that list from growing quietly. See `csrf.py`.

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
Approving the grant means being signed in as an admin reader.

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
credentials this project actually handles — Marvel keys, reader passwords,
session and OAuth tokens, connection strings with embedded passwords, AWS keys, private key
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
token, a reader password, or the Marvel private key.

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

Heroku eco dyno + Postgres, via the button above or by hand.

`Procfile` runs `alembic upgrade head` on **every** release. `app.json`'s
`postdeploy` runs `crossover bootstrap` **once**, on a button install only — it
seeds the first reader and reports any config that will bite later. A `git push
heroku main` deploy skips postdeploy, so seed manually:

```bash
heroku run python -m scripts.cli seed you@example.com
heroku run python -m scripts.cli bootstrap   # config sanity check, idempotent
```

Config vars: `DATABASE_URL` is injected by the addon and normalized for asyncpg
in `config/settings.py` (Heroku hands out `postgres://`, which the driver cannot
use). Set `CROSSOVER_PUBLIC_URL` (the https origin — it is the OAuth issuer),
`CROSSOVER_INVITE_CODE` (registration stays closed without it), and
`UI_COOKIE_SECURE=true`. Leave
`OTEL_ENABLED=false` unless a dyno can actually reach your collector.

**The filesystem is ephemeral**, which is why curation YAML and the catalog
snapshot are loaded from the repo on every boot, and why the curation admin
view offers a YAML export rather than saving.

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

- No credential of any kind is committed. There is no master admin key at all;
  reader passwords live in config vars; the local Postgres uses trust auth bound
  to `127.0.0.1` so there is no password to commit; `.env` and every `.env.*`
  except the template are gitignored.
- Session cookies hold a random token, stored only as a SHA-256 hash, so a
  database dump contains nothing replayable. Sessions expire after 30 days and
  can be revoked per-reader without deleting anything they own.
