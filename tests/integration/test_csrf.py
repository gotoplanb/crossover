"""CSRF protection (#18).

The threat is narrow: a page on another site causing the reader's browser to
POST here with the session cookie attached, and the app treating it as an
intentional act. `/oauth/authorize` matters most — that POST grants a connector
access to a whole reading list.

Two things need proving. That a forged request is actually rejected, and that
the protection is applied broadly enough that a form added later cannot quietly
escape it.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select

from csrf import CSRF_FIELD, CSRF_HEADER, EXEMPT_PREFIXES, UNSAFE_METHODS
from models.catalog import IssueReference

# --- the attack this exists to stop ---


async def test_a_forged_post_with_a_valid_session_is_rejected(
    signed_in, session, loaded_event
) -> None:
    """The whole point: the browser attaches the session cookie automatically,
    so authentication alone cannot distinguish a forgery from a real click."""
    before = await session.scalar(select(func.count()).select_from(IssueReference))

    forged = dict(signed_in.headers)
    forged.pop(CSRF_HEADER, None)
    response = await signed_in.post(
        "/ui/curate/king-in-black/reference",
        data={
            "from_key": "king-in-black-1",
            "to_key": "king-in-black-2",
            "relation_type": "references",
        },
        headers={CSRF_HEADER: ""},
        follow_redirects=False,
    )
    assert response.status_code == 403
    after = await session.scalar(select(func.count()).select_from(IssueReference))
    assert after == before, "a forged request changed state"


async def test_a_wrong_token_is_rejected(signed_in, loaded_event) -> None:
    response = await signed_in.post(
        "/ui/curate/king-in-black/move",
        data={
            "issue_key": "king-in-black-1",
            "direction": "down",
            CSRF_FIELD: "not-the-right-token",
        },
        headers={CSRF_HEADER: "not-the-right-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_another_sessions_token_does_not_work(
    client, signed_in, session, other_user, loaded_event
) -> None:
    """Tokens are bound to a session, so one reader's cannot authorise a request
    made with another's cookie."""
    from auth import _hash, create_session
    from models.session import UserSession

    other_token = await create_session(session, other_user)
    other_row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _hash(other_token))
    )
    response = await signed_in.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": "king-in-black-1", "direction": "down"},
        headers={CSRF_HEADER: other_row.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_the_right_token_is_accepted(signed_in, loaded_event) -> None:
    """The fixture carries the session's token, the way a rendered form does."""
    response = await signed_in.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": "king-in-black-1", "direction": "down"},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def test_the_consent_form_is_protected(signed_in, session, user) -> None:
    """The highest-value target: this POST grants a connector a whole rack."""
    from models.oauth import OAuthClient
    from oauth_provider import hash_secret, new_client_id, new_client_secret

    redirect = "https://claude.ai/api/mcp/auth_callback"
    connector = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(new_client_secret()),
        name="phone",
        user_id=user.id,
        redirect_uris=[redirect],
    )
    session.add(connector)
    await session.commit()

    response = await signed_in.post(
        "/oauth/authorize",
        data={
            "client_id": connector.client_id,
            "redirect_uri": redirect,
            "code_challenge": "abc",
            "decision": "approve",
        },
        headers={CSRF_HEADER: "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_a_revoked_session_cannot_be_used_to_forge(
    client, signed_in, session, user, loaded_event
) -> None:
    """Revoking kills the CSRF token with the session, so a captured pair is
    worthless together as well as apart."""
    from auth import revoke_all_for_user

    await revoke_all_for_user(session, user.id)
    response = await signed_in.post(
        "/ui/curate/king-in-black/move",
        data={"issue_key": "king-in-black-1", "direction": "down"},
        follow_redirects=False,
    )
    assert response.status_code in {303, 403}
    # Either way, nothing was curated: the redirect is to the login page.
    if response.status_code == 303:
        assert "/ui/login" in response.headers["location"]


# --- an expired session should not produce a bare 403 ---


async def test_an_unauthenticated_post_bounces_to_login(client, loaded_event) -> None:
    """A reader whose session expired mid-form should land on the login page,
    not an error. Safe because there is no authenticated context to forge — the
    route's own auth rejects it either way."""
    response = await client.post(
        "/ui/rack/confirm",
        data={"candidate_id": "x", "chosen_key": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


# --- breadth: a new form must not escape ---


def test_protection_is_applied_to_the_router_not_per_route() -> None:
    """Per-route decoration is what lets a new endpoint silently skip the check.
    Both routers that serve cookie-authenticated forms declare it once."""
    import routes.oauth
    import routes.ui

    for module in (routes.ui, routes.oauth):
        source = inspect.getsource(module)
        assert "dependencies=[Depends(csrf_protect)]" in source, module.__name__


def test_every_unsafe_ui_route_is_covered() -> None:
    """Enumerated rather than assumed, so adding a POST without protection shows
    up here as a failure rather than as a security hole."""
    import routes.ui

    unsafe = [
        route
        for route in routes.ui.router.routes
        if set(getattr(route, "methods", set())) & UNSAFE_METHODS
    ]
    assert unsafe, "expected the UI router to have state-changing routes"
    # The dependency is declared on the router, so every route inherits it.
    assert any(
        "csrf_protect" in str(d.dependency) for d in routes.ui.router.dependencies
    )


@pytest.mark.parametrize("prefix", EXEMPT_PREFIXES)
def test_every_exemption_is_deliberate_and_documented(prefix: str) -> None:
    """The exemption list is the one place protection can be dropped, so it has
    to stay small and each entry has to say why."""
    import csrf

    source = inspect.getsource(csrf)
    assert prefix in source
    # Each exemption is explained in the comment block above the list.
    start = source.index("#: Paths that authenticate")
    explanation = source[start : source.index("EXEMPT_PREFIXES =")]
    assert prefix in explanation, f"{prefix} is exempt with no stated reason"


def test_the_exemption_list_has_not_grown() -> None:
    """Both entries authenticate with something other than a cookie, so a
    cross-site request gains nothing. A third would need justifying."""
    assert set(EXEMPT_PREFIXES) == {"/oauth/token", "/mcp"}


async def test_the_token_endpoint_still_works_without_a_csrf_token(client) -> None:
    """It is called by a server with client credentials, never a browser form —
    requiring a CSRF token would simply break OAuth."""
    response = await client.post("/oauth/token", data={"grant_type": "bogus"})
    assert response.status_code in {400, 401}


async def test_the_mcp_endpoint_is_not_csrf_gated(client) -> None:
    """Bearer-authenticated; a 401 proves it reached the auth gate rather than
    being turned away at the CSRF check."""
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 401


# --- the token reaches the page ---


async def test_forms_carry_the_token(signed_in, loaded_event) -> None:
    html = (await signed_in.get("/ui/curate/king-in-black")).text
    assert f'name="{CSRF_FIELD}"' in html
    assert 'value=""' not in html.split(f'name="{CSRF_FIELD}"')[1][:40]


async def test_htmx_requests_carry_the_token_as_a_header(signed_in, loaded_event) -> None:
    """HTMX posts have no form body, so the token rides as a header — attached
    globally in base.html so a new hx-post cannot forget it."""
    html = (await signed_in.get("/ui/rack")).text
    assert "htmx:configRequest" in html
    assert "X-CSRF-Token" in html


async def test_the_login_form_carries_a_token_before_any_session(client) -> None:
    """No session exists yet, so the token is double-submitted from a cookie
    issued when the page renders."""
    from csrf import CSRF_COOKIE

    response = await client.get("/ui/login")
    assert f'name="{CSRF_FIELD}"' in response.text
    assert any(CSRF_COOKIE in c for c in response.headers.get_list("set-cookie"))


async def test_login_rejects_a_missing_token(client, user, reader_password) -> None:
    await client.get("/ui/login")
    response = await client.post(
        "/ui/login", data={"handle": user.handle, "password": reader_password}
    )
    assert response.status_code == 403


async def test_signing_in_while_already_signed_in_works(
    client, sign_in, session, user, other_user, reader_password, monkeypatch
) -> None:
    """Two people share a machine and hand it over — so the login form has to
    work when a session already exists. It broke when the form rendered a
    pre-auth token while the check expected the existing session's."""
    from config.settings import get_settings

    monkeypatch.setenv(
        f"CROSSOVER_PASSWORD_{other_user.handle.upper()}", "second-reader-pw"
    )
    get_settings.cache_clear()
    try:
        assert (await sign_in(user.handle, reader_password)).status_code == 303
        assert (await sign_in(other_user.handle, "second-reader-pw")).status_code == 303
    finally:
        get_settings.cache_clear()
