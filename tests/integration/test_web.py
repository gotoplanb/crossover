"""The web surface. Server-rendered, no SPA — so the assertions are on HTML.

SPEC §7 calls the rack the centerpiece and names three things it must do:
covers, grouping with headers rather than a flat list, and provenance on every
card. Those are the tests worth having.
"""

from __future__ import annotations

from curation.resolve import candidates_from_guide, resolve
from marvel.sync import apply_record, promote_availability
from service import bookmarks as bookmark_service
from service import guide as guide_service


async def _save(session, user, ref: str):
    entries = await guide_service.all_entries(session)
    resolution = resolve(ref, candidates_from_guide(entries))
    assert resolution.resolved
    entry = next(e for e in entries if e.key == resolution.matched.key)
    ref_id, provenance = await bookmark_service.provenance_for(session, entry)
    return await bookmark_service.create_bookmark(
        session, user_id=user.id, entry=entry,
        source_reference_id=ref_id, provenance=provenance,
    )


async def test_healthz_reports_the_two_facts_that_explain_a_bad_deploy(client) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert "marvel_credentials" in body
    assert body["one_tap_links"] is False


async def test_the_root_lands_on_the_rack(client) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/ui/rack"


async def test_the_rack_requires_signing_in(client) -> None:
    response = await client.get("/ui/rack", follow_redirects=False)
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_an_empty_rack_tells_you_how_to_fill_it(signed_in, loaded_event) -> None:
    html = (await signed_in.get("/ui/rack")).text
    assert "Nothing saved yet" in html
    assert "bookmark the Namor one" in html


async def test_the_rack_groups_with_headers_not_a_flat_list(
    signed_in, session, user, loaded_event
) -> None:
    await _save(session, user, "Web of Venom: Empyre's End #1")
    await _save(session, user, "Venom #34")
    html = (await signed_in.get("/ui/rack")).text

    assert "Before it starts" in html
    assert "After the dust settles" in html
    # Group headers carry their reason: a batch you don't understand is just a
    # shorter list.
    assert "Read before anything else." in html


async def test_every_card_shows_why_it_is_there(
    signed_in, session, user, loaded_event
) -> None:
    """Provenance is where the reference graph pays off visibly (SPEC §7)."""
    await _save(session, user, "King in Black: Planet of the Symbiotes #1")
    html = (await signed_in.get("/ui/rack")).text
    assert "Expanded in King in Black #3" in html


async def test_covers_render_from_marvel_art_once_synced(
    signed_in, session, user, loaded_event, record_index
) -> None:
    from sqlalchemy import select

    from models.catalog import Issue

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    promote_availability(issue)
    await session.commit()
    await _save(session, user, "King in Black #1")

    html = (await signed_in.get("/ui/rack")).text
    assert "portrait_incredible.jpg" in html
    # And it is tappable through to Marvel Unlimited.
    assert "https://read.marvel.com/#/book/55901" in html


async def test_an_unlinkable_card_says_so_rather_than_faking_a_link(
    signed_in, session, user, loaded_event
) -> None:
    await _save(session, user, "King in Black #1")
    html = (await signed_in.get("/ui/rack")).text
    assert "not on Marvel Unlimited" in html
    assert "read.marvel.com" not in html


async def test_read_state_toggles(signed_in, session, user, loaded_event) -> None:
    bookmark = await _save(session, user, "King in Black: Namor #1")
    first = await signed_in.post(f"/ui/rack/{bookmark.id}/read")
    assert "Read" in first.text
    second = await signed_in.post(f"/ui/rack/{bookmark.id}/read")
    assert "Mark read" in second.text


async def test_another_users_bookmark_cannot_be_toggled(
    signed_in, session, other_user, loaded_event
) -> None:
    bookmark = await _save(session, other_user, "King in Black: Namor #1")
    assert (await signed_in.post(f"/ui/rack/{bookmark.id}/read")).status_code == 404


async def test_pending_shelf_entries_surface_for_confirmation(
    signed_in, session, user, loaded_event
) -> None:
    from models.types import ShelfSource
    from service import shelf as shelf_service

    await shelf_service.propose(
        session, user_id=user.id, candidates=["King in Black: Namor #1"],
        source=ShelfSource.PHOTO,
    )
    html = (await signed_in.get("/ui/rack")).text
    assert "Needs confirming" in html
    assert "King in Black: Namor #1" in html


async def test_the_event_browser_shows_curation_depth(signed_in, loaded_event) -> None:
    html = (await signed_in.get("/ui/events")).text
    assert "King in Black" in html
    assert "best effort" in html
    assert "40 issues" in html


async def test_the_event_guide_shows_order_and_roles(signed_in, loaded_event) -> None:
    html = (await signed_in.get("/ui/events/king-in-black")).text
    assert "core" in html
    assert "essential tie in" in html
    # The best-effort warning explains what "not on Marvel Unlimited" means here.
    assert "unverified, not necessarily absent" in html


async def test_an_unknown_event_is_a_404(signed_in) -> None:
    assert (await signed_in.get("/ui/events/secret-wars")).status_code == 404


async def test_marvel_attribution_appears_on_every_page(signed_in, loaded_event) -> None:
    """Their terms require it wherever their data or art is displayed."""
    for path in ("/ui/rack", "/ui/events", "/ui/events/king-in-black"):
        assert "MARVEL" in (await signed_in.get(path)).text, path


# --- curation admin ---


async def test_curation_view_requires_the_admin_cookie(client, loaded_event) -> None:
    response = await client.get("/ui/curate/king-in-black", follow_redirects=False)
    assert response.status_code == 303


async def test_curation_view_warns_that_edits_are_not_persistent(
    signed_in, loaded_event
) -> None:
    """Heroku's filesystem is ephemeral, so the export is the load-bearing part.
    A curator must not discover that by losing an afternoon's work."""
    html = (await signed_in.get("/ui/curate/king-in-black")).text
    assert "export the YAML" in html
    assert "lost on the next deploy" in html


async def test_moving_an_issue_keeps_positions_contiguous(
    signed_in, session, loaded_event
) -> None:
    """A swap, not a re-index, so the SPEC §8 contiguity gate can never be
    broken by fiddling in the UI."""
    _, before = await guide_service.event_entries(session, "king-in-black")
    second = before[1]

    await signed_in.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": second.key, "direction": "up"},
        follow_redirects=False,
    )
    _, after = await guide_service.event_entries(session, "king-in-black")
    assert [e.position for e in after] == list(range(1, len(after) + 1))
    assert after[0].key == second.key


async def test_a_reference_edge_can_be_added_by_hand(
    signed_in, session, loaded_event
) -> None:
    await signed_in.post(
        "/ui/curate/king-in-black/reference",
        data={
            "from_key": "king-in-black-1",
            "to_key": "king-in-black-marauders-1",
            "relation_type": "references",
            "note": "read off page 88 of the omnibus",
            "omnibus_page": "88",
        },
        follow_redirects=False,
    )
    html = (await signed_in.get("/ui/curate/king-in-black")).text
    assert "read off page 88 of the omnibus" in html
    assert "(p. 88)" in html


async def test_the_yaml_export_round_trips_through_the_loader(
    signed_in, session, loaded_event
) -> None:
    """The export is only useful if what comes out can go back in — and still
    pass the data-quality gates."""
    import yaml

    from curation.schema import parse_event
    from curation.validate import problems

    body = (await signed_in.get("/ui/curate/king-in-black/export.yaml")).text
    reparsed = parse_event(yaml.safe_load(body))

    assert reparsed.slug == "king-in-black"
    assert len(reparsed.issues) == 40
    assert len(reparsed.references) == 15
    assert problems(reparsed, record_index=None) == []


# --- OAuth discovery, which Claude's connector flow reads first ---


async def test_oauth_discovery_endpoints_are_public(client) -> None:
    server = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert server["code_challenge_methods_supported"] == ["S256"]
    resource = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert resource["resource"].endswith("/mcp")


async def test_an_unknown_client_never_redirects(client) -> None:
    """An open redirect to an unregistered URI would be the bug here."""
    response = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "xoc_nope",
            "redirect_uri": "https://evil.example/cb",
            "code_challenge": "abc",
        },
    )
    assert response.status_code == 400
    assert "Unknown client_id" in response.text


async def test_reordering_survives_whatever_row_ids_happen_to_be(
    signed_in, session, loaded_event
) -> None:
    """Guards a real bug: the swap left both position updates pending in one
    flush, and SQLAlchemy orders UPDATEs by primary key rather than by
    dependency — so whether the pair collided on the unique (event, position)
    constraint depended on which row happened to have the lower id. Reordering
    failed intermittently with a 500.

    Walking one issue the length of the list exercises both orderings many
    times over.
    """
    from service import guide as guide_service

    _, before = await guide_service.event_entries(session, "king-in-black")
    target = before[0].key

    for _ in range(6):
        response = await signed_in.post(
            "/ui/curate/king-in-black/move",
            data={"issue_key": target, "direction": "down"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    _, after = await guide_service.event_entries(session, "king-in-black")
    # Still a dense 1..N sequence, and the issue actually moved.
    assert [e.position for e in after] == list(range(1, len(after) + 1))
    assert [e.key for e in after].index(target) == 6

    # And back up again, which exercises the opposite direction.
    for _ in range(6):
        assert (
            await signed_in.post(
                "/ui/curate/king-in-black/move",
                data={"issue_key": target, "direction": "up"},
                follow_redirects=False,
            )
        ).status_code == 303

    _, restored = await guide_service.event_entries(session, "king-in-black")
    assert [e.key for e in restored] == [e.key for e in before]
