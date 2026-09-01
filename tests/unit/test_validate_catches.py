"""The §8 gates, tested by breaking things.

`test_curation_data.py` asserts the real files pass. That is necessary and not
sufficient: a gate that has never fired is not known to work, and these gates
are the only thing standing between a hand-edited YAML file and a kid tapping
through to the wrong comic.

Every test here mutates a copy of the real King in Black data, so the fixtures
stay realistic rather than being minimal shapes that happen to satisfy the
parser.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from curation import validate
from curation.schema import CuratedEvent, CuratedReference, load_all_events
from models.types import Availability, CurationStatus, IssueRole, RelationType


@pytest.fixture
def event() -> CuratedEvent:
    return load_all_events()[0]


def _with_issues(event: CuratedEvent, mutate) -> CuratedEvent:
    return replace(event, issues=[mutate(i) for i in event.ordered])


def _rules(problems) -> set[str]:
    return {p.rule for p in problems}


# --- positions ---


def test_a_duplicate_position_is_caught(event) -> None:
    broken = _with_issues(
        event, lambda i: replace(i, position=1) if i.position == 2 else i
    )
    problems = validate.check_positions_contiguous(broken)
    assert problems
    assert "position 1 used by 2 issues" in str(problems[0])


def test_a_gap_in_the_order_is_caught(event) -> None:
    broken = _with_issues(
        event, lambda i: replace(i, position=99) if i.position == 3 else i
    )
    problems = validate.check_positions_contiguous(broken)
    assert _rules(problems) == {"positions"}
    assert any("missing positions [3]" in str(p) for p in problems)


def test_a_zero_indexed_order_is_caught(event) -> None:
    """Off-by-one in a hand-edited file is the likeliest way to break this."""
    broken = _with_issues(event, lambda i: replace(i, position=i.position - 1))
    assert validate.check_positions_contiguous(broken)


# --- core completeness ---


def test_a_missing_core_issue_is_caught(event) -> None:
    kept = [i for i in event.ordered if i.key != "king-in-black-3"]
    renumbered = [replace(i, position=n) for n, i in enumerate(kept, start=1)]
    problems = validate.check_core_complete(replace(event, issues=renumbered))
    assert any("'king-in-black-3' is missing" in str(p) for p in problems)


def test_a_core_issue_demoted_to_a_tie_in_is_caught(event) -> None:
    broken = _with_issues(
        event,
        lambda i: replace(i, role=IssueRole.OPTIONAL_TIE_IN)
        if i.key == "king-in-black-2"
        else i,
    )
    problems = validate.check_core_complete(broken)
    assert any("part of the core run" in str(p) for p in problems)


def test_a_tie_in_wrongly_marked_core_is_caught(event) -> None:
    """Silently inflates what a reader thinks is mandatory."""
    broken = _with_issues(
        event,
        lambda i: replace(i, role=IssueRole.CORE) if i.key == "venom-31" else i,
    )
    problems = validate.check_core_complete(broken)
    assert any("not in the declared core run" in str(p) for p in problems)


def test_core_issues_out_of_order_are_caught(event) -> None:
    by_key = {i.key: i for i in event.ordered}
    first, last = by_key["king-in-black-1"], by_key["king-in-black-5"]
    broken = _with_issues(
        event,
        lambda i: (
            replace(i, position=last.position)
            if i.key == "king-in-black-1"
            else replace(i, position=first.position)
            if i.key == "king-in-black-5"
            else i
        ),
    )
    problems = validate.check_core_complete(broken)
    assert any("not in issue-number order" in str(p) for p in problems)


def test_an_event_that_declares_no_core_run_is_caught(event) -> None:
    """Without a declared spine the core gate would be a tautology."""
    problems = validate.check_core_complete(replace(event, core_series="", core_count=0))
    assert any("declare `core:" in str(p) for p in problems)


# --- Gate B ---


def test_a_digital_id_with_no_cached_record_is_caught(event, record_index) -> None:
    broken = _with_issues(
        event,
        lambda i: replace(i, digital_id=999999) if i.key == "venom-31" else i,
    )
    problems = validate.check_digital_ids_traceable(broken, record_index)
    assert any("no cached Marvel record exists" in str(p) for p in problems)


def test_a_digital_id_that_disagrees_with_marvel_is_caught(event, record_index) -> None:
    """The exact Gate B hazard: a plausible id that isn't the one Marvel gave."""
    broken = _with_issues(
        event,
        lambda i: replace(i, digital_id=55902) if i.key == "king-in-black-1" else i,
    )
    problems = validate.check_digital_ids_traceable(broken, record_index)
    assert any("does not come from Marvel's response" in str(p) for p in problems)


def test_an_id_copied_from_the_wrong_issue_is_caught(event, record_index) -> None:
    """Marvel's real id for King in Black #1, pasted onto Namor #1."""
    real = record_index["king-in-black-1"].digital_id
    broken = _with_issues(
        event,
        lambda i: replace(i, digital_id=real) if i.key == "king-in-black-namor-1" else i,
    )
    problems = validate.check_digital_ids_traceable(broken, record_index)
    assert problems, "an id belonging to a different issue must not validate"


def test_with_no_cache_available_any_claimed_id_is_rejected(event) -> None:
    """Fails closed. An unverified id is exactly the state Gate B calls dangerous."""
    broken = _with_issues(
        event, lambda i: replace(i, digital_id=1) if i.position == 1 else i
    )
    problems = validate.check_digital_ids_traceable(broken, None)
    assert any("no Marvel cache is available" in str(p) for p in problems)


def test_claiming_nothing_needs_no_cache(event) -> None:
    """The real files claim no ids, so they must pass without a cache."""
    assert validate.check_digital_ids_traceable(event, None) == []


# --- reference edges ---


def test_an_edge_pointing_at_an_unknown_issue_is_caught(event) -> None:
    broken = replace(
        event,
        references=[
            *event.references,
            CuratedReference("king-in-black-1", "fantastic-four-52", RelationType.REFERENCES),
        ],
    )
    problems = validate.check_references_resolve(broken)
    assert any("unknown `to` issue" in str(p) for p in problems)


def test_an_edge_from_an_unknown_issue_is_caught(event) -> None:
    broken = replace(
        event,
        references=[CuratedReference("nope-1", "king-in-black-1", RelationType.REFERENCES)],
    )
    problems = validate.check_references_resolve(broken)
    assert any("unknown `from` issue" in str(p) for p in problems)


def test_a_self_referencing_edge_is_caught(event) -> None:
    broken = replace(
        event,
        references=[
            CuratedReference("king-in-black-1", "king-in-black-1", RelationType.REFERENCES)
        ],
    )
    problems = validate.check_references_resolve(broken)
    assert any("points at itself" in str(p) for p in problems)


# --- linkability, which only binds on a curated event ---


def test_unconfirmed_issues_are_allowed_while_best_effort(event) -> None:
    """An event still being seeded is allowed to be honest about it."""
    assert event.curation_status is CurationStatus.BEST_EFFORT
    assert validate.check_linkable_or_flagged(event) == []


def test_claiming_curated_status_with_unconfirmed_issues_is_caught(event) -> None:
    curated = replace(event, curation_status=CurationStatus.CURATED)
    problems = validate.check_linkable_or_flagged(curated)
    assert problems
    assert any("still `unconfirmed`" in str(p) for p in problems)


def test_linkable_without_an_id_is_caught(event) -> None:
    broken = replace(
        _with_issues(
            event,
            lambda i: replace(i, availability=Availability.LINKABLE, provisional=False)
            if i.position == 1
            else replace(
                i, availability=Availability.UNAVAILABLE, unavailable_note="n/a",
                provisional=False,
            ),
        ),
        curation_status=CurationStatus.CURATED,
    )
    problems = validate.check_linkable_or_flagged(broken)
    assert any("nothing to build a reader URL from" in str(p) for p in problems)


def test_unavailable_without_a_note_is_caught(event) -> None:
    """The rack needs something to tell the reader."""
    broken = replace(
        _with_issues(
            event,
            lambda i: replace(i, availability=Availability.UNAVAILABLE, provisional=False),
        ),
        curation_status=CurationStatus.CURATED,
    )
    problems = validate.check_linkable_or_flagged(broken)
    assert any("flagged unavailable with no note" in str(p) for p in problems)


def test_provisional_entries_cannot_claim_curated_status(event) -> None:
    broken = replace(
        _with_issues(
            event,
            lambda i: replace(
                i, availability=Availability.UNAVAILABLE, unavailable_note="n/a"
            ),
        ),
        curation_status=CurationStatus.CURATED,
    )
    problems = validate.check_linkable_or_flagged(broken)
    assert any("still marked provisional" in str(p) for p in problems)


def test_problems_aggregates_every_gate(event, record_index) -> None:
    broken = replace(
        _with_issues(
            event,
            lambda i: replace(i, position=1, digital_id=42) if i.position == 2 else i,
        ),
        references=[CuratedReference("nope-1", "king-in-black-1", RelationType.REFERENCES)],
    )
    assert _rules(validate.problems(broken, record_index)) >= {
        "positions", "gate-b", "references",
    }


def test_an_id_from_a_record_with_a_different_series_is_caught(event, record_index) -> None:
    """The identity cross-check. A cached record can legitimately exist and
    still not *be* this issue — a key collision or a hand-edited series name —
    so matching by key alone is not enough to trust the id."""
    real = record_index["king-in-black-1"]
    mismatched = replace(
        real, series_name="Some Other Book", series_slug="some-other-book"
    )
    problems = validate.check_digital_ids_traceable(
        _with_issues(
            event,
            lambda i: replace(i, digital_id=real.digital_id)
            if i.key == "king-in-black-1"
            else i,
        ),
        {"king-in-black-1": mismatched},
    )
    assert any("series/number mismatch" in str(p) for p in problems)


def test_an_id_from_a_record_with_a_different_issue_number_is_caught(
    event, record_index
) -> None:
    real = record_index["king-in-black-1"]
    problems = validate.check_digital_ids_traceable(
        _with_issues(
            event,
            lambda i: replace(i, digital_id=real.digital_id)
            if i.key == "king-in-black-1"
            else i,
        ),
        {"king-in-black-1": replace(real, issue_number=99)},
    )
    assert any("series/number mismatch" in str(p) for p in problems)
