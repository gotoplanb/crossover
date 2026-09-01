"""Guardrails on hand-edited YAML.

The curation files are written by a person, so every one of these errors is a
plausible typo. What matters is that each fails loudly at load with a message
naming the file and the field — a silently dropped issue or a mis-keyed
reference edge is how curation quietly rots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from curation.schema import CurationError, load_all_events, parse_event

BASE = {
    "slug": "test-event",
    "display_name": "Test Event",
    "core": {"series": "Test", "count": 1},
}


def _event(**overrides):
    return parse_event({**BASE, **overrides})


def _issue(**overrides) -> dict:
    return {"series": "Test", "number": 1, "position": 1, **overrides}


def test_a_missing_slug_is_refused() -> None:
    with pytest.raises(CurationError, match="missing `slug`"):
        parse_event({"display_name": "no slug"})


def test_an_issue_without_series_or_number_is_refused() -> None:
    for broken in ({"number": 1, "position": 1}, {"series": "Test", "position": 1}):
        with pytest.raises(CurationError, match="needs `series` and `number`"):
            _event(issues=[broken])


def test_an_issue_without_a_position_is_refused() -> None:
    """Position is what makes the contiguity gate checkable at all."""
    with pytest.raises(CurationError, match="has no `position`"):
        _event(issues=[{"series": "Test", "number": 1}])


def test_a_key_that_disagrees_with_series_and_number_is_refused() -> None:
    """The dangerous one: reference edges point at keys, so a hand-written key
    that doesn't match its own issue makes edges resolve to nothing."""
    with pytest.raises(CurationError, match="does not match series\\+number"):
        _event(issues=[_issue(key="something-else-3")])


def test_a_matching_explicit_key_is_accepted() -> None:
    event = _event(issues=[_issue(key="test-1")])
    assert event.issues[0].key == "test-1"


@pytest.mark.parametrize(
    ("field", "value", "expected_in_message"),
    [
        ("role", "sidekick", "core, essential_tie_in, optional_tie_in"),
        ("narrative_role", "prequel", "core_line"),
        ("franchise", "spiderman", "spider"),
        ("availability", "maybe", "linkable"),
    ],
)
def test_an_unknown_enum_value_lists_the_allowed_ones(
    field, value, expected_in_message
) -> None:
    """A typo in a role would silently drop an issue out of a cluster, so the
    error has to say what was allowed."""
    with pytest.raises(CurationError) as exc:
        _event(issues=[_issue(**{field: value})])
    assert value in str(exc.value)
    assert expected_in_message in str(exc.value)


def test_an_unknown_curation_status_is_refused() -> None:
    with pytest.raises(CurationError, match="curation_status"):
        _event(curation_status="mostly")


def test_an_unknown_relation_type_is_refused() -> None:
    with pytest.raises(CurationError, match="type="):
        _event(
            issues=[_issue()],
            references=[{"from": "test-1", "to": "test-1", "type": "kind-of-related"}],
        )


def test_a_reference_missing_an_endpoint_is_refused() -> None:
    for broken in ({"to": "test-1"}, {"from": "test-1"}):
        with pytest.raises(CurationError, match="needs `from` and `to`"):
            _event(issues=[_issue()], references=[broken])


@pytest.mark.parametrize("field", ["published_on"])
def test_a_malformed_date_is_refused(field) -> None:
    with pytest.raises(CurationError, match="is not an ISO date"):
        _event(issues=[_issue(**{field: "December 2020"})])


@pytest.mark.parametrize("field", ["started_on", "ended_on"])
def test_a_malformed_event_date_is_refused(field) -> None:
    with pytest.raises(CurationError, match="is not an ISO date"):
        _event(**{field: "2020-13-45"})


def test_an_empty_date_is_treated_as_absent() -> None:
    assert _event(issues=[_issue(published_on="")]).issues[0].published_on is None
    assert _event(issues=[_issue(published_on=None)]).issues[0].published_on is None


def test_a_real_date_object_passes_through() -> None:
    """PyYAML parses unquoted `2020-12-02` into a date, not a string."""
    from datetime import date

    parsed = _event(issues=[_issue(published_on=date(2020, 12, 2))])
    assert parsed.issues[0].published_on == date(2020, 12, 2)


def test_availability_defaults_from_whether_a_digital_id_is_present() -> None:
    """The default follows from the data rather than needing to be restated —
    and a digital_id is the only thing that can make an issue linkable."""
    from models.types import Availability

    assert _event(issues=[_issue()]).issues[0].availability is Availability.UNCONFIRMED
    assert (
        _event(issues=[_issue(digital_id=55901)]).issues[0].availability
        is Availability.LINKABLE
    )


def test_duplicate_event_slugs_across_files_are_refused(tmp_path: Path) -> None:
    """Two files claiming one slug would silently overwrite each other at load."""
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(
            "slug: same-event\ndisplay_name: Same\ncore:\n  series: X\n  count: 1\n"
        )
    with pytest.raises(CurationError, match="duplicate event slugs"):
        load_all_events(tmp_path)


def test_an_empty_directory_loads_nothing(tmp_path: Path) -> None:
    assert load_all_events(tmp_path) == []


def test_an_empty_yaml_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("")
    with pytest.raises(CurationError, match="missing `slug`"):
        load_all_events(tmp_path)


def test_error_messages_name_the_source_file(tmp_path: Path) -> None:
    """Without the filename, a message like "role=x is not one of" is useless
    once there are several event files."""
    path = tmp_path / "king-in-black.yaml"
    path.write_text(
        "slug: kib\ndisplay_name: KiB\nissues:\n"
        "  - series: Test\n    number: 1\n    position: 1\n    role: sidekick\n"
    )
    with pytest.raises(CurationError, match="king-in-black.yaml"):
        load_all_events(tmp_path)


def test_ordered_and_core_helpers() -> None:
    event = _event(
        issues=[
            _issue(number=2, position=2, role="core"),
            _issue(number=1, position=1, role="core"),
            _issue(number=3, position=3),
        ]
    )
    assert [i.position for i in event.ordered] == [1, 2, 3]
    assert [i.issue_number for i in event.core_issues] == [1, 2]
    assert event.is_curated is False
    assert event.issues_by_key["test-3"].issue_number == 3
    assert event.issues[0].display == "Test #2"
    assert event.issues[0].series_slug == "test"
