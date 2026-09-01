"""The read-only tool surface: list_events, get_event_guide, whats_next.

After step 5 of the build order this is a working reading companion with no
persistence, which is already useful — so it gets tested on its own.
"""

from __future__ import annotations

import pytest

from curation.resolve import candidates_from_guide, resolve
from marvel.links import NOT_ON_MU, assert_tappable
from marvel.sync import apply_record, promote_availability
from service import guide as guide_service


def _find(rows: list[dict], issue: str) -> dict:
    """Lookup that fails as an assertion.

    A bare next() over a generator raises StopIteration, which asyncio turns
    into an unrelated "coroutine raised StopIteration" RuntimeError — a useless
    error message for what is really just a missing row.
    """
    match = [r for r in rows if r["issue"] == issue]
    assert match, f"{issue!r} not found in {[r['issue'] for r in rows]}"
    return match[0]


async def _entry(session, ref: str):
    entries = await guide_service.all_entries(session)
    resolution = resolve(ref, candidates_from_guide(entries))
    assert resolution.resolved, f"{ref!r} did not resolve"
    return next(e for e in entries if e.key == resolution.matched.key)


async def test_list_events_reports_curation_depth(session, loaded_event) -> None:
    events = await guide_service.list_events(session)
    assert len(events) == 1
    payload = events[0].to_payload()
    assert payload["slug"] == "king-in-black"
    assert payload["curation"] == "best_effort"
    assert payload["core_issues"] == 5
    assert payload["issues"] == 40


async def test_event_guide_is_in_curated_order(session, loaded_event) -> None:
    guide = await guide_service.get_event_guide(session, "king-in-black")
    positions = [e["position"] for e in guide["reading_order"]]
    assert positions == list(range(1, len(positions) + 1))
    # The hand-moved prelude leads, ahead of the core #1 it publishes months before.
    assert guide["reading_order"][0]["issue"].startswith("Web of Venom")
    assert guide["reading_order"][1]["issue"] == "King in Black #1"


async def test_unknown_event_says_how_to_find_the_slugs(session) -> None:
    with pytest.raises(ValueError, match="no event with slug"):
        await guide_service.get_event_guide(session, "secret-wars")


async def test_unlinkable_issues_say_so_explicitly(session, loaded_event) -> None:
    """SPEC §6: every link is a confirmed reader URL or an explicit
    "not on Marvel Unlimited". No third state, and no guesses."""
    guide = await guide_service.get_event_guide(session, "king-in-black")
    links = {e["link"] for e in guide["reading_order"]}
    assert links == {NOT_ON_MU}, "nothing should be linkable before a real sync"
    assert "unverified" in guide["note"]


async def test_a_synced_issue_becomes_a_tappable_markdown_link(
    session, loaded_event, record_index
) -> None:
    from sqlalchemy import select

    from models.catalog import Issue

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    promote_availability(issue)
    await session.commit()

    guide = await guide_service.get_event_guide(session, "king-in-black")
    entry = _find(guide["reading_order"], "King in Black #1")
    assert entry["link"] == (
        "[King in Black #1](https://read.marvel.com/#/book/55901)"
    )
    for row in guide["reading_order"]:
        assert_tappable(row["link"])


async def test_whats_next_returns_the_next_core_issue(session, loaded_event) -> None:
    entry = await _entry(session, "King in Black #3")
    result = await guide_service.whats_next(session, entry)
    assert result["next_core_issue"]["issue"] == "King in Black #4"
    assert result["event"] == "King in Black"


async def test_whats_next_lists_what_to_read_before_the_next_core(
    session, loaded_event
) -> None:
    entry = await _entry(session, "King in Black #3")
    result = await guide_service.whats_next(session, entry)
    between = [e["issue"] for e in result["read_before_next_core"]]
    assert between, "there are tie-ins between #3 and #4 and they should be listed"
    assert all(e["role"] != "core" for e in result["read_before_next_core"])


async def test_whats_next_surfaces_the_curated_footnotes(session, loaded_event) -> None:
    """The half no API could produce: which tie-in expands the scene you just
    read, and why."""
    entry = await _entry(session, "King in Black #3")
    result = await guide_service.whats_next(session, entry)
    expands = result["expands_on_what_you_just_read"]
    assert expands, "King in Black #3 has curated reference edges"

    targets = {e["issue"] for e in expands}
    assert "King in Black: Planet of the Symbiotes #1" in targets
    valkyries_matches = [e for e in expands if "Valkyries" in e["issue"]]
    assert valkyries_matches, f"no Valkyries edge in {targets}"
    valkyries = valkyries_matches[0]
    assert valkyries["relation"] == "expanded in"
    assert valkyries["why"], "an edge with no reason is a bookmark nobody can act on"
    for edge in expands:
        assert_tappable(edge["link"])


async def test_relation_phrasing_reads_as_a_sentence(session, loaded_event) -> None:
    entry = await _entry(session, "King in Black #2")
    result = await guide_service.whats_next(session, entry)
    phrases = {e["relation"] for e in result["expands_on_what_you_just_read"]}
    assert "the flashback is in" in phrases
    assert "happening in parallel in" in phrases


async def test_the_last_core_issue_reports_the_end(session, loaded_event) -> None:
    entry = await _entry(session, "King in Black #5")
    result = await guide_service.whats_next(session, entry)
    assert result["next_core_issue"] is None
    # Epilogues still follow, so this is not "at_the_end".
    assert result["read_before_next_core"]
    assert result["expands_on_what_you_just_read"]


async def test_the_very_last_entry_is_at_the_end(session, loaded_event) -> None:
    entries = await guide_service.all_entries(session)
    last = max(entries, key=lambda e: e.position)
    result = await guide_service.whats_next(session, last)
    assert result["at_the_end"] is True


async def test_every_payload_carries_marvels_attribution(session, loaded_event) -> None:
    """Their terms require it wherever their data is displayed."""
    guide = await guide_service.get_event_guide(session, "king-in-black")
    assert "Marvel" in guide["attribution"]
    entry = await _entry(session, "King in Black #1")
    assert "Marvel" in (await guide_service.whats_next(session, entry))["attribution"]
