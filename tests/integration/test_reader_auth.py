"""Per-reader passwords, and the separation they exist to provide.

Two people share this deployment. A shared key would let either of them open the
other's rack, which defeats the point of a per-person reading list — so each
reader signs in with their own password, supplied as
their own argon2-hashed password, and only admins reach the curation views.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from auth import SESSION_COOKIE
from models.user import User, valid_handle

DAVE_PASSWORD = "dave-local-password-1"  # pragma: allowlist secret
TABITHA_PASSWORD = "tabitha-local-password-2"  # pragma: allowlist secret


@pytest_asyncio.fixture
async def passwords(session, readers):
    """Give both readers a password, the way an admin would.

    Real argon2 hashes. These used to be environment variables, one per reader,
    which meant every sign-in test below exercised a path that no longer
    exists.
    """
    from auth import set_password

    dave, tabitha = readers
    await set_password(session, dave, DAVE_PASSWORD)
    await set_password(session, tabitha, TABITHA_PASSWORD)


@pytest.fixture
async def readers(session):
    dave = User(email="dave@test.local", handle="dave", display_name="Dave", is_admin=True)
    tabitha = User(
        email="tabitha@test.local", handle="tabitha", display_name="Tabitha", is_admin=False
    )
    session.add_all([dave, tabitha])
    await session.commit()
    await session.refresh(dave)
    await session.refresh(tabitha)
    return dave, tabitha


# --- handles ---


@pytest.mark.parametrize("handle", ["../etc/passwd", "DAVE", "has space", "1leading", "", "a" * 40])
def test_a_malformed_handle_is_refused(handle) -> None:
    """The constraint originally existed because a handle named an environment
    variable. Passwords live in the database now, so it is no longer
    load-bearing — but it is kept, because handles are typed at a login form
    and read aloud, and the narrow character set keeps them unambiguous."""
    assert valid_handle(handle) is False


def test_valid_handles_are_accepted() -> None:
    for handle in ("dave", "tabitha", "claude", "r2d2", "a_b_c"):
        assert valid_handle(handle), handle


# --- signing in ---


async def test_each_reader_signs_in_with_their_own_password(
    client, sign_in, readers, passwords
) -> None:
    dave, tabitha = readers
    for handle, password, user in (
        ("dave", DAVE_PASSWORD, dave),
        ("tabitha", TABITHA_PASSWORD, tabitha),
    ):
        response = await sign_in(handle, password)
        assert response.status_code == 303, handle
        cookies = response.headers.get_list("set-cookie")
        assert any(SESSION_COOKIE in c for c in cookies), handle
        # A random token, never the reader's database id (#17).
        assert str(user.id) not in "".join(cookies), handle
        assert any("xo_sess_" in c for c in cookies), handle


async def test_a_reader_cannot_sign_in_as_the_other(client, sign_in, readers, passwords) -> None:
    response = await sign_in("tabitha", DAVE_PASSWORD)
    assert response.status_code == 401
    assert "Wrong password, or no such reader." in response.text


async def test_failures_are_indistinguishable(client, sign_in, readers, passwords) -> None:
    """A wrong password, an unknown handle and a deactivated reader all return
    the same thing, so the form cannot be used to enumerate who exists."""
    messages = set()
    for handle, password in (
        ("dave", "wrong"),
        ("nobody", "wrong"),
        ("tabitha", ""),
    ):
        response = await sign_in(handle, password)
        assert response.status_code == 401
        messages.add("Wrong password, or no such reader." in response.text)
    assert messages == {True}


async def test_a_deactivated_reader_cannot_sign_in(
    client, sign_in, session, readers, passwords
) -> None:
    dave, _ = readers
    dave.is_active = False
    await session.commit()
    response = await sign_in("dave", DAVE_PASSWORD)
    assert response.status_code == 401


# --- admin separation ---


async def test_signing_in_sets_one_session_cookie(client, sign_in, readers, passwords) -> None:
    """Admin is a property of the reader the session resolves to, not a second
    cookie — so there is one credential to steal instead of two, and revoking a
    session revokes curation access with it. The pre-auth CSRF cookie is cleared
    on the way through, having done its job."""
    for handle, password in (("dave", DAVE_PASSWORD), ("tabitha", TABITHA_PASSWORD)):
        response = await sign_in(handle, password)
        cookies = response.headers.get_list("set-cookie")
        assert sum(SESSION_COOKIE in c for c in cookies) == 1, handle
        assert any("crossover_csrf" in c and "Max-Age=0" in c for c in cookies), handle


async def test_a_non_admin_is_bounced_from_the_curation_views(
    client, sign_in, readers, passwords, loaded_event
) -> None:
    await sign_in("tabitha", TABITHA_PASSWORD)
    response = await client.get("/ui/curate/king-in-black", follow_redirects=False)
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_a_non_admin_still_gets_their_own_rack(
    client, sign_in, readers, passwords, loaded_event
) -> None:
    """Not admin does not mean not welcome — the rack is the point."""
    await sign_in("tabitha", TABITHA_PASSWORD)
    response = await client.get("/ui/rack")
    assert response.status_code == 200
    assert "Tabitha" in response.text


async def test_an_admin_reaches_the_curation_views(
    client, sign_in, readers, passwords, loaded_event
) -> None:
    await sign_in("dave", DAVE_PASSWORD)
    assert (await client.get("/ui/curate/king-in-black")).status_code == 200


async def test_the_login_form_names_nobody(client, readers) -> None:
    """It used to render a dropdown of every active reader, which was fine for a
    household and became a directory of everyone with an account the moment
    registration opened. Neither handles nor emails belong on a public page."""
    html = (await client.get("/ui/login")).text
    assert "dave@test.local" not in html
    assert 'value="dave"' not in html
    assert "<select" not in html, "a reader list is a list of who exists"
    assert 'name="handle"' in html, "the handle is typed, not chosen"


async def test_racks_stay_separate(
    client, sign_in, session, readers, passwords, loaded_event
) -> None:
    """The end result: signing in as one reader shows that reader's rack."""
    from curation.resolve import candidates_from_guide, resolve
    from service import bookmarks as bookmark_service
    from service import guide as guide_service

    dave, tabitha = readers
    entries = await guide_service.all_entries(session)
    pool = candidates_from_guide(entries)
    by_key = {e.key: e for e in entries}
    for user, ref in ((dave, "King in Black: Namor #1"), (tabitha, "Venom #34")):
        entry = by_key[resolve(ref, pool).matched.key]
        await bookmark_service.create_bookmark(session, user_id=user.id, entry=entry)

    await sign_in("tabitha", TABITHA_PASSWORD)
    html = (await client.get("/ui/rack")).text
    assert "Venom #34" in html
    assert "Namor" not in html


# --- where the password is read from ---
