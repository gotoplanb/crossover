# Validation gates — outcomes

Status of the three gates in SPEC.md §0, plus the Marvel API key precondition.
Everything here is a *finding*, not a design; the design consequences are noted
inline and enforced by tests where possible.

---

## Gate A — reader URL opens the app — PASSED (inherited from SPEC)

`https://read.marvel.com/#/book/{digitalId}` is the baseline link and the only
link this service emits by default.

Enforced in code:

- `marvel/links.py` builds reader URLs and **only** from a confirmed `digital_id`.
- Links are rendered as markdown `[label](url)`. `tests/unit/test_links.py`
  asserts no emitted link is ever wrapped in backticks or embedded in HTML —
  the failure mode that cost a copy-paste during Gate A testing.

## Gate B — bogus IDs fail silently — CONFIRMED (inherited from SPEC)

A fabricated `digitalId` serves an unrelated comic rather than erroring, so a
wrong ID is *worse* than a missing one: it silently sends the reader into the
wrong book.

Enforced in code:

- `digital_id` is only ever populated from a Marvel API response for that
  specific issue (`marvel/records.py` + `marvel/cache.py`).
- Curation YAML may carry a `digital_id`, but `tests/unit/test_curation_data.py`
  refuses any that does not trace back to a cached Marvel record whose identity
  (series + issue number) matches the curated entry.
- An entry with no confirmed ID renders as "not on Marvel Unlimited". There is
  no third state in tool output.

---

## Gate C — is the one-tap Branch link constructable? — RESOLVED: YES, with a caveat

Probed 2026-09-01 against `https://marvel.smart.link/fiir7ec77`.

### Experiment 2 — remove `drn` entirely

```
GET https://marvel.smart.link/fiir7ec77?type=issue&sourceId=17721
→ 302 Found
→ Location: https://www.marvel.com/comics/issue/17721/slug
```

**The link still resolves with `drn` removed.** The DRN is decorative for this
path — Branch does not need it to pick a destination.

### Experiment 1 — change the id

```
GET https://marvel.smart.link/fiir7ec77?type=issue&sourceId=90983
→ 302 Found
→ Location: https://www.marvel.com/comics/issue/90983/slug
```

**A different `sourceId` resolves to a different book**, and the `drn` is absent
in both cases. `sourceId` alone selects the destination.

### The useful finding

The redirect target is `marvel.com/comics/issue/{sourceId}/slug`. That means
**`sourceId` is not a third ID space at all — it is the marvel.com issue id**,
which the public API *does* expose: every comic record carries a `urls` entry of
the form `http://marvel.com/comics/issue/{marvel_com_issue_id}/...`.

So the one-tap link is templatable from public data:

```
https://marvel.smart.link/fiir7ec77?type=issue&sourceId={marvel_com_issue_id}
```

Per the spec, that template lives in **`config/links.yaml`, not in code** —
Branch link configs change without notice, and `fiir7ec77` is someone else's
config key.

### The caveat, and why one-tap ships disabled by default

What was confirmed is the **web fallback** redirect, from a non-iOS client.
Branch chooses app-vs-web from the requesting platform, so this probe does *not*
prove the native Marvel Unlimited app opens the correct issue on iOS. Given
Gate B — a link that silently resolves to the *wrong* book is the exact failure
this project must not ship — that is not good enough to make one-tap the primary
link on the strength of a desktop 302.

Therefore:

- `config/links.yaml` carries the template with `enabled: false`.
- With it disabled (the default), tools emit only the Gate A reader URL.
- With it enabled, tools emit the one-tap link as primary **and** the reader URL
  as a labelled fallback, per SPEC §0 Gate C ("emit it as a primary link with
  the reader URL as fallback").
- Flipping it on is a one-line config change after someone taps one on an iPhone
  with the app installed and confirms the right book opens. `docs/gates.md` is
  the place to record that when it happens.

The reader URL remains the durable baseline either way.

---

## Marvel API discontinued — 2026-09-01

**There is no Marvel developer API any more.** `developer.marvel.com` 301-redirects
every path to `www.marvel.com`, so no key can be registered, and
`gateway.marvel.com` returns `500 InternalServerErrorException` with an empty body
on every endpoint — including with fabricated credentials, where the documented
response is `409 InvalidCredentials`. An existing key would not help; the backend
is gone, not the auth.

### What replaced it

A vendored **catalog snapshot**, captured once from a third-party mirror and
committed to the repo (`curation/snapshots/<slug>.json`, built by
`scripts/fetch_snapshot.py`). Vendored rather than fetched at runtime because a
single-operator mirror can disappear exactly as Marvel's did.

Marvel's image CDN (`i.annihil.us`) **is** still serving cover art, in the same
`portrait_incredible` / `portrait_uncanny` variants, so covers need no
replacement at all.

### Gate B, amended

> A `digital_id` must originate from a Marvel API response for that specific
> issue, **or from a Marvel-derived source whose id has been verified to resolve
> to that same issue**, with the source recorded. Never derived, inferred,
> incremented, or guessed.

The reason is unchanged — a wrong id opens a different comic rather than
failing. What changed is that "came from Marvel's API" is no longer available as
the evidence, so the evidence is now:

1. **Identity match.** The record must be *this* issue by series and number —
   `check_digital_ids_traceable` still enforces this unchanged.
2. **Recorded provenance.** `issue.digital_id_source` names the source of every
   id, e.g. `snapshot:king-in-black`. An id with no recorded source is not a
   verified id.
3. **Sampled human verification.** Reader URLs were loaded in a browser and
   confirmed to open the expected issue:

   | digitalId | opened | marvel.com issue |
   |---|---|---|
   | 55807 | King In Black (2020) #1 | 85649 |
   | 54937 | The Union (2020) #1 | 82509 |
   | 56429 | Venom (2018) #34 | 89766 |
   | 55812 | King In Black: Namor (2020) #1 | 91747 |

   Each resolved to exactly the marvel.com issue id the snapshot predicted, so
   the two identifiers cross-validate.
4. **Independent corroboration.** A separately-assembled King in Black dataset
   agrees on all 30 overlapping `digital_id`s, with zero disagreements.

That is more evidence than a single live API call ever provided.

### Historical note — the original precondition

`GET /v1/public/events/{id}/comics` was never exercised against live Marvel
servers: no key was available before the API was withdrawn.

Consequences, and how they are contained:

- The API client (`marvel/client.py`) is complete, including the
  `md5(ts + privateKey + publicKey)` auth scheme, and is tested against
  recorded-shape fixtures via `respx` so the suite never hits Marvel.
- Fixtures in `tests/fixtures/marvel/` are **hand-authored to the documented
  response shape**, not recorded from a live call. They prove the parsing and
  Gate B traceability *mechanism*; they are not evidence about King in Black's
  real digital IDs. Replace them with real recordings once a key exists:
  `make sync-event slug=king-in-black record=1`.
- No `digital_id` is asserted anywhere in curated data. King in Black therefore
  ships as `curation_status: best_effort` with every issue in the
  `unconfirmed` availability state, which renders as "not on Marvel Unlimited"
  rather than as a guessed link.
- **The open question the spec flags is still open**: whether event tagging
  returns usable `digitalId` values for King in Black. Run
  `make check-api-key` then `make sync-event slug=king-in-black` and read the
  coverage line it prints. If digital IDs come back mostly null, the linking
  premise needs rethinking before more curation effort goes in.

## §5 source 2 — Marvel's reading guides — NOT SCRAPABLE FROM CI

`marvel.com` returns **403** to plain programmatic requests (both
`/comics/guides/2075/king-in-black` and `/comics/issue/...`). The scraper in
`scripts/scrape_guide.py` is written against the page structure but must be run
from an environment with a real browser session; it is not wired into any
automated path. The King in Black roster in `curation/events/king-in-black.yaml`
is consequently seeded by hand and marked `provisional: true` per entry until
reconciled against the guide and the API.
