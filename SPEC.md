# Crossover — Marvel event tie-in guide as an MCP service

A small FastAPI + Postgres service exposing an MCP tool surface that answers, mid-read:
"we just finished this issue — what's next, and which tie-ins expand on it?" Each answer
carries a link that lands the reader in Marvel Unlimited.

Curated deeply for a small number of events (starting with King in Black), best-effort
for everything else. Completeness across the Marvel catalog is an explicit non-goal.

---

## Genesis

This started as a text exchange while my daughter was reading the King in Black omnibus.
She said she was enjoying it and that she likes the omnibus format. I said the only thing
better would be QR codes between chapters linking to the Marvel digital editions of the
side stories — then realized I could just build that.

Her reply is the actual product insight, and it's worth keeping in front of whoever works
on this: the comics themselves already name specific issues when they reference something,
so with a Marvel Unlimited subscription you could pull those up yourself. The omnibus
footnotes are a hand-authored citation graph sitting in print, unlinked. This project
digitizes those edges — which is why `issue_reference` (§4) is curated by hand and cannot
come from any API.

I then talked it through with Claude for about thirty minutes on a walk, which produced
the architecture below. Design decisions came out of that conversation; the reason the
thing exists came from a fourteen-line text thread.

Two consequences for scope:

- **The reader is holding a physical book.** The entry point is a spoken or typed
  reference to where they are ("we just finished issue 3"), not a click in a UI. That is
  why this is an MCP tool surface rather than a website — it has to work from inside a
  conversation, one-handed, on a couch.
- **Bookmarking while reading the omnibus** was the second idea in that thread and is a
  first-class feature, not an add-on. You notice an interesting reference, save it, and
  sequence the saved list later.

QR codes remain a genuinely good idea for a later phase: generate a printable insert of
codes keyed to omnibus chapter breaks, one per reference edge. Out of scope for v1, but
the data model supports it, and it is the original vision in its purest form.

---

## Why this exists

Nothing here is impossible today. She could hit a footnote, put the omnibus down, open
Marvel Unlimited, search the referenced issue, and read it. The service isn't unlocking
new capability — it's removing friction at two specific moments.

**1. Capture without leaving the story.**
The lookup is cheap in isolation and expensive in context. Stopping to search mid-chapter
breaks the thread of a story you're deep inside, and the cost is high enough that most
references just go unfollowed. So the interaction has to be closer to leaving yourself a
sticky note than to running a search: "oh, that's cool, I might want more of that,"
captured in a sentence, from the couch, without putting the book down. The bookmark tool
is that sticky note. Minimizing the interruption *is* the feature — if capturing a
breadcrumb costs as much attention as just looking it up, the project has failed.

**2. Turn a pile of breadcrumbs into a readable arc.**
The second moment comes later, when you've finished the main line and have twenty saved
tie-ins. Crossover tie-ins jump around: some are flashbacks reaching years back, some run
parallel to the core issues, some are epilogues. Read in capture order, or even in strict
publication order, they're disjointed — a list of errands rather than a story.

The useful sequencing is rarely pure chronology. It's clustering: these five all happen
within the same couple of weeks in-universe, these are all X-Men-side books, these are the
Spider-Man ones. Reading a cluster as a batch is how tie-ins add up to the grandness of
the event instead of feeling like scattered footnotes. Straight chronological should be
available, but it's one option, not the default.

That second point is what makes this an LLM tool rather than a sorted list. Grouping by
"these belong together" requires judgment about in-universe timing, character families,
and narrative role — which is exactly what Claude can do over a well-formed bookmark
payload, and what a `ORDER BY publication_date` cannot.

---

## 0. Validation gates — do these before writing application code

Two of the three are already settled. Do not start on the data model until gate C
resolves one way or the other, because it changes what an issue record has to store.

### Gate A — reader URL opens the app — **PASSED**

`https://read.marvel.com/#/book/{digitalId}` opens the Marvel Unlimited web reader.
The page renders a "read in the app" badge which does hand off to the native app on iOS.

**Confirmed tap path from an MCP response on iOS:** tap link → Claude's in-app webview →
tap "open in Safari" → Safari → tap Marvel's badge → Marvel Unlimited app. Two extra taps
beyond the first. Not ideal, but far cheaper than putting the book down and searching
manually, and good enough to prove the idea.

**Output format requirement:** tools must return links as markdown links
(`[label](url)`). Bare URLs wrapped in backticks render as code spans and are *not
tappable* — this was hit during testing and cost a copy-paste. Do not wrap URLs in
backticks or inline code anywhere a user is meant to tap them, and do not render them
inside an HTML card either; plain markdown links are the tested, working format.

**Session state lives in Safari, not the webview.** Logging into Marvel Unlimited inside
Claude's in-app webview buys nothing — the webview is a pass-through you leave immediately.
What matters is that Safari is already authenticated, in which case Marvel's badge reads
"read now" (straight into the app) rather than "learn more" (a subscription pitch). The
badge text is a useful tell for whether the user is signed in.

The Safari hop itself is not fixable and shouldn't be designed around: iOS deliberately
prevents in-app webviews from redirecting out to arbitrary native apps, which is the
correct security behavior. Two taps is the floor for this flow. Accept it.

### Gate B — bogus IDs fail silently — **CONFIRMED, drives a hard requirement**

A made-up `digitalId` did not error. It served a completely different, unrelated comic.

Requirement that follows: **every `digitalId` must originate from a Marvel API response
for that specific issue.** Never derived, inferred, incremented, or guessed. A curated
entry without a confirmed ID renders as "not on Marvel Unlimited" — never as a link.
This is enforced by test, see §7.

### Gate C — is the one-tap Branch link constructable? — **OPEN**

Marvel's app-store-style deep links look like:

```
https://marvel.smart.link/fiir7ec77
  ?type=issue
  &drn=drn:src:marvel:unison::prod:<uuid>
  &sourceId=17721
```

`fiir7ec77` is a Branch link key. The `drn` UUID lives in Marvel's internal "unison"
content namespace and does not appear in public `gateway.marvel.com` responses.
`sourceId` is a third identifier, distinct from both `digitalId` and the marvel.com
issue id.

Two cheap experiments, in order:

1. Change `sourceId` to a different known digital ID, leave `drn` untouched. Does a
   different book open?
2. Remove the `drn` parameter entirely. Does the link still resolve?

If either works, the DRN is decorative and one-tap links are templatable — capture the
template in config, not in code. If both fail, the link is not constructable from public
data.

**Either outcome is shippable.** The reader URL from Gate A is the baseline and is more
durable anyway, since Branch link configs change without notice. If one-tap works, emit
it as a primary link with the reader URL as fallback. Do not block the build on this.

### Also to confirm early

Get a Marvel developer API key from https://developer.marvel.com and verify that
`GET /v1/public/events/{id}/comics` returns usable `digitalId` values for King in Black.
Event tagging quality is known to be good for modern events and poor for early-90s ones.
If digital IDs come back mostly null, the whole linking premise needs rethinking before
anything else gets built.

---

## 1. Reference implementation

**`gotoplanb/conduct`** — same author, same stack, already solving the hard parts.
Lift from it rather than reinventing:

| Concern | File in conduct | Notes |
|---|---|---|
| MCP OAuth authorization server | `oauth_provider.py` | Hand-rolled auth-code grant, mandatory PKCE S256, refresh tokens, all secrets stored as SHA-256 hashes. No external auth library. |
| MCP server + tool registration | `mcp_server.py` | FastMCP over Streamable HTTP, stateless, mounted at `/mcp`. ASGI middleware resolves the bearer token to a principal into a contextvar. |
| Bearer/API-key auth deps | `auth.py` | FastAPI dependency pattern for resolving a token to a principal. |
| Templates + frontend | `templates/base.html` | Tailwind, HTMX 2.x, Alpine 3.x, all off CDN. |
| Migrations | `alembic/` | Standard async SQLAlchemy + Alembic setup. |

**One deliberate divergence:** conduct attributes work to a `ClientApp`. This project
needs a human `User` as the principal, because reading lists are per-person and two
people share one deployment. The OAuth machinery ports nearly as-is; the entity it
resolves to changes.

MCP tools run outside FastAPI's dependency-injection system, so they open their own DB
sessions. Same as conduct. Easy to forget on the first write-path tool.

## 2. Stack

Python 3.12+, FastAPI, async SQLAlchemy 2.x + asyncpg, Alembic, Jinja2, Tailwind/HTMX/
Alpine via CDN, `mcp` (FastMCP). Deployed to the existing Heroku eco dyno + Postgres.

Heroku's filesystem is ephemeral, so SQLite is not an option and there is no on-disk
cache. Everything persistent goes in Postgres.

## 3. Two data layers, kept separate

This separation is the single most important structural decision in the project.

**Raw cache (Postgres, disposable).** Marvel API responses stored verbatim, keyed by
endpoint + normalized params, with a `fetched_at`. Refetchable at any time. Nothing
hand-edited ever lives here.

**Curation (git-tracked YAML, one file per event).** Reading order, ordering corrections,
essential/optional/skippable annotations, and the reference edges. Hand-maintained,
version controlled, diffable — so when Marvel changes their tagging you can see exactly
what moved.

Merged at read time. A refetch must never be able to clobber hand-curation.

## 4. Data model

- `user` — id, email, oauth subject, active flag. Small allowlist.
- `event` — slug, display name, publication window, curation status (curated / best-effort).
- `issue` — series, number, title, publication date, and **separately keyed identifiers**:
  `marvel_com_issue_id`, `digital_id`, `source_id`, `unison_drn`. Populate only what has
  been confirmed. Never assume one converts to another; they are three or four distinct
  ID spaces. Nullable by design.
- `event_issue` — membership plus `position` in the curated order and a `role`
  (core / essential tie-in / optional tie-in).
- `issue_reference` — the graph: `from_issue_id`, `to_issue_id`, `relation_type`, `note`.
  This is the omnibus-footnote layer ("this scene is expanded in X"). Marvel's API does
  not provide it; it is entirely curated. Plain adjacency table; recursive CTE if
  "everything downstream of this issue" is ever needed. No graph database.
- `bookmark` — user_id, issue_id, created_at, optional note. Denormalize enough at write
  time (event, position, publication date, role) that sequencing a reading list needs
  zero follow-up API calls. If ordering bookmarks requires extra roundtrips, this record
  is too thin.

  **Bookmarks are ours, not Marvel's.** Do not attempt to read from or write to a user's
  Marvel Unlimited library or saved items. The public API exposes catalog data only —
  there are no user-account endpoints — so this would require scraping an authenticated
  session and holding Marvel credentials. Rejected on both fragility and privacy grounds.
  A local bookmark also stores things Marvel's model has no slot for: the omnibus page
  that prompted it, why it looked interesting, and which `issue_reference` edge it came
  from.

## 5. Data sources

1. **Marvel API** (`gateway.marvel.com/v1/public`) — auth is md5 of `ts + privateKey +
   publicKey`. `/events/{id}/comics` for the issue roster; `digitalId` and reader URLs
   come from comic records. Attribution text must be displayed per their terms.
2. **Marvel's own reading guides** — e.g. `marvel.com/comics/guides/2075/king-in-black`.
   Better seed for ordering than third-party lists. Scraped, not API.
3. **Hand curation** — reference edges and order fixes. The part that makes this good.

## 6. MCP tool surface

Keep it small. Six tools:

- `list_events()` — what's available, and whether curated or best-effort.
- `get_event_guide(event_slug)` — the full ordered reading guide with links and roles.
- `whats_next(issue_ref)` — given "we just finished X", the next core issue plus tie-ins
  that expand on it. **This is the reason this is an MCP and not a static webpage.**
- `bookmark_issue(issue_ref, note?)` — save something interesting mid-read.
- `sequence_bookmarks()` — return the user's saved tie-ins in a sensible reading order.
  Not a chronological sort. The payload must carry enough for Claude to cluster: in-universe
  timing relative to the core issues, franchise/character family (X-books, Spider-books,
  cosmic), series, narrative role (flashback / parallel / epilogue), and publication date.
  Straight chronological is one option the user can ask for, not the built-in behavior.
  Character and series data comes from the Marvel API; narrative role is curated.

- `add_to_shelf(candidates, source)` — capture issues seen in the wild: photos of a comic
  shop rack, or spoken/typed titles. **The service does no image recognition.** Claude
  reads the photo in-app and passes resolved text; this tool takes strings and resolves
  them against the Marvel API. `source` is `photo` or `typed`, recorded for later review.

  Resolution is best-effort and **confirms in conversation, not later**. A shelf photo can
  yield several issues at once, blurry spines yield guesses, and variant covers are
  distinct issue records sharing one story — so a cover photo can easily match the wrong
  record. The tool is therefore two-phase:

  1. Called with raw candidate text, it resolves against the Marvel API and returns match
     candidates with enough detail to identify them out loud — series, issue number, cover
     date, cover thumbnail. It stores nothing confirmed. Claude then asks the human plainly:
     "I think that's Venom #87 — right?" Standing in the shop holding the book, they can
     correct it in one sentence.
  2. Called again with the chosen match, it commits the shelf entry.

  If the human never answers, the entry stays **pending** with the raw text preserved and
  appears on the web rack for confirmation later. Pending is the fallback path, not the
  primary one. Per Gate B, an unconfirmed candidate never becomes a linkable ID.

`issue_ref` should accept loose natural input ("King in Black #3", the Namor one) and
resolve against the curated set, since it will be spoken aloud mid-conversation.

The shelf is not scoped to an event. Issues found in a comic shop may belong to no curated
event at all, so `bookmark.event_id` and `position` (§4) must be nullable, and the rack
view (§7) needs an ungrouped section for entries with no event context.

Every link in a response is either a confirmed reader URL or an explicit
"not on Marvel Unlimited". No third state.

## 7. Web experience

Not admin scaffolding — the second half of the product. The two surfaces divide by moment,
not by capability:

| | MCP (in conversation) | Web |
|---|---|---|
| When | Mid-read, book in hand | Later, deliberate |
| Job | Capture and sequence | Browse and consume |
| Mode | Spoken, one-handed, fast | Visual, scanning, unhurried |

The realistic usage pattern is a burst of "bookmark that one for later" while reading,
then "resort my reading list," then switching to the web to actually work through it. The
conversation is a bad place to browse twenty covers; a webpage is a bad place to jot a
note without losing your page.

**The reading list view is the centerpiece.** Think comic rack, not todo list:

- Cover thumbnails from the Marvel API (`thumbnail.path` + `.extension`; the
  `portrait_incredible` and `portrait_uncanny` variants are the right sizes). Cover art is
  how anyone actually recognizes an issue.
- Grouped by whatever clustering `sequence_bookmarks` produced — franchise family,
  in-universe timing, arc — with group headers, not a flat sorted list.
- **Provenance on every card.** Why is this here? "Referenced in King in Black #3, p. 14"
  or "tie-in to the Namor thread." A bookmark with no remembered reason is noise a week
  later, and this is where the `issue_reference` graph pays off visibly.
- Read / unread state, tappable through to Marvel Unlimited using the same markdown-link
  rules from Gate A.

Other views: an event guide browser (the full curated order with covers and roles), and a
lightweight curation view for fixing order and adding reference edges by hand — that one
is genuinely just admin, and can stay ugly.

Stack per §2 — Jinja2 + HTMX + Alpine + Tailwind, same as conduct. Server-rendered
partials, no SPA. Marvel's attribution notice must appear wherever their data or art is
displayed.

Same user identity as the MCP principal, so a bookmark made by voice mid-chapter shows up
on the rack without any sync step.

## 8. Tests

Pytest, unit and integration split, mirroring conduct's layout.

Curated-data checks, parameterized over each event YAML:

- Order positions are contiguous, no gaps or duplicates.
- Every core issue in the event is present and ordered.
- **Every `digital_id` traces back to a cached Marvel API record for that same issue.**
  Directly enforces Gate B. This is the test that keeps a kid from tapping the Namor
  tie-in and landing in an unrelated comic.
- Every `issue_reference` points at an issue that exists in the dataset.
- Every issue is either linkable or explicitly flagged unavailable.

Plus the usual: OAuth flow tests (PKCE happy path, replayed code rejected, expired token
rejected), per-user bookmark isolation, and API-client tests against recorded fixtures so
the suite never hits Marvel's servers.

## 9. Build order

1. Gate C experiments + API key check (§0).
2. Marvel API client with on-disk fixtures for tests, plus the raw cache table.
3. King in Black curation YAML, seeded from Marvel's reading guide, hand-corrected.
4. Data-quality test suite — before any serving code, so curation is trustworthy from
   the start.
5. Read-only MCP tools (`list_events`, `get_event_guide`, `whats_next`).
6. OAuth + users, ported from conduct.
7. Bookmarks and `sequence_bookmarks`.
8. Reading list web view — the comic rack (§7). Covers, grouping, provenance.
9. Event guide browser and the curation admin view.
10. `add_to_shelf` plus the pending-confirmation flow on the rack.

Two viable stopping points. After step 5 you have a working reading companion with no
persistence, which is already useful. After step 8 you have the whole idea: capture in
conversation, consume on the rack. Step 9 is convenience.

## 10. Open items

- Gate C outcome (§0) — determines whether one-tap links are in scope.
- Second curated event after King in Black. Infinity War is the likely candidate, but
  confirm the Marvel API actually has usable event tagging and digital IDs for 1991-92
  material before committing to it.
- Whether reference edges get sourced from the printed omnibus footnotes by hand, or
  whether a good-enough approximation exists in an online reading order.
