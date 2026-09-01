"""Parsing Marvel comic records into the fields this service stores.

Kept separate from the HTTP client so it can be tested against fixtures with no
network and no DB, and so the Gate B traceability check has one function to
call rather than reimplementing the parse.

Marvel quirks handled here, each of which has bitten someone:

- `digitalId` is **0**, not null, for a comic with no digital edition. Treated
  as absent; a 0 that reached a reader URL would be a Gate B bug.
- The marvel.com issue id is not a field. It only appears inside the `urls`
  array as `.../comics/issue/{id}/{slug}`, so it has to be parsed out.
- `series.name` carries the year range ("King in Black (2020 - 2021)"), which
  is useful for display and terrible for matching, so both forms are kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

_ISSUE_URL_RE = re.compile(r"/comics/issue/(\d+)")

#: Trailing year range on a Marvel series name: "(2020)", "(2020 - 2021)",
#: "(2018 - Present)". Written without adjacent unbounded quantifiers — the
#: input is stripped first and `$` anchors the end — so it runs in linear time.
#: An earlier version used `\s*...\s*$` around an optional group, which is the
#: polynomial-backtracking shape SonarQube's S5852 flags.
_YEAR_SUFFIX_RE = re.compile(
    r"\((\d{4})(?: ?- ?(?:\d{4}|present))?\)$", re.IGNORECASE
)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def series_display_name(series_name: str) -> str:
    """Marvel's series name with the trailing year range stripped.

    Marvel returns "King in Black (2020 - 2021)", which is correct and also
    not what anybody says out loud. Reader-facing text uses this form, so a
    sync can make Marvel canonical without making every title noisier.
    """
    return _YEAR_SUFFIX_RE.sub("", (series_name or "").strip()).strip()


def series_slug(series_name: str) -> str:
    """Normalize a series name to a stable matching key.

    Drops the trailing year range, lowercases, and collapses punctuation, so
    "King in Black (2020 - 2021)" and "King In Black" agree. This is the key
    curation YAML and loose `issue_ref` resolution both hash on.
    """
    bare = series_display_name(series_name)
    return _SLUG_STRIP_RE.sub("-", bare.lower()).strip("-")


def issue_key(series_name: str, issue_number: int | str) -> str:
    """The stable handle used across curation YAML, e.g. "king-in-black-3"."""
    return f"{series_slug(series_name)}-{issue_number}"


def _digital_id(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # 0 means "no digital edition", not "id zero".
    return value or None


def _marvel_com_issue_id(urls: list[dict] | None) -> int | None:
    for entry in urls or []:
        match = _ISSUE_URL_RE.search(entry.get("url") or "")
        if match:
            return int(match.group(1))
    return None


def _on_sale_date(dates: list[dict] | None) -> date | None:
    for entry in dates or []:
        if entry.get("type") != "onsaleDate":
            continue
        raw = entry.get("date") or ""
        try:
            # Marvel sends e.g. "2021-01-06T00:00:00-0500".
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None
    return None


def _names(container: dict | None) -> list[str]:
    return [i["name"] for i in (container or {}).get("items", []) if i.get("name")]


@dataclass(frozen=True)
class ComicRecord:
    """A Marvel comic record, reduced to what we store.

    `marvel_api_comic_id` is yet another id space — the `id` on the comic
    resource itself — and is deliberately not conflated with either
    `marvel_com_issue_id` or `digital_id`.
    """

    marvel_api_comic_id: int
    #: Already normalized for display — the year range is stripped. The raw form
    #: Marvel sent is still in the response cache verbatim if it is ever needed.
    series_name: str
    series_slug: str
    issue_number: int
    title: str
    published_on: date | None
    digital_id: int | None
    marvel_com_issue_id: int | None
    thumbnail_path: str | None
    thumbnail_extension: str | None
    characters: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return issue_key(self.series_name, self.issue_number)

    @property
    def source_id(self) -> int | None:
        """The Branch `sourceId`.

        Gate C found this equal to the marvel.com issue id for every link
        probed. Exposed as its own property rather than as an alias so the day
        that stops being true is a one-line change here, not a hunt.
        """
        return self.marvel_com_issue_id


def parse_comic(raw: dict) -> ComicRecord:
    raw_series_name = (raw.get("series") or {}).get("name") or ""
    series_name = series_display_name(raw_series_name)
    thumbnail = raw.get("thumbnail") or {}
    return ComicRecord(
        marvel_api_comic_id=int(raw.get("id") or 0),
        series_name=series_name,
        series_slug=series_slug(series_name),
        issue_number=int(raw.get("issueNumber") or 0),
        title=raw.get("title") or "",
        published_on=_on_sale_date(raw.get("dates")),
        digital_id=_digital_id(raw.get("digitalId")),
        marvel_com_issue_id=_marvel_com_issue_id(raw.get("urls")),
        thumbnail_path=thumbnail.get("path"),
        thumbnail_extension=thumbnail.get("extension"),
        characters=_names(raw.get("characters")),
        creators=_names(raw.get("creators")),
    )


def parse_comics(payload: dict) -> list[ComicRecord]:
    """Parse a full `/v1/public/...` envelope into records."""
    results = ((payload or {}).get("data") or {}).get("results") or []
    return [parse_comic(r) for r in results]


# --- cover art (SPEC §7) ---

#: Marvel's variant suffixes. These two are the sizes the rack wants.
PORTRAIT_INCREDIBLE = "portrait_incredible"
PORTRAIT_UNCANNY = "portrait_uncanny"

#: Marvel serves this path for anything with no cover on file.
_IMAGE_NOT_AVAILABLE = "image_not_available"


def cover_url(
    path: str | None, extension: str | None, variant: str = PORTRAIT_INCREDIBLE
) -> str | None:
    """Build a cover URL, or None when Marvel has no art.

    Marvel's placeholder path is detected and turned into None so the rack can
    render its own missing-cover treatment rather than their grey box.
    """
    if not path or not extension or _IMAGE_NOT_AVAILABLE in path:
        return None
    # Marvel serves http:// in API responses; this *upgrades* it to https so
    # browsers don't warn about mixed content. The http:// literal below is the
    # string being replaced, not a URL we fetch — SonarQube S5332 flags the
    # literal, but removing it would leave the insecure URL in place.
    secure = path.replace("http://", "https://", 1)
    return f"{secure}/{variant}.{extension}"
