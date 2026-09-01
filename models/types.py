"""Enumerations shared by the DB models, the curation schema, and the tools.

Everything here is a closed vocabulary on purpose. Curation is hand-written
YAML, so a typo in a role or a franchise name would otherwise sail through and
silently drop an issue out of a cluster.
"""

from enum import StrEnum


class CurationStatus(StrEnum):
    """How much to trust an event's ordering and annotations.

    CURATED means the strict data-quality gates in tests/unit/test_curation_data.py
    apply in full — including that every issue is either linkable or explicitly
    flagged unavailable. BEST_EFFORT means the roster is still provisional.
    """

    CURATED = "curated"
    BEST_EFFORT = "best_effort"


class IssueRole(StrEnum):
    """An issue's job within one event."""

    CORE = "core"
    ESSENTIAL_TIE_IN = "essential_tie_in"
    OPTIONAL_TIE_IN = "optional_tie_in"


class Availability(StrEnum):
    """Whether we can hand the reader a link.

    Gate B is the whole reason this is three states in curation and two in tool
    output. UNCONFIRMED and UNAVAILABLE both render as "not on Marvel Unlimited"
    to a reader; they are distinguished here so curation can tell "we know it
    isn't there" from "we haven't synced yet", which are very different jobs.
    """

    LINKABLE = "linkable"
    UNAVAILABLE = "unavailable"
    UNCONFIRMED = "unconfirmed"


class RelationType(StrEnum):
    """Edge kinds in the curated reference graph — the omnibus-footnote layer.

    Direction is always from the issue you just read to the issue being pointed
    at, i.e. `from_issue` EXPANDS_IN `to_issue`.
    """

    EXPANDED_IN = "expanded_in"
    CONTINUES_IN = "continues_in"
    FLASHBACK_IN = "flashback_in"
    PARALLEL_TO = "parallel_to"
    EPILOGUE_IN = "epilogue_in"
    PRELUDE_TO = "prelude_to"
    REFERENCES = "references"


class NarrativeRole(StrEnum):
    """Where a tie-in sits relative to the core line, in story time.

    Curated, not derivable: publication order says nothing about whether an
    issue is a flashback. This is one of the axes sequence_bookmarks clusters on.
    """

    CORE_LINE = "core_line"
    PRELUDE = "prelude"
    FLASHBACK = "flashback"
    PARALLEL = "parallel"
    EPILOGUE = "epilogue"


class Franchise(StrEnum):
    """Character-family grouping — the other main clustering axis.

    Deliberately coarse: the point is "read the Spider-books as a batch", not a
    precise taxonomy.
    """

    SYMBIOTE = "symbiote"
    SPIDER = "spider"
    X_MEN = "x_men"
    COSMIC = "cosmic"
    AVENGERS = "avengers"
    STREET = "street"
    MYSTIC = "mystic"
    ATLANTEAN = "atlantean"
    WAKANDAN = "wakandan"
    OTHER = "other"


class BookmarkOrigin(StrEnum):
    """How a bookmark got made. Drives how the rack presents it."""

    MID_READ = "mid_read"
    SHELF_PHOTO = "shelf_photo"
    SHELF_TYPED = "shelf_typed"


class BookmarkStatus(StrEnum):
    """PENDING means a shelf candidate was never confirmed in conversation and
    is waiting on the rack. Per Gate B, a PENDING row never yields a link."""

    CONFIRMED = "confirmed"
    PENDING = "pending"


class ShelfSource(StrEnum):
    """SPEC §6: recorded for later review of resolution quality."""

    PHOTO = "photo"
    TYPED = "typed"


_ORIGIN_FOR_SHELF_SOURCE = {
    ShelfSource.PHOTO: BookmarkOrigin.SHELF_PHOTO,
    ShelfSource.TYPED: BookmarkOrigin.SHELF_TYPED,
}


def origin_for_shelf_source(source: ShelfSource) -> BookmarkOrigin:
    return _ORIGIN_FOR_SHELF_SOURCE[source]
