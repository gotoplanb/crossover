"""Live lookups against the third-party Marvel metadata mirror.

Marvel's own API is gone (docs/gates.md), and the curated catalog is served from
a vendored snapshot precisely so nothing at read time depends on a single
-operator service staying up.

This is the deliberate exception. `add_to_shelf` resolves issues **seen in the
wild** — a comic shop rack, a title said aloud — which by definition may belong
to no curated event, so there is nothing to vendor in advance. A live lookup is
the only way that feature works at all.

The dependency is contained:

- Only the shelf path uses it. The reading guide, the rack, and every link stay
  on vendored data.
- Failure degrades rather than breaks: an unresolved candidate stays **pending**
  with its raw text preserved, which is the fallback `add_to_shelf` already had
  for a blurry spine.
- Anything it supplies still obeys Gate B — an id is attached to an issue only
  after a human confirms the match, and the source is recorded.

**The mirror allows 60 requests per minute** (`x-ratelimit-limit`), so the call
budget shapes the design: searching returns *candidates* built from the search
page alone, and the detail call that carries the digital id is made once, for
the single issue a human confirms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

import httpx

from marvel.records import ComicRecord, parse_comic, series_display_name

DEFAULT_BASE_URL = "https://marvel.emreparker.com/v1"

#: Stamped onto `digital_id_source` for anything this client supplies, so a row
#: can always say which source vouched for its id, and so mirror-sourced ids can
#: be told apart from `snapshot:<slug>` ones later (Gate B).
MIRROR_SOURCE = "mirror:marvel.emreparker.com"

#: Their search 500s on a colon, so "King in Black: Namor" must be asked for
#: without punctuation.
_PUNCTUATION = re.compile(r"[^A-Za-z0-9 ]")
_WHITESPACE = re.compile(r"\s+")
#: A year in a spoken title ("Venom 2018 87") names the run, not the issue.
_YEAR = re.compile(r"^(19[3-9]\d|20[0-3]\d)$")


def search_query(text: str) -> str:
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text)).strip()


def normalize_series(name: str) -> str:
    """Fold a series name to something two spellings of it both reach.

    Marvel is inconsistent about the leading article and about hyphens — the
    mirror says "The Amazing Spider-Man (1963 - 1998)" where a reader says
    "Amazing Spider-Man" — and neither difference means a different series.
    """
    folded = _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", name)).strip().lower()
    return folded[4:] if folded.startswith("the ") else folded


class Outcome(StrEnum):
    """Why a lookup returned what it did.

    Exists because "no candidates" was previously indistinguishable from "the
    catalogue was too busy to ask". Both surfaced as an empty list, so
    `add_to_shelf` told someone holding a comic that it did not exist when the
    truth was "ask again in a moment" (#29).
    """

    OK = "ok"
    #: The mirror answered, and the answer is that there is no such thing.
    NOT_FOUND = "not_found"
    #: 60 requests a minute, shared with whatever else uses our address.
    RATE_LIMITED = "rate_limited"
    #: A 5xx, a network error, or a body that was not readable JSON.
    UNAVAILABLE = "unavailable"

    @property
    def is_failure(self) -> bool:
        """True when the absence of results says nothing about the query."""
        return self in {Outcome.RATE_LIMITED, Outcome.UNAVAILABLE}


class Page(NamedTuple):
    items: list[dict[str, Any]]
    outcome: Outcome


class Detail(NamedTuple):
    record: ComicRecord | None
    outcome: Outcome


class Lookup(NamedTuple):
    """Candidates, plus why there might not be any.

    A tuple so callers can unpack it — `found, outcome = await ...` — while
    still reading `.candidates` where that is clearer.
    """

    candidates: list[Candidate]
    outcome: Outcome


@dataclass(frozen=True)
class Candidate:
    """One possible match, built from a search hit alone.

    Deliberately not a `ComicRecord`: it carries no digital id, because getting
    one costs a request per candidate and the id is meaningless until a human
    says which candidate is right. Everything here is what a reader needs to
    recognise their comic — series, number, year.
    """

    issue_id: int
    title: str
    series_name: str
    issue_number: str | None
    on_sale_date: str | None = None


class MirrorClient:
    """Read-only lookups. Every method returns empty rather than raising.

    Resolution is best-effort by design — a shelf capture must survive the
    mirror being down, since the alternative is losing what someone said while
    standing in a shop.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._injected = client
        self._owned: httpx.AsyncClient | None = None
        # Short: this sits in the request path of a tool call made mid-read, and
        # a slow answer is worse than a pending entry the rack can resolve later.
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        """One connection, reused.

        Resolving a title is several sequential calls. Opening a fresh
        connection for each means a TLS handshake for each, and under that churn
        requests began exceeding the timeout and returning None — a lookup that
        was fast in isolation resolved to nothing in sequence. Pooling took the
        same lookup to well under a second.
        """
        if self._injected is not None:
            return self._injected
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=self._timeout)
        return self._owned

    async def aclose(self) -> None:
        """Close only what this instance opened; an injected client belongs to
        the caller."""
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def __aenter__(self) -> MirrorClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, **params: Any) -> tuple[dict | None, Outcome]:
        """A GET that never raises, and never waits out a rate limit.

        Returns *why* alongside the body. It still never raises — the caller
        should not have to handle a mirror outage to answer a reader — but a
        failure and an empty answer are different facts, and collapsing them is
        how the app came to tell people a comic did not exist because the
        catalogue was busy.

        Sleeping for `Retry-After` is right in the snapshot script, which is a
        batch job with all night. Here it would hold a reader's tool call open,
        and a pending shelf entry they can resolve later is the better answer.
        """
        try:
            response = await self._client().get(f"{self._base_url}{path}", params=params)
            if response.status_code == 429:
                return None, Outcome.RATE_LIMITED
            if response.status_code == 404:
                # An answer, not a failure: the mirror looked and there is no
                # such issue or series.
                return None, Outcome.NOT_FOUND
            if response.status_code >= 400:
                return None, Outcome.UNAVAILABLE
            body = response.json()
            # Valid JSON is not necessarily a body we can read. A bare array
            # would reach `record()`, which calls `.get()` on it and raises an
            # AttributeError that nothing here catches — turning a malformed
            # upstream response into a crashed tool call rather than a miss.
            if not isinstance(body, dict):
                return None, Outcome.UNAVAILABLE
            return body, Outcome.OK
        except (httpx.HTTPError, ValueError):
            return None, Outcome.UNAVAILABLE

    async def search(self, text: str, *, limit: int = 100) -> Page:
        """Raw search hits.

        The width is deliberate. Results come back by relevance to the mirror,
        not by issue number, so a long run buries its middle issues: at 40,
        "Fantastic Four 52" did not contain #52 anywhere on the page. `offset`
        is ignored, so this page is all there is — hence `series_issues` below.
        """
        query = search_query(text)
        if not query:
            # Nothing askable, so nothing failed. "???" has no answer, and that
            # is a fact about the query rather than about the mirror.
            return Page([], Outcome.NOT_FOUND)
        body, outcome = await self._get("/search/issues", q=query, limit=limit)
        return Page((body or {}).get("items") or [], outcome)

    async def series_issues(self, series_id: int) -> Page:
        """Every issue in one series.

        The only endpoint on the mirror that genuinely filters. `/issues`
        accepts `titleStartsWith`, `issueNumber`, and `seriesId` and **ignores
        all three**, returning the same unfiltered page whatever you pass.
        Without this, a numbered issue from a long run is unreachable.
        """
        body, outcome = await self._get(f"/series/{series_id}/issues", limit=500)
        return Page((body or {}).get("items") or [], outcome)

    async def record(self, issue_id: int) -> Detail:
        """One full record, including the digital id that makes a link.

        The expensive half of resolution — call it for a confirmed match, not
        for every candidate. Reports the outcome like everything else here, so
        a caller looping over many issues can tell "this one is unknown" from
        "stop, we are out of budget".
        """
        body, outcome = await self._get(f"/issues/{issue_id}")
        if not body or not body.get("id"):
            return Detail(None, outcome)
        return Detail(parse_comic(to_marvel_shape(body)), outcome)

    async def candidates(self, text: str, *, limit: int = 5) -> Lookup:
        """Possible matches for a spoken or photographed title, best first.

        One search request, plus one more when a numbered issue needs the series
        drill-down. No per-candidate detail calls: the digital id is fetched by
        `record()` once a human has confirmed which of these is right.

        A number ranks matches, it never filters them. A number misread off a
        spine is likelier than a wholly wrong series, so a miss still offers the
        series — but it must not push a real match below an unrelated one.
        """
        wanted, year, query = self._parse(text)
        # Search *with* the year when one was given. Mirror titles embed the run
        # ("Venom (2018 - 2021) #31"), so the year is a strong discriminator in
        # the substring match — and dropping it is what buried the Venom 2018
        # run under two dozen unrelated Venom titles.
        hits, outcome = await self.search(f"{query} {year}" if year else query)
        if year and not hits:
            hits, outcome = await self.search(query)
        if not hits:
            return Lookup([], outcome)

        ordered: list[dict[str, Any]] = []
        if wanted is not None:
            ordered, drill_failure = await self._drilldown(hits, query, year, wanted)
            if drill_failure is not None:
                outcome = drill_failure
            exact = [h for h in hits if self._number(h) == wanted]
            rest = [h for h in hits if self._number(h) != wanted]
            hits = exact + rest

        seen = {h["id"] for h in ordered if h.get("id")}
        for hit in hits:
            if hit.get("id") and hit["id"] not in seen:
                ordered.append(hit)
                seen.add(hit["id"])

        return Lookup([self._candidate(h) for h in ordered[:limit] if h.get("id")], outcome)

    async def _drilldown(
        self, hits: list[dict[str, Any]], query: str, year: str | None, wanted: str
    ) -> tuple[list[dict[str, Any]], Outcome | None]:
        """Reach a numbered issue through the series endpoint.

        This is what gets past the search page, which returns one 100-result
        page ranked by the mirror's own relevance and ignores `offset` — so the
        middle of a long run is not on it at all.

        Returns the matching issues, plus a failure outcome when the drill-down
        itself could not be done. That second value matters: the search
        succeeding while this fails means a *partial* answer, and the search
        page's near misses must not be presented as the whole of it.
        """
        for series_id in self._matching_series(hits, query, year):
            series_page, outcome = await self.series_issues(series_id)
            if outcome.is_failure:
                return [], outcome
            match = [i for i in series_page if self._number(i) == wanted and i.get("id")]
            if match:
                return match, None
        return [], None

    @staticmethod
    def _parse(text: str) -> tuple[str | None, str | None, str]:
        """Split a spoken title into (issue number, year, series query)."""
        parts = text.replace("#", " ").split()
        wanted: str | None = None
        year: str | None = None
        if parts and parts[-1].isdigit() and not _YEAR.match(parts[-1]):
            wanted = str(int(parts[-1]))
            parts = parts[:-1]
        # A trailing year names the run. Readers add one exactly when a title is
        # ambiguous ("Venom 2018 87"), which is when it helps most.
        if parts and _YEAR.match(parts[-1]):
            year = parts[-1]
            parts = parts[:-1]
        return wanted, year, " ".join(parts) or text

    @staticmethod
    def _candidate(hit: dict[str, Any]) -> Candidate:
        return Candidate(
            issue_id=hit["id"],
            title=hit.get("title") or "",
            series_name=hit.get("seriesName") or "",
            issue_number=(str(hit["issueNumber"]) if hit.get("issueNumber") is not None else None),
            on_sale_date=hit.get("onSaleDate"),
        )

    @staticmethod
    def _number(hit: dict[str, Any]) -> str:
        """An issue number as a comparable string. "007" and "7" are one issue."""
        return str(hit.get("issueNumber") or "").strip().lstrip("0") or "0"

    @staticmethod
    def _matching_series(
        hits: list[dict[str, Any]], query: str, year: str | None = None
    ) -> list[int]:
        """Series ids whose name matches the query, best first.

        Ranked: an exact name match in the requested run, an exact match in any
        run, then a prefix match — so "Venom" reaches the Venom run before
        "Venom: Lethal Protector".
        """
        wanted = normalize_series(query)
        if not wanted:
            return []
        exact_year: list[int] = []
        exact: list[int] = []
        prefix: list[int] = []
        seen: set[int] = set()
        for hit in hits:
            series_id = hit.get("seriesId")
            if not series_id or series_id in seen:
                continue
            seen.add(series_id)
            raw = hit.get("seriesName") or ""
            name = normalize_series(series_display_name(raw))
            if name == wanted:
                (exact_year if year and year in raw else exact).append(series_id)
            elif name.startswith(wanted):
                prefix.append(series_id)
        return exact_year + exact + prefix


def to_marvel_shape(detail: dict[str, Any]) -> dict[str, Any]:
    """Re-shape a mirror record into Marvel's comic-record envelope.

    Not an attempt to pass mirror data off as Marvel's — every caller records
    the provenance. This exists so one parser (`marvel.records`) serves both
    sources, rather than a second shape being threaded through the codebase for
    data that is a mirror of the first.
    """
    cover = detail.get("cover") or {}
    dates = []
    if detail.get("onSaleDate"):
        dates.append({"type": "onsaleDate", "date": detail["onSaleDate"]})
    if detail.get("unlimitedDate"):
        # Marvel published this too. It answers "is this readable on Unlimited
        # yet", which lags print by roughly three months.
        dates.append({"type": "unlimitedDate", "date": detail["unlimitedDate"]})
    return {
        "id": detail.get("id"),
        "digitalId": detail.get("digitalId") or 0,
        "title": detail.get("title") or "",
        "issueNumber": detail.get("issueNumber"),
        "description": detail.get("description") or "",
        "pageCount": detail.get("pageCount"),
        "modified": detail.get("modified"),
        "series": {"name": detail.get("seriesName") or ""},
        "dates": dates,
        "urls": (
            [{"type": "detail", "url": detail["detailUrl"]}] if detail.get("detailUrl") else []
        ),
        "thumbnail": {"path": cover.get("path"), "extension": cover.get("extension")},
        # The mirror carries no character list. An empty array keeps the shape
        # valid rather than inventing names.
        "characters": {"items": []},
        "creators": {
            "items": [
                {"name": c.get("name"), "role": c.get("role")}
                for c in (detail.get("creators") or [])
            ]
        },
    }
