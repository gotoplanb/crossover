"""Per-reader passwords, and the separation they exist to provide.

Two people share this deployment. A shared key would let either of them open the
other's rack, which defeats the point of a per-person reading list — so each
reader signs in with their own password, supplied as
`CROSSOVER_PASSWORD_{HANDLE}`, and only admins receive the curation cookie.
"""

from __future__ import annotations

import pytest

from auth import SESSION_COOKIE, verify_reader_password
from config.settings import get_settings
from models.user import User, valid_handle

DAVE_PASSWORD = "dave-local-password-1"  # pragma: allowlist secret
TABITHA_PASSWORD = "tabitha-local-password-2"  # pragma: allowlist secret


@pytest.fixture
def passwords(monkeypatch):
    monkeypatch.setenv("CROSSOVER_PASSWORD_DAVE", DAVE_PASSWORD)
    monkeypatch.setenv("CROSSOVER_PASSWORD_TABITHA", TABITHA_PASSWORD)
    monkeypatch.delenv("CROSSOVER_PASSWORD_NOBODY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


# --- the password check itself ---


def test_the_right_password_verifies(passwords) -> None:
    assert verify_reader_password("dave", DAVE_PASSWORD) is True


def test_one_readers_password_does_not_work_for_another(passwords) -> None:
    """The whole point. A shared secret could not do this."""
    assert verify_reader_password("dave", TABITHA_PASSWORD) is False
    assert verify_reader_password("tabitha", DAVE_PASSWORD) is False


def test_an_unset_password_never_verifies(passwords) -> None:
    """A seeded reader with no config var must not be loginable with anything,
    least of all an empty string."""
    assert verify_reader_password("nobody", "") is False
    assert verify_reader_password("nobody", "anything") is False


def test_an_empty_submission_is_rejected(passwords) -> None:
    assert verify_reader_password("dave", "") is False


def test_a_near_miss_is_rejected(passwords) -> None:
    """Guards the comparison: `==` would short-circuit and leak the password a
    character at a time, which is why this uses hmac.compare_digest."""
    assert verify_reader_password("dave", DAVE_PASSWORD[:-1]) is False
    assert verify_reader_password("dave", DAVE_PASSWORD + "x") is False


@pytest.mark.parametrize("handle", ["../etc/passwd", "DAVE", "has space", "1leading", "", "a" * 40])
def test_a_handle_that_cannot_name_an_env_var_is_refused(handle, passwords) -> None:
    """A handle becomes part of an environment variable name, so anything that
    is not a legal suffix must never reach `os.environ.get`."""
    assert valid_handle(handle) is False
    assert verify_reader_password(handle, "anything") is False


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


def test_a_password_in_the_env_file_is_found(monkeypatch, tmp_path) -> None:
    """Reader passwords are looked up by a *dynamic* key, so pydantic never
    loads them from .env the way it does declared fields. Without an explicit
    fallback a password in .env works in production — where config vars are real
    environment variables — and silently fails locally, which is the worst
    possible split.
    """
    from config import settings as settings_module

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "CROSSOVER_PASSWORD_FROMFILE=from-the-file\n"  # pragma: allowlist secret
        "MALFORMED_LINE_WITHOUT_EQUALS\n"
    )
    monkeypatch.setattr(settings_module, "ENV_FILE", env_file)
    monkeypatch.delenv("CROSSOVER_PASSWORD_FROMFILE", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().reader_password("fromfile") == "from-the-file"
    finally:
        get_settings.cache_clear()


def test_a_real_environment_variable_beats_the_env_file(monkeypatch, tmp_path) -> None:
    """A Heroku config var must win over a stale checked-out .env."""
    from config import settings as settings_module

    env_file = tmp_path / ".env"
    env_file.write_text("CROSSOVER_PASSWORD_BOTH=from-the-file\n")  # pragma: allowlist secret
    monkeypatch.setattr(settings_module, "ENV_FILE", env_file)
    monkeypatch.setenv("CROSSOVER_PASSWORD_BOTH", "from-the-environment")
    get_settings.cache_clear()
    try:
        assert get_settings().reader_password("both") == "from-the-environment"
    finally:
        get_settings.cache_clear()


def test_a_missing_env_file_is_not_an_error(monkeypatch, tmp_path) -> None:
    """A container has no .env at all — only real environment variables."""
    from config import settings as settings_module

    monkeypatch.setattr(settings_module, "ENV_FILE", tmp_path / "nope.env")
    monkeypatch.setenv("CROSSOVER_PASSWORD_ONLYENV", "set")
    get_settings.cache_clear()
    try:
        assert get_settings().reader_password("onlyenv") == "set"
        assert get_settings().reader_password("absent") is None
    finally:
        get_settings.cache_clear()
