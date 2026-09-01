"""The data-quality gates from SPEC §8, as pure functions.

These run before any serving code exists, over the YAML files directly, so
curation is trustworthy from the start. `tests/unit/test_curation_data.py`
parameterizes over every event file and asserts `problems()` is empty.

The most important one is `check_digital_ids_traceable`. It is the test that
keeps a kid from tapping the Namor tie-in and landing in an unrelated comic.
"""

from __future__ import annotations

from dataclasses import dataclass

from curation.schema import CuratedEvent
from marvel.records import ComicRecord, issue_key
from models.types import Availability, IssueRole


@dataclass(frozen=True)
class Problem:
    """One rule violation. `rule` names the SPEC §8 bullet it enforces."""

    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def check_positions_contiguous(event: CuratedEvent) -> list[Problem]:
    """Order positions are contiguous, no gaps or duplicates."""
    positions = [i.position for i in event.issues]
    problems: list[Problem] = []

    duplicates = sorted({p for p in positions if positions.count(p) > 1})
    for position in duplicates:
        keys = sorted(i.key for i in event.issues if i.position == position)
        problems.append(
            Problem("positions", f"position {position} used by {len(keys)} issues: {keys}")
        )

    if positions:
        expected = set(range(1, len(positions) + 1))
        missing = sorted(expected - set(positions))
        extra = sorted(p for p in set(positions) if p not in expected)
        if missing:
            problems.append(
                Problem("positions", f"gaps in reading order: missing positions {missing}")
            )
        if extra:
            problems.append(
                Problem(
                    "positions",
                    f"positions outside 1..{len(positions)}: {extra} — the order must be "
                    "a dense sequence so 'what's next' is always answerable",
                )
            )
    return problems


def check_core_complete(event: CuratedEvent) -> list[Problem]:
    """Every core issue in the event is present and ordered."""
    if not event.core_series or not event.core_count:
        return [
            Problem(
                "core",
                f"{event.slug}: declare `core: {{series, count}}` so core completeness "
                "can be checked — without it this gate is a tautology",
            )
        ]

    problems: list[Problem] = []
    expected_keys = [
        issue_key(event.core_series, n) for n in range(1, event.core_count + 1)
    ]
    by_key = event.issues_by_key
    for expected in expected_keys:
        issue = by_key.get(expected)
        if issue is None:
            problems.append(
                Problem("core", f"core issue {expected!r} is missing from the reading order")
            )
        elif issue.role is not IssueRole.CORE:
            problems.append(
                Problem(
                    "core",
                    f"{expected!r} is part of the core run but is marked "
                    f"role={issue.role.value!r}",
                )
            )

    # Marked core but not part of the declared run — usually a copy-paste slip,
    # and it silently inflates what a reader thinks is mandatory.
    for issue in event.core_issues:
        if issue.key not in expected_keys:
            problems.append(
                Problem(
                    "core",
                    f"{issue.key!r} is marked role=core but is not in the declared core "
                    f"run ({event.core_series} #1-{event.core_count})",
                )
            )

    # Core issues must read in issue-number order.
    core_present = [i for i in event.core_issues if i.key in expected_keys]
    by_position = sorted(core_present, key=lambda i: i.position)
    if [i.issue_number for i in by_position] != sorted(i.issue_number for i in by_position):
        problems.append(
            Problem(
                "core",
                "core issues are not in issue-number order: "
                + " -> ".join(f"#{i.issue_number}@{i.position}" for i in by_position),
            )
        )
    return problems


def check_digital_ids_traceable(
    event: CuratedEvent, record_index: dict[str, ComicRecord] | None
) -> list[Problem]:
    """Every `digital_id` traces back to a cached Marvel record for that issue.

    Directly enforces Gate B. A curated `digital_id` is only ever legitimate as
    a *copy* of something Marvel returned for this exact issue; anything else is
    derived, inferred, or guessed, and Gate B proved a guess does not error — it
    quietly opens a different comic.

    With no cache available (`record_index is None`), any curated digital_id is
    unverifiable and therefore rejected. Failing closed is the point: an
    unverified id is exactly the state Gate B says is dangerous.
    """
    claimed = [i for i in event.issues if i.digital_id]
    if not claimed:
        return []
    if record_index is None:
        return [
            Problem(
                "gate-b",
                f"{len(claimed)} issue(s) carry a digital_id but no Marvel cache is "
                "available to verify them against: "
                + ", ".join(sorted(i.key for i in claimed))
                + ". Run `make sync-event` first — an unverified id is a Gate B hazard.",
            )
        ]

    problems: list[Problem] = []
    for issue in claimed:
        record = record_index.get(issue.key)
        if record is None:
            problems.append(
                Problem(
                    "gate-b",
                    f"{issue.key!r} claims digital_id={issue.digital_id} but no cached "
                    "Marvel record exists for that issue",
                )
            )
            continue
        if record.digital_id != issue.digital_id:
            problems.append(
                Problem(
                    "gate-b",
                    f"{issue.key!r} claims digital_id={issue.digital_id} but Marvel's "
                    f"record for it says {record.digital_id!r} — the curated id does not "
                    "come from Marvel's response for this issue",
                )
            )
        # Identity cross-check: the record has to actually *be* this issue.
        if record.issue_number != issue.issue_number or record.series_slug != issue.series_slug:
            problems.append(
                Problem(
                    "gate-b",
                    f"{issue.key!r} matched a Marvel record for "
                    f"{record.series_name} #{record.issue_number} — series/number mismatch",
                )
            )
    return problems


def check_references_resolve(event: CuratedEvent) -> list[Problem]:
    """Every `issue_reference` points at an issue that exists in the dataset."""
    known = set(event.issues_by_key)
    problems: list[Problem] = []
    for ref in event.references:
        for side, key in (("from", ref.from_key), ("to", ref.to_key)):
            if key not in known:
                problems.append(
                    Problem(
                        "references",
                        f"reference {ref.from_key!r} -> {ref.to_key!r} has an unknown "
                        f"`{side}` issue {key!r}",
                    )
                )
        if ref.from_key == ref.to_key:
            problems.append(
                Problem("references", f"reference {ref.from_key!r} points at itself")
            )
    return problems


def check_linkable_or_flagged(event: CuratedEvent) -> list[Problem]:
    """Every issue is either linkable or explicitly flagged unavailable.

    Only applies to events claiming `curation_status: curated`. An event still
    being seeded is allowed to hold UNCONFIRMED issues — that state is honest,
    and it renders as "not on Marvel Unlimited" to the reader either way. What
    is *not* allowed is claiming full curation while the roster is unverified.
    """
    if not event.is_curated:
        return []
    problems: list[Problem] = []
    for issue in event.ordered:
        if issue.availability is Availability.LINKABLE and not issue.digital_id:
            problems.append(
                Problem(
                    "linkable",
                    f"{issue.key!r} is marked linkable but has no digital_id — "
                    "there is nothing to build a reader URL from",
                )
            )
        elif issue.availability is Availability.UNCONFIRMED:
            problems.append(
                Problem(
                    "linkable",
                    f"{issue.key!r} is still `unconfirmed` in an event marked "
                    "`curated`. Either sync a digital_id or flag it "
                    "`availability: unavailable` with a note.",
                )
            )
        if issue.availability is Availability.UNAVAILABLE and not issue.unavailable_note:
            problems.append(
                Problem(
                    "linkable",
                    f"{issue.key!r} is flagged unavailable with no note — the rack has "
                    "nothing to tell the reader about why",
                )
            )
        if issue.provisional:
            problems.append(
                Problem(
                    "linkable",
                    f"{issue.key!r} is still marked provisional in an event claiming "
                    "`curated` status",
                )
            )
    return problems


def problems(
    event: CuratedEvent, record_index: dict[str, ComicRecord] | None = None
) -> list[Problem]:
    """Every gate, in one call."""
    return [
        *check_positions_contiguous(event),
        *check_core_complete(event),
        *check_digital_ids_traceable(event, record_index),
        *check_references_resolve(event),
        *check_linkable_or_flagged(event),
    ]
