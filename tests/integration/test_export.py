"""Exporting DB curation state back to YAML.

The admin view edits the database, but the git-tracked YAML is the source of
truth and Heroku's filesystem is ephemeral — so this is how work done in the
browser gets back into the repo. It is only useful if the output round-trips
*and* still passes the §8 gates.
"""

from __future__ import annotations

import yaml
from sqlalchemy import select

from curation.export import event_slugs, export_event_yaml
from curation.schema import parse_event
from curation.validate import problems
from marvel.sync import apply_record, promote_availability
from models.catalog import Issue


async def test_export_round_trips_through_the_parser(session, loaded_event) -> None:
    body = await export_event_yaml(session, "king-in-black")
    reparsed = parse_event(yaml.safe_load(body))

    assert reparsed.slug == "king-in-black"
    assert reparsed.display_name == "King in Black"
    assert len(reparsed.issues) == len(loaded_event.issues)
    assert len(reparsed.references) == len(loaded_event.references)
    assert reparsed.core_series == "King in Black"
    assert reparsed.core_count == 5


async def test_exported_yaml_passes_every_data_quality_gate(session, loaded_event) -> None:
    """An export that couldn't be committed would be worthless."""
    body = await export_event_yaml(session, "king-in-black")
    assert problems(parse_event(yaml.safe_load(body)), record_index=None) == []


async def test_the_order_and_annotations_survive(session, loaded_event) -> None:
    body = await export_event_yaml(session, "king-in-black")
    reparsed = parse_event(yaml.safe_load(body))
    original = {i.key: i for i in loaded_event.ordered}

    for issue in reparsed.ordered:
        source = original[issue.key]
        assert issue.position == source.position
        assert issue.role is source.role
        assert issue.narrative_role is source.narrative_role
        assert issue.franchise is source.franchise
        assert issue.note == source.note


async def test_reference_notes_and_direction_survive(session, loaded_event) -> None:
    """The footnote layer is the hand-authored part; losing a note in export
    would quietly destroy the work it exists to preserve."""
    body = await export_event_yaml(session, "king-in-black")
    exported = {
        (r.from_key, r.to_key): r for r in parse_event(yaml.safe_load(body)).references
    }
    for ref in loaded_event.references:
        match = exported[(ref.from_key, ref.to_key)]
        assert match.relation_type is ref.relation_type
        assert match.note == ref.note


async def test_a_synced_digital_id_is_exported_and_still_traceable(
    session, loaded_event, record_index
) -> None:
    """Gate B survives the round trip: an exported id is re-verified against the
    cache when the file is loaded back in."""
    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    promote_availability(issue)
    await session.commit()

    body = await export_event_yaml(session, "king-in-black")
    assert "digital_id: 55901" in body
    assert "availability: linkable" in body

    reparsed = parse_event(yaml.safe_load(body))
    assert problems(reparsed, record_index) == []
    # And with no cache to check against, the same file is refused.
    assert problems(reparsed, record_index=None)


async def test_quotes_in_a_series_name_survive(session, loaded_event) -> None:
    """"Web of Venom: Empyre's End" has an apostrophe; a naive emitter breaks
    the file on it."""
    body = await export_event_yaml(session, "king-in-black")
    reparsed = parse_event(yaml.safe_load(body))
    assert any(
        i.series_name == "Web of Venom: Empyre's End" for i in reparsed.issues
    )


async def test_an_unknown_event_raises(session) -> None:
    import pytest

    with pytest.raises(ValueError, match="no event with slug"):
        await export_event_yaml(session, "secret-wars")


async def test_event_slugs_lists_what_can_be_exported(session, loaded_event) -> None:
    assert await event_slugs(session) == ["king-in-black"]


async def test_exporting_an_event_with_no_dates_or_summary(session, loaded_event) -> None:
    """A newly-started event file has neither yet; the export must stay valid
    YAML rather than emitting empty keys."""
    import yaml
    from sqlalchemy import select

    from models.catalog import Event

    event = await session.scalar(select(Event).where(Event.slug == "king-in-black"))
    event.started_on = None
    event.ended_on = None
    event.summary = ""
    await session.commit()

    body = await export_event_yaml(session, "king-in-black")
    assert "started_on:" not in body
    assert "ended_on:" not in body
    assert "summary:" not in body

    reparsed = parse_event(yaml.safe_load(body))
    assert reparsed.started_on is None
    assert reparsed.summary == ""
    assert len(reparsed.issues) == 40


async def test_exporting_an_event_with_no_marvel_id_emits_null(
    session, loaded_event
) -> None:
    """`marvel_event_id: null` has to round-trip, because a wrong id would sync
    an entirely wrong roster and an absent one is the honest state."""
    import yaml

    body = await export_event_yaml(session, "king-in-black")
    assert "marvel_event_id: null" in body
    assert parse_event(yaml.safe_load(body)).marvel_event_id is None
