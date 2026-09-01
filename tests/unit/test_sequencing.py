"""Clustering saved tie-ins.

The point of SPEC §2: read in capture order — or even strict publication order —
tie-ins are a list of errands. The tool's job is to hand back batches that read
together, plus every axis needed to regroup them, and *not* to be a sort.
"""

from __future__ import annotations

from datetime import date

import pytest

from service.sequencing import build_clusters, chronological, sequence_payload


def item(
    key: str,
    *,
    narrative_role: str = "parallel",
    franchise: str = "other",
    position: int | None = None,
    published: date | None = None,
    event: str = "king-in-black",
    status: str = "confirmed",
    **kwargs,
):
    from service.sequencing import SeqItem

    return SeqItem(
        bookmark_id=key,
        key=key,
        display=key,
        series_name=kwargs.pop("series", key),
        issue_number=1,
        event_slug=event,
        event_name=event,
        position=position,
        role="optional_tie_in",
        narrative_role=narrative_role,
        franchise=franchise,
        published_on=published,
        note="",
        provenance="",
        link="",
        status=status,
        origin="mid_read",
        **kwargs,
    )


def test_in_universe_timing_orders_the_groups() -> None:
    """A flashback belongs before the parallel material and an epilogue after,
    regardless of when either published."""
    items = [
        item("epilogue", narrative_role="epilogue", published=date(2021, 4, 14)),
        item("flashback", narrative_role="flashback", published=date(2021, 3, 17)),
        item("prelude", narrative_role="prelude", published=date(2020, 9, 16)),
        item("parallel", narrative_role="parallel", published=date(2021, 1, 6)),
    ]
    headers = [c.header for c in build_clusters(items)]
    assert headers == [
        "Before it starts", "The flashbacks", "Alongside the main story",
        "After the dust settles",
    ]


def test_this_is_not_a_chronological_sort() -> None:
    """The load-bearing assertion of the whole feature."""
    items = [
        item("late-flashback", narrative_role="flashback", published=date(2021, 4, 21)),
        item("early-parallel", narrative_role="parallel", published=date(2020, 12, 30)),
    ]
    clustered = [i.display for c in build_clusters(items) for i in c.items]
    by_date = [i.display for i in chronological(items)]
    assert clustered == ["late-flashback", "early-parallel"]
    assert by_date == ["early-parallel", "late-flashback"]
    assert clustered != by_date


def test_parallel_material_splits_by_character_family() -> None:
    """"read the Spider-books as a batch" is the stated win, and the parallel
    tie-ins are where a flat list hurts most."""
    items = [
        item(f"spider-{n}", franchise="spider", position=n) for n in range(1, 4)
    ] + [item(f"xmen-{n}", franchise="x_men", position=10 + n) for n in range(1, 4)]
    clusters = build_clusters(items)
    headers = [c.header for c in clusters]
    assert "Alongside the main story — Spider-books" in headers
    assert "Alongside the main story — X-books" in headers
    spider = next(c for c in clusters if "Spider" in c.header)
    assert [i.display for i in spider.items] == ["spider-1", "spider-2", "spider-3"]


def test_a_small_bucket_is_not_split_into_groups_of_one() -> None:
    """Splitting three items across three headers is worse than not splitting."""
    items = [
        item("a", franchise="spider", position=1),
        item("b", franchise="x_men", position=2),
        item("c", franchise="cosmic", position=3),
    ]
    clusters = build_clusters(items)
    assert len(clusters) == 1
    assert clusters[0].header == "Alongside the main story"


def test_off_event_finds_get_their_own_ungrouped_section() -> None:
    """SPEC §6: the shelf is not scoped to an event, so the rack needs a section
    for entries with no event context."""
    items = [item("in-event", position=1), item("wild", event="")]
    clusters = build_clusters(items)
    wild = next(c for c in clusters if c.header == "Found in the wild")
    assert [i.display for i in wild.items] == ["wild"]


def test_unconfirmed_entries_are_quarantined_last() -> None:
    """Pending shelf entries are guesses, so they must be visibly separate and
    never mixed into a reading order (Gate B)."""
    items = [item("solid", position=1), item("guess", status="pending")]
    clusters = build_clusters(items)
    assert clusters[-1].header == "Needs confirming"
    assert [i.display for i in clusters[-1].items] == ["guess"]


def test_curated_position_beats_publication_date_within_a_group() -> None:
    """A human already made the ordering call; the date is only a tiebreak."""
    items = [
        item("second", position=2, published=date(2020, 1, 1)),
        item("first", position=1, published=date(2021, 1, 1)),
    ]
    clusters = build_clusters(items)
    assert [i.display for i in clusters[0].items] == ["first", "second"]


def test_payload_carries_every_regrouping_axis() -> None:
    """The last mile of judgment is Claude's, so nothing may be collapsed away
    into a sort key."""
    items = [item("a", position=1, franchise="spider", published=date(2021, 2, 10))]
    items[0].anchor = "around King in Black #3"
    payload = sequence_payload(items)
    issue = payload["groups"][0]["issues"][0]
    for axis in ("franchise", "narrative_role", "in_universe_anchor", "series",
                 "published_on", "curated_position"):
        assert axis in issue, f"{axis} missing — Claude cannot recluster without it"
    assert payload["ordering"] == "clustered"
    assert any("chronological" in o for o in payload["other_orderings_available"])


def test_every_group_explains_itself() -> None:
    """A batch you don't understand is just a shorter list."""
    items = [item(f"i{n}", position=n) for n in range(1, 4)]
    for cluster in build_clusters(items):
        assert cluster.reason.strip(), f"{cluster.header} has no stated reason"


@pytest.mark.parametrize("count", [0, 1])
def test_degenerate_inputs_do_not_crash(count: int) -> None:
    items = [item(f"i{n}", position=n) for n in range(count)]
    assert len(build_clusters(items)) == (1 if count else 0)


def test_payload_includes_the_note_and_provenance_when_present() -> None:
    """Both are what make a card readable a week later, so neither may be
    dropped in serialization."""
    saved = item("a", position=1)
    saved.note = "want the global scale"
    saved.provenance = "Expanded in King in Black #3"
    payload = saved.to_payload()
    assert payload["your_note"] == "want the global scale"
    assert payload["why_you_saved_it"] == "Expanded in King in Black #3"


def test_a_pending_item_carries_its_status_and_raw_text() -> None:
    """The rack needs the original words to re-offer the choice, and the status
    so it never renders as confirmed."""
    guess = item("g", status="pending")
    guess.raw_text = "that venom one with the red cover"
    payload = guess.to_payload()
    assert payload["status"] == "pending"
    assert payload["raw_text"] == "that venom one with the red cover"


def test_a_confirmed_item_carries_neither() -> None:
    payload = item("c", position=1).to_payload()
    assert "status" not in payload
    assert "raw_text" not in payload


def test_an_unknown_franchise_still_gets_a_readable_label() -> None:
    """Curation could add a Franchise member before the label map catches up;
    the group header must stay readable rather than showing a raw enum value."""
    items = [
        item(f"i{n}", franchise="new_corner", position=n) for n in range(1, 4)
    ] + [item(f"j{n}", franchise="spider", position=10 + n) for n in range(1, 4)]
    headers = [c.header for c in build_clusters(items)]
    assert any("New Corner" in h for h in headers)


def test_an_unknown_narrative_role_still_groups() -> None:
    clusters = build_clusters([item("x", narrative_role="interlude", position=1)])
    assert clusters[0].header == "Interlude"
    assert clusters[0].reason
