"""Bookmarks: capture, per-user isolation, and sequencing.

The isolation test is the one that matters operationally — two people share this
deployment, which is the entire reason the principal is a User rather than a
client app.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from curation.resolve import candidates_from_guide, resolve
from marvel.links import assert_tappable
from models.bookmark import Bookmark
from models.types import BookmarkStatus
from service import bookmarks as bookmark_service
from service import guide as guide_service


async def _entry(session, ref: str):
    entries = await guide_service.all_entries(session)
    resolution = resolve(ref, candidates_from_guide(entries))
    assert resolution.resolved, f"{ref!r} did not resolve"
    return next(e for e in entries if e.key == resolution.matched.key)


async def _save(session, user, ref: str, **kwargs):
    entry = await _entry(session, ref)
    ref_id, provenance = await bookmark_service.provenance_for(session, entry)
    return await bookmark_service.create_bookmark(
        session,
        user_id=user.id,
        entry=entry,
        source_reference_id=ref_id,
        provenance=provenance,
        **kwargs,
    )


async def test_a_bookmark_denormalizes_everything_sequencing_needs(
    session, user, loaded_event
) -> None:
    """SPEC §4: ordering a reading list must need zero follow-up calls. If this
    record is too thin, sequencing starts making queries per card."""
    bookmark = await _save(session, user, "King in Black: Namor #1")

    assert bookmark.event_slug == "king-in-black"
    assert bookmark.event_name == "King in Black"
    assert bookmark.position is not None
    assert bookmark.role == "essential_tie_in"
    assert bookmark.narrative_role == "flashback"
    assert bookmark.franchise == "atlantean"
    assert bookmark.series_name == "King in Black: Namor"
    assert bookmark.issue_number == 1
    assert bookmark.published_on == date(2020, 12, 16)
    assert bookmark.availability == "unconfirmed"


async def test_provenance_comes_from_the_reference_graph(session, user, loaded_event) -> None:
    """"Why is this here?" answered automatically, without asking someone
    holding a book an extra question."""
    bookmark = await _save(session, user, "King in Black: Planet of the Symbiotes #1")
    assert bookmark.source_reference_id is not None
    assert bookmark.provenance == "Expanded in King in Black #3"


async def test_an_issue_with_several_footnotes_gets_no_invented_provenance(
    session, user, loaded_event
) -> None:
    """Guessing which of two footnotes prompted the save would be a made-up
    citation, which is worse than none."""
    entry = await _entry(session, "King in Black #1")
    from models.catalog import IssueReference

    session.add_all(
        [
            IssueReference(
                from_issue_id=(await _entry(session, "King in Black #3")).issue_id,
                to_issue_id=entry.issue_id,
                relation_type="references",
                note="second edge",
            )
        ]
    )
    await session.commit()
    ref_id, provenance = await bookmark_service.provenance_for(session, entry)
    assert ref_id is None
    assert provenance == ""


async def test_a_note_and_omnibus_page_survive(session, user, loaded_event) -> None:
    bookmark = await _save(
        session, user, "King in Black: Marauders #1", note="want more mutants", omnibus_page=214
    )
    assert bookmark.note == "want more mutants"
    assert bookmark.omnibus_page == 214


async def test_bookmarks_are_isolated_per_user(session, user, other_user, loaded_event) -> None:
    """Two people, one deployment. A reading list is per-person."""
    await _save(session, user, "King in Black: Namor #1")
    await _save(session, other_user, "King in Black: Marauders #1")

    mine = await bookmark_service.user_bookmarks(session, user.id)
    theirs = await bookmark_service.user_bookmarks(session, other_user.id)
    assert [b.series_name for b in mine] == ["King in Black: Namor"]
    assert [b.series_name for b in theirs] == ["King in Black: Marauders"]

    total = await session.scalar(select(Bookmark).where(Bookmark.user_id == user.id))
    assert total.user_id == user.id


async def test_marking_read_is_scoped_to_the_owner(
    session, user, other_user, loaded_event
) -> None:
    bookmark = await _save(session, user, "King in Black: Namor #1")
    assert await bookmark_service.mark_read(session, other_user.id, bookmark.id) is None
    assert await bookmark_service.mark_read(session, user.id, bookmark.id) is not None


async def test_sequence_bookmarks_clusters_rather_than_sorts(
    session, user, loaded_event
) -> None:
    for ref in [
        "King in Black: Namor #1",
        "Web of Venom: Empyre's End #1",
        "Venom #34",
        "King in Black: Marauders #1",
    ]:
        await _save(session, user, ref)

    payload = await bookmark_service.sequence_bookmarks(session, user.id)
    assert payload["ordering"] == "clustered"
    headers = [g["group"] for g in payload["groups"]]
    # Prelude first, epilogue last, regardless of when anything published.
    assert headers[0] == "Before it starts"
    assert headers[-1] == "After the dust settles"
    for group in payload["groups"]:
        assert group["why_grouped"]


async def test_the_in_universe_anchor_is_computed_at_write_time(
    session, user, loaded_event
) -> None:
    """"this happens around King in Black #3" — derived from curated order, not
    from a date, and stored so sequencing needs no extra query."""
    await _save(session, user, "King in Black: Planet of the Symbiotes #1")
    items = await bookmark_service.seq_items(session, user.id)
    assert items[0].anchor == "around King in Black #3"


async def test_chronological_is_available_but_not_the_default(
    session, user, loaded_event
) -> None:
    await _save(session, user, "Venom #34")
    await _save(session, user, "Web of Venom: Empyre's End #1")

    clustered = await bookmark_service.sequence_bookmarks(session, user.id)
    chrono = await bookmark_service.sequence_bookmarks(
        session, user.id, ordering="chronological"
    )
    assert clustered["ordering"] == "clustered"
    assert chrono["ordering"] == "chronological"
    assert [i["issue"] for i in chrono["issues"]] == [
        "Web of Venom: Empyre's End #1", "Venom #34"
    ]


async def test_an_empty_rack_says_what_to_do(session, user, loaded_event) -> None:
    payload = await bookmark_service.sequence_bookmarks(session, user.id)
    assert payload["count"] == 0
    assert "bookmark_issue" in payload["note"]


async def test_every_bookmark_link_obeys_the_gate_a_rules(
    session, user, loaded_event
) -> None:
    await _save(session, user, "King in Black: Namor #1")
    payload = await bookmark_service.sequence_bookmarks(session, user.id)
    for group in payload["groups"]:
        for issue in group["issues"]:
            assert_tappable(issue["link"])


async def test_a_pending_entry_is_never_linkable(session, user, loaded_event) -> None:
    """Gate B: an unconfirmed match must not become a tappable link."""
    entry = await _entry(session, "King in Black #1")
    bookmark = await bookmark_service.create_bookmark(
        session,
        user_id=user.id,
        entry=entry,
        status=BookmarkStatus.PENDING,
        raw_text="king in black one maybe",
    )
    bookmark.digital_id = 55901  # even with an id present
    await session.commit()
    assert "http" not in bookmark_service.bookmark_link(bookmark)
    assert bookmark_service.bookmark_link(bookmark) == "unconfirmed — not linked"
