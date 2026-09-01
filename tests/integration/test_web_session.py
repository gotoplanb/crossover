"""The session and authorization paths of the web surface.

Two people share this deployment, so "which reader am I" is not cosmetic — it
decides whose rack you see and whose bookmarks you can change. These are the
tests for that boundary, plus the login/logout flow itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from auth import ADMIN_COOKIE
from config.settings import get_settings
from models.bookmark import Bookmark
from models.types import ShelfSource
from routes.ui import USER_COOKIE
from service import shelf as shelf_service


@pytest.fixture
def admin_key() -> str:
    return get_settings().admin_key


@pytest.fixture
def reader_password(user, monkeypatch) -> str:
    """Give the fixture reader a password, the way a deployment would."""
    password = "fixture-reader-password"  # pragma: allowlist secret
    monkeypatch.setenv(f"CROSSOVER_PASSWORD_{user.handle.upper()}", password)
    get_settings.cache_clear()
    yield password
    get_settings.cache_clear()


# --- login ---


async def test_the_login_form_lists_active_readers(client, user) -> None:
    html = (await client.get("/ui/login")).text
    assert user.handle in html
    assert "Password" in html


async def test_the_login_form_explains_an_empty_allowlist(client, session) -> None:
    """A fresh deploy with no readers should say what to run, not present an
    empty dropdown with no explanation."""
    from sqlalchemy import delete

    from models.user import User

    await session.execute(delete(User))
    await session.commit()

    html = (await client.get("/ui/login")).text
    assert "<option" not in html
    assert "make seed" in html


async def test_signing_in_sets_both_cookies_and_redirects(
    client, user, reader_password
) -> None:
    response = await client.post(
        "/ui/login",
        data={"handle": user.handle, "password": reader_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/rack"
    cookies = response.headers.get_list("set-cookie")
    assert any(ADMIN_COOKIE in c for c in cookies)
    assert any(USER_COOKIE in c for c in cookies)
    # The admin cookie must not be readable by page scripts.
    assert all("HttpOnly" in c for c in cookies if ADMIN_COOKIE in c)


async def test_signing_in_honors_the_next_url(client, user, reader_password) -> None:
    """So the OAuth consent bounce lands back where it started."""
    response = await client.post(
        "/ui/login",
        data={"handle": user.handle, "password": reader_password, "next": "/ui/events"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/ui/events"


async def test_a_wrong_password_is_401_and_re_renders_the_form(
    client, user, reader_password
) -> None:
    response = await client.post(
        "/ui/login", data={"handle": user.handle, "password": "wrong"}
    )
    assert response.status_code == 401
    assert "Wrong password, or no such reader." in response.text


async def test_an_unknown_handle_is_401(client) -> None:
    """Deliberately the same message as a wrong password — distinguishing them
    would turn the form into a reader-enumeration oracle."""
    response = await client.post(
        "/ui/login", data={"handle": "nobody", "password": "whatever"}
    )
    assert response.status_code == 401
    assert "Wrong password, or no such reader." in response.text


async def test_a_deactivated_reader_cannot_sign_in(
    client, session, user, reader_password
) -> None:
    user.is_active = False
    await session.commit()
    response = await client.post(
        "/ui/login", data={"handle": user.handle, "password": reader_password}
    )
    assert response.status_code == 401


async def test_signing_out_clears_both_cookies(client, user, reader_password) -> None:
    await client.post(
        "/ui/login", data={"handle": user.handle, "password": reader_password}
    )
    response = await client.post("/ui/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    cleared = response.headers.get_list("set-cookie")
    assert any(ADMIN_COOKIE in c and 'Max-Age=0' in c or 'expires=' in c.lower()
               for c in cleared)


# --- the user cookie itself ---


async def test_a_garbage_user_cookie_bounces_to_login(client) -> None:
    """The cookie is attacker-controlled; a non-UUID must not raise a 500."""
    client.cookies.set(USER_COOKIE, "not-a-uuid")
    response = await client.get("/ui/rack", follow_redirects=False)
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_a_cookie_for_a_nonexistent_user_bounces_to_login(client) -> None:
    from uuid import uuid4

    client.cookies.set(USER_COOKIE, str(uuid4()))
    response = await client.get("/ui/rack", follow_redirects=False)
    assert response.status_code == 303


async def test_a_cookie_for_a_deactivated_reader_bounces_to_login(
    client, session, user
) -> None:
    """Deactivating a reader has to take effect on the web surface too, not just
    on their OAuth tokens."""
    user.is_active = False
    await session.commit()
    client.cookies.set(USER_COOKIE, str(user.id))
    response = await client.get("/ui/rack", follow_redirects=False)
    assert response.status_code == 303


# --- authorization on the write paths ---


async def test_toggling_read_requires_a_session(client) -> None:
    from uuid import uuid4

    response = await client.post(f"/ui/rack/{uuid4()}/read", follow_redirects=False)
    assert response.status_code == 303


async def test_toggling_a_nonexistent_bookmark_is_404(client, user) -> None:
    from uuid import uuid4

    client.cookies.set(USER_COOKIE, str(user.id))
    assert (await client.post(f"/ui/rack/{uuid4()}/read")).status_code == 404


async def test_confirming_a_shelf_candidate_requires_a_session(client) -> None:
    response = await client.post(
        "/ui/rack/confirm",
        data={"candidate_id": "x", "chosen_key": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_confirming_from_the_rack_commits_the_entry(
    client, session, user, loaded_event
) -> None:
    """The fallback path: confirmation was meant to happen out loud in the shop."""
    proposed = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["King in Black: Namor #1"],
        source=ShelfSource.PHOTO,
    )
    candidate_id = proposed["results"][0]["candidate_id"]

    client.cookies.set(USER_COOKIE, str(user.id))
    response = await client.post(
        "/ui/rack/confirm",
        data={"candidate_id": candidate_id, "chosen_key": "king-in-black-namor-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id, Bookmark.status == "confirmed"
        )
    )
    assert saved.series_name == "King in Black: Namor"


async def test_confirming_a_stale_candidate_just_re_renders(client, user) -> None:
    """A double-submitted form must not produce a 500 — the rack shows the truth
    either way."""
    from uuid import uuid4

    client.cookies.set(USER_COOKIE, str(user.id))
    response = await client.post(
        "/ui/rack/confirm",
        data={"candidate_id": str(uuid4()), "chosen_key": "king-in-black-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def test_confirming_with_a_malformed_candidate_id_does_not_500(
    client, user
) -> None:
    client.cookies.set(USER_COOKIE, str(user.id))
    response = await client.post(
        "/ui/rack/confirm",
        data={"candidate_id": "not-a-uuid", "chosen_key": "king-in-black-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- curation admin authorization ---


async def test_curation_writes_require_the_admin_cookie(client, loaded_event) -> None:
    """Reading the rack needs a reader cookie; changing curated order needs the
    admin key as well."""
    for path, data in [
        ("/ui/curate/king-in-black/move", {"issue_key": "king-in-black-1",
                                          "direction": "up"}),
        (
            "/ui/curate/king-in-black/reference",
            {"from_key": "king-in-black-1", "to_key": "king-in-black-2",
             "relation_type": "references"},
        ),
    ]:
        response = await client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303, path
        assert "/ui/login" in response.headers["location"]


async def test_the_yaml_export_requires_the_admin_cookie(client, loaded_event) -> None:
    response = await client.get(
        "/ui/curate/king-in-black/export.yaml", follow_redirects=False
    )
    assert response.status_code == 303


async def test_exporting_an_unknown_event_is_404(client, admin_key) -> None:
    client.cookies.set(ADMIN_COOKIE, admin_key)
    response = await client.get("/ui/curate/nope/export.yaml")
    assert response.status_code == 404


async def test_curating_an_unknown_event_is_404(client, admin_key) -> None:
    client.cookies.set(ADMIN_COOKIE, admin_key)
    assert (await client.get("/ui/curate/nope")).status_code == 404


async def test_moving_an_unknown_issue_is_a_no_op(client, admin_key, loaded_event) -> None:
    client.cookies.set(ADMIN_COOKIE, admin_key)
    response = await client.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": "no-such-issue-1", "direction": "up"},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def test_moving_the_first_issue_up_is_a_no_op(
    client, admin_key, session, loaded_event
) -> None:
    """There is no position 0, and the swap must not create one."""
    from service import guide as guide_service

    client.cookies.set(ADMIN_COOKIE, admin_key)
    _, before = await guide_service.event_entries(session, "king-in-black")
    await client.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": before[0].key, "direction": "up"},
        follow_redirects=False,
    )
    _, after = await guide_service.event_entries(session, "king-in-black")
    assert [e.key for e in after] == [e.key for e in before]
    assert [e.position for e in after] == list(range(1, len(after) + 1))


async def test_moving_the_last_issue_down_is_a_no_op(
    client, admin_key, session, loaded_event
) -> None:
    from service import guide as guide_service

    client.cookies.set(ADMIN_COOKIE, admin_key)
    _, before = await guide_service.event_entries(session, "king-in-black")
    await client.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": before[-1].key, "direction": "down"},
        follow_redirects=False,
    )
    _, after = await guide_service.event_entries(session, "king-in-black")
    assert [e.key for e in after] == [e.key for e in before]


async def test_a_self_referencing_edge_is_refused(
    client, admin_key, session, loaded_event
) -> None:
    """`validate.check_references_resolve` rejects a self-edge, so the UI must
    not be able to create one — otherwise the admin view can put the repo into a
    state the data-quality suite fails on."""
    from sqlalchemy import func

    from models.catalog import Issue, IssueReference

    client.cookies.set(ADMIN_COOKIE, admin_key)
    before = await session.scalar(select(func.count()).select_from(IssueReference))
    response = await client.post(
        "/ui/curate/king-in-black/reference",
        data={
            "from_key": "king-in-black-1",
            "to_key": "king-in-black-1",
            "relation_type": "references",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = await session.scalar(select(func.count()).select_from(IssueReference))
    assert after == before, "a self-referencing edge was created"

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    self_edges = await session.scalar(
        select(func.count())
        .select_from(IssueReference)
        .where(
            IssueReference.from_issue_id == issue.id,
            IssueReference.to_issue_id == issue.id,
        )
    )
    assert self_edges == 0


async def test_an_edge_to_an_unknown_issue_is_refused(
    client, admin_key, session, loaded_event
) -> None:
    from sqlalchemy import func

    from models.catalog import IssueReference

    client.cookies.set(ADMIN_COOKIE, admin_key)
    before = await session.scalar(select(func.count()).select_from(IssueReference))
    await client.post(
        "/ui/curate/king-in-black/reference",
        data={
            "from_key": "king-in-black-1",
            "to_key": "does-not-exist-1",
            "relation_type": "references",
        },
        follow_redirects=False,
    )
    after = await session.scalar(select(func.count()).select_from(IssueReference))
    assert after == before


async def test_a_non_numeric_omnibus_page_is_stored_as_null(
    client, admin_key, session, loaded_event
) -> None:
    """The field is free text in the form; "p. 14" must not become an error."""
    from models.catalog import IssueReference

    client.cookies.set(ADMIN_COOKIE, admin_key)
    await client.post(
        "/ui/curate/king-in-black/reference",
        data={
            "from_key": "king-in-black-2",
            "to_key": "king-in-black-marauders-1",
            "relation_type": "references",
            "omnibus_page": "page fourteen",
        },
        follow_redirects=False,
    )
    edge = await session.scalar(
        select(IssueReference)
        .order_by(IssueReference.id.desc())
        .limit(1)
    )
    assert edge.omnibus_page is None
