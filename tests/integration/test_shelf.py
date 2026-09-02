"""`add_to_shelf` — the two-phase capture flow.

The whole design rests on one observation (SPEC §6): a blurry spine or a variant
cover matches the wrong record easily, so confirmation happens out loud in the
shop, not in a queue cleared later. These tests pin the "nothing is committed
until someone says yes" property.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import func, select

from marvel.client import BASE_URL, MarvelClient
from marvel.mirror import DEFAULT_BASE_URL, MirrorClient
from models.bookmark import Bookmark, ShelfCandidate
from models.catalog import Issue
from models.types import Availability, BookmarkStatus, ShelfSource
from service import shelf as shelf_service


async def test_phase_one_stores_nothing_confirmed(session, user, loaded_event) -> None:
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black: Namor #1"],
        source=ShelfSource.TYPED,
    )
    assert result["phase"] == "propose"
    assert result["results"][0]["matches"]

    # Scoped to this user: every query in the real code paths is scoped the
    # same way (bookmarks are per-person), and an unscoped test would pass or
    # fail depending on what else happens to be in the dev database.
    confirmed = await session.scalar(
        select(func.count())
        .select_from(Bookmark)
        .where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.CONFIRMED.value,
        )
    )
    assert confirmed == 0


async def test_phase_one_returns_enough_to_identify_out_loud(session, user, loaded_event) -> None:
    """ "I think that's Venom #87 — right?" needs series, number and cover date;
    the cover art is what someone holding the book can actually check."""
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["namor 1"],
        source=ShelfSource.PHOTO,
    )
    match = result["results"][0]["matches"][0]
    for field in ("issue", "series", "number", "cover_date", "cover_thumbnail"):
        assert field in match, f"{field} missing — cannot be confirmed verbally"
    assert match["issue"] == "King in Black: Namor #1"


async def test_an_unanswered_candidate_stays_pending_with_the_raw_text(
    session, user, loaded_event
) -> None:
    """The fallback path: if nobody answers, the capture is still not lost."""
    await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["that venom one with the red cover"],
        source=ShelfSource.PHOTO,
    )
    pending = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.PENDING.value,
        )
    )
    assert pending is not None
    assert pending.raw_text == "that venom one with the red cover"
    assert pending.issue_id is None

    on_rack = await shelf_service.pending_for_user(session, user.id)
    assert on_rack[0]["raw_text"] == "that venom one with the red cover"


async def test_phase_two_commits_and_clears_the_pending_row(session, user, loaded_event) -> None:
    proposed = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black: Namor #1"],
        source=ShelfSource.TYPED,
    )
    entry = proposed["results"][0]

    result = await shelf_service.confirm(
        session,
        user_id=user.id,
        candidate_id=entry["candidate_id"],
        chosen_key="king-in-black-namor-1",
    )
    assert result["saved"] == "King in Black: Namor #1"

    pending = await session.scalar(
        select(func.count())
        .select_from(Bookmark)
        .where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.PENDING.value,
        )
    )
    assert pending == 0
    confirmed = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.CONFIRMED.value,
        )
    )
    assert confirmed.series_name == "King in Black: Namor"
    assert "Seen in the wild (typed)" in confirmed.provenance

    candidate = await session.get(ShelfCandidate, __import__("uuid").UUID(entry["candidate_id"]))
    assert candidate.resolved_bookmark_id == confirmed.id


async def test_confirming_a_key_that_was_never_offered_is_refused(
    session, user, loaded_event
) -> None:
    """Gate B, at the commit boundary. An id nobody confirmed against a Marvel
    record must not be committable by passing it in directly."""
    proposed = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black: Namor #1"],
        source=ShelfSource.TYPED,
    )
    with pytest.raises(ValueError, match="not one of the options offered"):
        await shelf_service.confirm(
            session,
            user_id=user.id,
            candidate_id=proposed["results"][0]["candidate_id"],
            chosen_key="king-in-black-5",
        )


async def test_another_users_candidate_cannot_be_confirmed(
    session, user, other_user, loaded_event
) -> None:
    proposed = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black: Namor #1"],
        source=ShelfSource.TYPED,
    )
    with pytest.raises(ValueError, match="no pending shelf candidate"):
        await shelf_service.confirm(
            session,
            user_id=other_user.id,
            candidate_id=proposed["results"][0]["candidate_id"],
            chosen_key="king-in-black-namor-1",
        )


@respx.mock
async def test_an_off_event_find_resolves_against_the_marvel_api(
    session, user, loaded_event, event_comics_payload
) -> None:
    """A comic-shop find may belong to no curated event at all."""
    respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Fantastic Four 52"],
            source=ShelfSource.PHOTO,
            client=client,
        )
    matches = result["results"][0]["matches"]
    assert matches, "the Marvel search should supply candidates the curated set cannot"
    assert all(m["source"] == "marvel_api" for m in matches)


@respx.mock
async def test_a_marvel_outage_degrades_to_pending_rather_than_losing_the_capture(
    session, user, loaded_event
) -> None:
    respx.get(f"{BASE_URL}/comics").mock(return_value=httpx.Response(500, json={}))
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Something Unknown 4"],
            source=ShelfSource.PHOTO,
            client=client,
        )
    assert result["results"][0]["matches"] == []
    pending = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.PENDING.value,
        )
    )
    assert pending.raw_text == "Something Unknown 4"


async def test_no_marvel_key_still_resolves_the_curated_set(session, user, loaded_event) -> None:
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black #5"],
        source=ShelfSource.TYPED,
        client=MarvelClient(None, None),
    )
    assert result["results"][0]["matches"][0]["key"] == "king-in-black-5"


async def test_several_candidates_from_one_photo(session, user, loaded_event) -> None:
    """A shelf photo can yield several issues at once."""
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black #5", "King in Black: Marauders #1", "venom 34"],
        source=ShelfSource.PHOTO,
    )
    assert len(result["results"]) == 3
    assert all(r["matches"] for r in result["results"])


@respx.mock
async def test_a_title_with_no_trailing_number_searches_the_whole_series(
    session, user, loaded_event
) -> None:
    """ "the new Venom" has no issue number, so every issue of the series is a
    legitimate candidate and none may be filtered out."""
    payload = {
        "data": {
            "total": 2,
            "results": [
                {
                    "id": 1,
                    "digitalId": 100,
                    "title": "Venom (2018) #31",
                    "issueNumber": 31,
                    "series": {"name": "Venom (2018 - 2021)"},
                    "urls": [],
                    "thumbnail": {},
                },
                {
                    "id": 2,
                    "digitalId": 101,
                    "title": "Venom (2018) #32",
                    "issueNumber": 32,
                    "series": {"name": "Venom (2018 - 2021)"},
                    "urls": [],
                    "thumbnail": {},
                },
            ],
        }
    }
    route = respx.get(f"{BASE_URL}/comics").mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as http:
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Some Unknown Series"],
            source=ShelfSource.TYPED,
            client=MarvelClient("pub", "priv", client=http),
        )
    # The whole string is the series query — nothing was stripped as a number.
    assert dict(route.calls.last.request.url.params)["titleStartsWith"] == ("Some Unknown Series")
    # With no issue number given, every issue of the series stays a candidate.
    matches = result["results"][0]["matches"]
    api_matches = [m for m in matches if m["source"] == "marvel_api"]
    assert [m["key"] for m in api_matches] == ["venom-31", "venom-32"]


# --- the live mirror, for finds outside the curated set ----------------------
#
# Marvel's API is gone, so without this path a comic picked up in a shop that
# belongs to no curated event resolves to nothing and stays pending forever.


def _mirror_hit(issue_id: int, series: str, number: str, series_id: int = 1) -> dict:
    return {
        "id": issue_id,
        "title": f"{series} #{number}",
        "issueNumber": number,
        "seriesId": series_id,
        "seriesName": series,
    }


def _mirror_detail(issue_id: int, series: str, number: str, digital: int) -> dict:
    return {
        "id": issue_id,
        "digitalId": digital,
        "title": f"{series} #{number}",
        "issueNumber": number,
        "seriesName": series,
        "onSaleDate": "1966-07-01T00:00:00-0500",
        "detailUrl": f"https://www.marvel.com/comics/issue/{issue_id}/x",
        "cover": {"path": "http://i.annihil.us/u/prod/marvel/i/mg/1/2/abc", "extension": "jpg"},
    }


@respx.mock
async def test_the_mirror_resolves_a_find_the_curated_set_cannot(
    session, user, loaded_event
) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(
            200, json={"items": [_mirror_hit(8073, "Daredevil (1964)", "181")]}
        )
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{DEFAULT_BASE_URL}/issues/8073").mock(
        return_value=httpx.Response(200, json=_mirror_detail(8073, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Daredevil 181"],
            source=ShelfSource.PHOTO,
            mirror=MirrorClient(client=http),
        )
    matches = result["results"][0]["matches"]
    assert matches, "the mirror should supply what the curated roster cannot"
    assert matches[0]["source"] == shelf_service.MIRROR_SOURCE
    assert matches[0]["cover_thumbnail"], "phase 1 needs art to confirm against out loud"
    assert matches[0]["digital_id"] == 1672


@respx.mock
async def test_a_confirmed_mirror_find_records_where_its_id_came_from(
    session, user, loaded_event
) -> None:
    """Gate B's recording half. An id is linkable only if the row can say where
    it came from — otherwise "verified" is a claim in a commit message rather
    than a property of the data."""
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(
            200, json={"items": [_mirror_hit(8073, "Daredevil (1964)", "181")]}
        )
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{DEFAULT_BASE_URL}/issues/8073").mock(
        return_value=httpx.Response(200, json=_mirror_detail(8073, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        proposed = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Daredevil 181"],
            source=ShelfSource.PHOTO,
            mirror=MirrorClient(client=http),
        )
    entry = proposed["results"][0]
    await shelf_service.confirm(
        session,
        user_id=user.id,
        candidate_id=entry["candidate_id"],
        chosen_key=entry["matches"][0]["key"],
    )
    issue = await session.scalar(select(Issue).where(Issue.key == entry["matches"][0]["key"]))
    assert issue is not None
    assert issue.digital_id == 1672
    assert issue.digital_id_source == shelf_service.MIRROR_SOURCE
    assert issue.availability == Availability.LINKABLE.value


@respx.mock
async def test_a_mirror_outage_degrades_to_pending(session, user, loaded_event) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Something Unknown 4"],
            source=ShelfSource.PHOTO,
            mirror=MirrorClient(client=http),
        )
    assert result["results"][0]["matches"] == []
    pending = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.status == BookmarkStatus.PENDING.value,
            Bookmark.raw_text == "Something Unknown 4",
        )
    )
    assert pending is not None, "the capture must survive the mirror being down"


@respx.mock
async def test_mirror_options_sit_alongside_a_curated_hit(session, user, loaded_event) -> None:
    """A single curated match is not proof curation was right: the resolver
    returns its nearest entry even for text naming nothing in the roster. So the
    mirror tops the list up instead of deferring, and the person confirming sees
    the alternatives."""
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(
            200, json={"items": [_mirror_hit(8073, "Daredevil (1964)", "181")]}
        )
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{DEFAULT_BASE_URL}/issues/8073").mock(
        return_value=httpx.Response(200, json=_mirror_detail(8073, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        result = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["King in Black: Namor #1"],
            source=ShelfSource.TYPED,
            mirror=MirrorClient(client=http),
        )
    sources = [m["source"] for m in result["results"][0]["matches"]]
    assert sources[0] == "curated", "the curated hit stays first"
    assert shelf_service.MIRROR_SOURCE in sources
    assert len(sources) <= shelf_service.MAX_MATCHES


async def test_propose_without_a_mirror_still_works(session, user, loaded_event) -> None:
    """The mirror is optional everywhere; nothing may hard-depend on it."""
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["Utterly Unknown 1"],
        source=ShelfSource.TYPED,
        mirror=None,
    )
    assert result["results"][0]["matches"] == []
