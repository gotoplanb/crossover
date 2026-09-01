"""Clustering saved tie-ins into something that reads like a story.

SPEC §2, and the reason `sequence_bookmarks` is a tool rather than an
`ORDER BY publication_date`: read in capture order — or even in strict
publication order — a pile of tie-ins is a list of errands. What makes them add
up to the grandness of an event is reading them in *batches*: these five happen
in the same stretch of in-universe time, these are the Spider-books, these are
the X-books.

So the built-in behavior is clustering, and chronological is one option the
reader can ask for. This module is pure and DB-free so the ordering logic can
be tested directly, and so it never grows a query.

**What this returns is a well-formed payload, not a final answer.** The last
mile of judgment — "these two clusters really belong together for this reader" —
is Claude's job, which is why every clustering axis stays on every item rather
than being collapsed away into a sort key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from models.types import NarrativeRole

#: In-universe reading order of the narrative roles. This is the timing axis:
#: a flashback belongs before the parallel material regardless of when it
#: published, and an epilogue belongs after it.
_ROLE_ORDER = {
    NarrativeRole.PRELUDE.value: 0,
    NarrativeRole.FLASHBACK.value: 1,
    NarrativeRole.CORE_LINE.value: 2,
    NarrativeRole.PARALLEL.value: 3,
    NarrativeRole.EPILOGUE.value: 4,
}

_ROLE_HEADERS = {
    NarrativeRole.PRELUDE.value: "Before it starts",
    NarrativeRole.FLASHBACK.value: "The flashbacks",
    NarrativeRole.CORE_LINE.value: "The core line",
    NarrativeRole.PARALLEL.value: "Alongside the main story",
    NarrativeRole.EPILOGUE.value: "After the dust settles",
}

_FRANCHISE_LABELS = {
    "symbiote": "Symbiote books",
    "spider": "Spider-books",
    "x_men": "X-books",
    "cosmic": "Cosmic",
    "avengers": "Avengers side",
    "street": "Street level",
    "mystic": "Magic and myth",
    "atlantean": "Atlantis",
    "wakandan": "Wakanda",
    "other": "Everything else",
}


@dataclass
class SeqItem:
    """One saved item, carrying every axis the clustering can use.

    All of this is denormalized onto the bookmark at write time (SPEC §4), so
    building this list costs one query and zero Marvel calls.
    """

    bookmark_id: str
    key: str
    display: str
    series_name: str
    issue_number: int | None
    event_slug: str
    event_name: str
    position: int | None
    role: str
    narrative_role: str
    franchise: str
    published_on: date | None
    note: str
    provenance: str
    link: str
    status: str
    origin: str
    read: bool = False
    #: Nearest preceding core issue in the event order — "this happens around
    #: the time of King in Black #3". The in-universe timing anchor, derived
    #: from curated order rather than from a date.
    anchor: str = ""
    characters: list[str] = field(default_factory=list)
    raw_text: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "bookmark_id": self.bookmark_id,
            "issue": self.display,
            "series": self.series_name,
            "event": self.event_name or None,
            "narrative_role": self.narrative_role or None,
            "franchise": self.franchise or None,
            "in_universe_anchor": self.anchor or None,
            "curated_position": self.position,
            "published_on": self.published_on.isoformat() if self.published_on else None,
            "characters": self.characters or None,
            "link": self.link,
            "read": self.read,
        }
        if self.note:
            payload["your_note"] = self.note
        if self.provenance:
            payload["why_you_saved_it"] = self.provenance
        if self.status != "confirmed":
            payload["status"] = self.status
            payload["raw_text"] = self.raw_text
        return {k: v for k, v in payload.items() if v is not None}


@dataclass
class Cluster:
    """A batch meant to be read together, with a header a human would say."""

    header: str
    reason: str
    items: list[SeqItem]

    def to_payload(self) -> dict[str, Any]:
        return {
            "group": self.header,
            "why_grouped": self.reason,
            "issues": [i.to_payload() for i in self.items],
        }


def _sort_key(item: SeqItem) -> tuple:
    """Within a cluster: curated order first, then publication date.

    Curated position beats the date because the position is where a human
    already made the ordering call; the date is only the tiebreak for items
    with no curated slot.
    """
    return (
        item.position if item.position is not None else 10**6,
        item.published_on or date.max,
        item.series_name,
        item.issue_number or 0,
    )


def _franchise_label(franchise: str) -> str:
    return _FRANCHISE_LABELS.get(franchise, franchise.replace("_", " ").title())


def build_clusters(items: list[SeqItem]) -> list[Cluster]:
    """Cluster saved items into batches worth reading together.

    Partitioned first on in-universe timing (narrative role), then on character
    family within the bulk of the parallel material — which is where a flat list
    hurts most, because the parallel tie-ins are the many and they jump between
    unrelated corners of the universe.
    """
    clusters: list[Cluster] = []

    pending = [i for i in items if i.status != "confirmed"]
    on_event = [i for i in items if i.status == "confirmed" and i.event_slug]
    off_event = [i for i in items if i.status == "confirmed" and not i.event_slug]

    for role in sorted({i.narrative_role for i in on_event}, key=lambda r: _ROLE_ORDER.get(r, 99)):
        in_role = [i for i in on_event if i.narrative_role == role]
        header = _ROLE_HEADERS.get(role, role.replace("_", " ").title())

        # Only split by franchise where it actually helps: a handful of items
        # in one bucket reads fine as one batch, and splitting it into groups of
        # one is worse than not splitting at all.
        franchises = {i.franchise for i in in_role}
        if role == NarrativeRole.PARALLEL.value and len(in_role) > 4 and len(franchises) > 1:
            for franchise in sorted(
                franchises, key=lambda f: min(_sort_key(i) for i in in_role if i.franchise == f)
            ):
                members = sorted((i for i in in_role if i.franchise == franchise), key=_sort_key)
                clusters.append(
                    Cluster(
                        header=f"{header} — {_franchise_label(franchise)}",
                        reason=(
                            f"{len(members)} book(s) from the same corner of the universe, "
                            "running alongside the core issues. Reads as one batch."
                        ),
                        items=members,
                    )
                )
        else:
            clusters.append(
                Cluster(
                    header=header,
                    reason=_role_reason(role, len(in_role)),
                    items=sorted(in_role, key=_sort_key),
                )
            )

    if off_event:
        clusters.append(
            Cluster(
                header="Found in the wild",
                reason=(
                    "Not part of any curated event — picked up off a shelf or mentioned "
                    "in passing. No reading order applies."
                ),
                items=sorted(off_event, key=_sort_key),
            )
        )

    if pending:
        clusters.append(
            Cluster(
                header="Needs confirming",
                reason=(
                    "Resolved from a photo or a spoken title but never confirmed, so "
                    "these are guesses. Confirm on the rack before reading — an "
                    "unconfirmed match links to the wrong comic rather than failing."
                ),
                items=sorted(pending, key=_sort_key),
            )
        )
    return clusters


def _role_reason(role: str, count: int) -> str:
    reasons = {
        NarrativeRole.PRELUDE.value: "Sets up the event. Read before anything else.",
        NarrativeRole.FLASHBACK.value: (
            "Reaches back before the event. Reads better as a block than scattered "
            "through the main run."
        ),
        NarrativeRole.CORE_LINE.value: "The main line.",
        NarrativeRole.PARALLEL.value: "Happening at the same time as the core issues.",
        NarrativeRole.EPILOGUE.value: "Lands after the finale. Save these for last.",
    }
    return reasons.get(role, f"{count} issue(s) grouped by narrative role.")


def chronological(items: list[SeqItem]) -> list[SeqItem]:
    """Straight publication order — the option, not the default (SPEC §6)."""
    return sorted(items, key=lambda i: (i.published_on or date.max, i.series_name,
                                        i.issue_number or 0))


def sequence_payload(items: list[SeqItem]) -> dict[str, Any]:
    clusters = build_clusters(items)
    return {
        "count": len(items),
        "ordering": "clustered",
        "how_to_read_this": (
            "Grouped into batches meant to be read together rather than sorted. Each "
            "group says why its issues belong together. Every issue carries its "
            "in-universe anchor, character family, narrative role, series and "
            "publication date, so these can be regrouped a different way without "
            "another call."
        ),
        "groups": [c.to_payload() for c in clusters],
        "other_orderings_available": [
            "chronological — strict publication order, ask for it explicitly",
            "by event — everything from one event together",
            "by character family — all the Spider-books, all the X-books",
        ],
    }
