"""The curated-data gates from SPEC §8, parameterized over every event file.

These run before any serving code is involved — no DB, no network — which is
the whole reason step 4 of the build order comes before step 5. If curation
isn't trustworthy, nothing built on top of it is.
"""

from __future__ import annotations

import pytest

from curation import validate
from curation.schema import CuratedEvent, load_all_events
from models.types import Availability

EVENTS = load_all_events()
IDS = [e.slug for e in EVENTS]


def test_at_least_one_event_is_curated() -> None:
    """A guide with no events is not a guide. Guards against a loader that
    silently finds nothing and makes every other test below vacuous."""
    assert EVENTS, "no curation YAML found in curation/events/"


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_positions_are_contiguous(event: CuratedEvent) -> None:
    assert validate.check_positions_contiguous(event) == []


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_core_issues_present_and_ordered(event: CuratedEvent) -> None:
    assert validate.check_core_complete(event) == []


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_digital_ids_trace_to_a_marvel_record(event: CuratedEvent, record_index) -> None:
    """Gate B, directly.

    Every curated `digital_id` must come from a Marvel API response for that
    same issue. This is the test that keeps a kid from tapping the Namor tie-in
    and landing in an unrelated comic.
    """
    assert validate.check_digital_ids_traceable(event, record_index) == []


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_references_resolve(event: CuratedEvent) -> None:
    assert validate.check_references_resolve(event) == []


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_every_issue_is_linkable_or_flagged(event: CuratedEvent) -> None:
    assert validate.check_linkable_or_flagged(event) == []


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_no_issue_claims_linkable_without_an_id(event: CuratedEvent) -> None:
    """Holds for *every* event, curated or not: `linkable` without a digital_id
    would render a link built from nothing."""
    for issue in event.issues:
        if issue.availability is Availability.LINKABLE:
            assert issue.digital_id, f"{issue.key} is linkable with no digital_id"


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_reference_notes_explain_themselves(event: CuratedEvent) -> None:
    """Most edges should say *why*.

    The graph's value is the explanation, not the adjacency — "referenced in
    King in Black #3" without a reason is a bookmark nobody can act on a week
    later. A few bare edges are fine; a file of them means the curation work
    was skipped.
    """
    if not event.references:
        return
    annotated = sum(1 for r in event.references if r.note.strip())
    assert annotated >= len(event.references) // 2, (
        f"{event.slug}: only {annotated}/{len(event.references)} reference edges have "
        "a note explaining the connection"
    )


@pytest.mark.parametrize("event", EVENTS, ids=IDS)
def test_all_gates_together(event: CuratedEvent, record_index) -> None:
    problems = validate.problems(event, record_index)
    assert problems == [], "\n".join(str(p) for p in problems)
