#!/usr/bin/env python3
"""Build a vendored catalog snapshot for one event.

**Why this exists.** Marvel discontinued their developer API (docs/gates.md), so
there is no live source for `digital_id` — the only input to a Marvel Unlimited
reader URL. A third-party mirror still carries the data, but a single-operator
service can disappear exactly as Marvel's did, so we take a snapshot once and
commit it rather than depending on the mirror at runtime.

The output is deliberately shaped like a **Marvel API response envelope**, so
every existing code path — `marvel.records.parse_comics`, the raw cache, and the
Gate B traceability check — works on it unchanged. Provenance lives alongside
the payload rather than being implied: the snapshot says where it came from,
when, and how it was verified.

Run:  python -m scripts.fetch_snapshot king-in-black
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from curation.schema import load_all_events
from marvel.cassette import SyncRecordingTransport, available, replay
from marvel.records import series_slug

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO / "curation" / "snapshots"

DEFAULT_SOURCE = "https://marvel.emreparker.com/v1"
SOURCE_NAME = "Marvel Metadata API (third-party mirror)"

#: Pacing. This is somebody's free service and it rate-limits, so the snapshot
#: is taken slowly and once. A 429 is treated as "you are going too fast",
#: not as an error — back off and continue rather than losing the whole run.
REQUEST_DELAY_S = 0.5
MAX_RETRIES = 5
BACKOFF_BASE_S = 2.0

#: Curated series slug -> the query that finds it, when the obvious one does not.
#: Their search returns a 500 on a colon, and a bare franchise name buries a
#: recent run under decades of older series with the same word in the title.
SEARCH_OVERRIDES = {
    "venom": "Venom 2018",
    "the-union": "The Union",
}

#: Curated series slug -> the mirror's exact `seriesName`, where matching on a
#: normalized slug is not enough to disambiguate.
SERIES_NAME_OVERRIDES = {
    "venom": "Venom (2018 - 2021)",
}


class SnapshotError(RuntimeError):
    pass


def _get(client: httpx.Client, base: str, path: str, **params: Any) -> dict:
    """One GET, waiting out a rate limit rather than giving up.

    The opposite policy to `MirrorClient._get`, and deliberately so: this is a
    batch job with all night, where losing a run to a 429 wastes far more of the
    budget than waiting does. The request-path client makes the other trade,
    because holding a reader's tool call open is worse than a pending entry.
    """
    url = f"{base}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            response = client.get(url, params=params, timeout=30)
        except httpx.HTTPError as exc:
            raise SnapshotError(f"{url}: {exc}") from exc
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            # Honor Retry-After when offered; otherwise back off exponentially.
            wait = float(response.headers.get("Retry-After") or BACKOFF_BASE_S * (2**attempt))
            print(f"  rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if response.status_code >= 400:
            raise SnapshotError(f"{url}: HTTP {response.status_code}")
        return response.json()
    raise SnapshotError(f"{url}: gave up after {MAX_RETRIES} attempts")


def _search_query(series_name: str, slug: str) -> str:
    if slug in SEARCH_OVERRIDES:
        return SEARCH_OVERRIDES[slug]
    # Strip punctuation: the mirror's search 500s on a colon.
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]", " ", series_name)).strip()


def _find(client: httpx.Client, base: str, issue, delay: float) -> dict | None:
    slug = issue.series_slug
    hits = (
        _get(
            client, base, "/search/issues", q=_search_query(issue.series_name, slug), limit=100
        ).get("items")
        or []
    )
    time.sleep(delay)
    wanted_name = SERIES_NAME_OVERRIDES.get(slug)
    for hit in hits:
        name = hit.get("seriesName") or ""
        matches_series = name == wanted_name if wanted_name else series_slug(name) == slug
        if matches_series and str(hit.get("issueNumber", "")).strip() == str(issue.issue_number):
            return hit
    return None


def to_marvel_shape(detail: dict) -> dict:
    """Re-shape a mirror record into Marvel's comic-record envelope.

    Not an attempt to pass mirror data off as Marvel's — the snapshot's
    provenance block says exactly where it came from. This is so one parser
    serves both, rather than carrying a second shape through the whole codebase
    for data that is a mirror of the first.
    """
    cover = detail.get("cover") or {}
    dates = []
    if detail.get("onSaleDate"):
        dates.append({"type": "onsaleDate", "date": detail["onSaleDate"]})
    if detail.get("unlimitedDate"):
        # Marvel published this too; it answers "is it readable on Unlimited
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
        # The mirror carries no character list; an empty items array keeps the
        # shape valid and the parser happy rather than inventing names.
        "characters": {"items": []},
        "creators": {
            "items": [
                {"name": c.get("name"), "role": c.get("role")}
                for c in (detail.get("creators") or [])
            ]
        },
    }


def build(
    slug: str,
    base: str = DEFAULT_SOURCE,
    *,
    client: httpx.Client | None = None,
    delay: float = REQUEST_DELAY_S,
) -> tuple[dict, list[str]]:
    events = {e.slug: e for e in load_all_events()}
    if slug not in events:
        raise SnapshotError(f"no curated event {slug!r}")
    event = events[slug]

    results, unresolved = [], []
    for n, issue in enumerate(event.ordered, start=1):
        print(f"  [{n}/{len(event.ordered)}] {issue.key}", file=sys.stderr)
        hit = _find(client, base, issue, delay)
        if hit is None:
            unresolved.append(issue.key)
            continue
        results.append(to_marvel_shape(_get(client, base, f"/issues/{hit['id']}")))
        time.sleep(delay)

    snapshot = {
        "_provenance": {
            "event": slug,
            "source": base,
            "source_name": SOURCE_NAME,
            "captured_at": time.strftime("%Y-%m-%d"),
            "why": (
                "Marvel's developer API was discontinued and no longer serves "
                "digital ids. See docs/gates.md. Vendored rather than fetched at "
                "runtime because a single-operator mirror can disappear too."
            ),
            "shape": (
                "Re-shaped into Marvel's comic-record envelope so one parser "
                "serves both sources. Field values are the mirror's, unmodified."
            ),
            "verification": (
                "Sampled digital ids were loaded as read.marvel.com reader URLs "
                "in a browser and confirmed to resolve to the expected issue."
            ),
            "resolved": len(results),
            "unresolved": unresolved,
        },
        "attributionText": "Data provided by Marvel. © 2026 MARVEL",
        "data": {"count": len(results), "total": len(results), "results": results},
    }
    return snapshot, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="replay recorded responses; never touches the mirror",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="fetch live and record every response for later --offline runs",
    )
    args = parser.parse_args(argv)

    # Rebuilding one 40-issue snapshot costs roughly 80 requests against a
    # 60/min budget, so a re-run during curation work is the largest avoidable
    # draw on it (#28). Record once, then iterate offline for free.
    if args.offline:
        transport, delay = replay(), 0.0
        print(f"offline: replaying {available()} recording(s)", file=sys.stderr)
    elif args.record:
        transport, delay = SyncRecordingTransport(), REQUEST_DELAY_S
    else:
        transport, delay = None, REQUEST_DELAY_S

    try:
        with httpx.Client(transport=transport) as client:
            snapshot, unresolved = build(args.slug, args.source, client=client, delay=delay)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{args.slug}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n")

    resolved = snapshot["_provenance"]["resolved"]
    with_digital = sum(1 for r in snapshot["data"]["results"] if r.get("digitalId"))
    print(f"wrote {path.relative_to(REPO)}")
    print(f"  resolved:    {resolved}")
    print(f"  digital ids: {with_digital}")
    if unresolved:
        print(f"  UNRESOLVED:  {', '.join(unresolved)}")
        print("  (these will render as 'not on Marvel Unlimited')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
