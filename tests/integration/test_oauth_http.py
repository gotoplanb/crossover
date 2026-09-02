"""The OAuth endpoints over HTTP — the flow Claude's connector actually walks.

`test_oauth_flow.py` covers the provider logic; this covers the wire: consent
screen, redirects, Basic-auth client credentials, and the error bodies.
"""

from __future__ import annotations

import hashlib
import re
from base64 import b64encode, urlsafe_b64encode

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.oauth import OAuthClient
from oauth_provider import hash_secret, new_client_id, new_client_secret

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
async def connector(session, user):
    secret = new_client_secret()
    client = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(secret),
        name="Claude iOS",
        user_id=user.id,
        redirect_uris=[REDIRECT],
    )
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client, secret


def _authorize_params(client) -> dict:
    return {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "mcp",
        "state": "xyz",
    }


@pytest.fixture
def reader(signed_in):
    """A client signed in as a reader.

    Consent no longer requires admin: the grant binds to whoever approves, so
    being signed in *is* the identity step. This fixture's user happens to be an
    admin, which the flow is now indifferent to — `as_other` below covers the
    reader who is not.
    """
    return signed_in


@pytest.fixture
async def as_other(client, session, other_user):
    """The shared client signed in as a *different*, non-admin reader.

    Mints the session directly for the same reason `signed_in` does — it is the
    same code path a login takes, without needing a password fixture per reader.
    """
    from sqlalchemy import select

    from auth import SESSION_COOKIE, _hash, create_session
    from csrf import CSRF_HEADER
    from models.session import UserSession

    token = await create_session(session, other_user)
    client.cookies.set(SESSION_COOKIE, token)
    row = await session.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
    client.headers[CSRF_HEADER] = row.csrf_token
    return client


async def test_authorize_bounces_to_login_when_nobody_is_signed_in(client, connector) -> None:
    """An anonymous visitor is sent to log in rather than refused: signing in is
    how the flow learns who the token should act as."""
    conn, _ = connector
    response = await client.get(
        "/oauth/authorize", params=_authorize_params(conn), follow_redirects=False
    )
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_the_consent_screen_names_the_reader(reader, connector, user) -> None:
    conn, _ = connector
    response = await reader.get("/oauth/authorize", params=_authorize_params(conn))
    assert response.status_code == 200
    assert "Connect Claude iOS?" in response.text
    assert user.email in response.text


async def test_plain_pkce_is_refused_before_any_code_is_minted(reader, connector) -> None:
    conn, _ = connector
    params = {**_authorize_params(conn), "code_challenge_method": "plain"}
    response = await reader.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303
    assert "error=invalid_request" in response.headers["location"]
    assert "PKCE+S256+required" in response.headers["location"]


async def test_an_unregistered_redirect_uri_never_redirects(reader, connector) -> None:
    """An open redirect to an attacker's URI would be the bug here."""
    conn, _ = connector
    params = {**_authorize_params(conn), "redirect_uri": "https://evil.example/cb"}
    response = await reader.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "unregistered redirect_uri" in response.text


async def test_denying_consent_redirects_with_access_denied(reader, connector) -> None:
    conn, _ = connector
    response = await reader.post(
        "/oauth/authorize",
        data={**_authorize_params(conn), "decision": "deny"},
        follow_redirects=False,
    )
    assert "error=access_denied" in response.headers["location"]
    assert "state=xyz" in response.headers["location"]


async def _code(reader, conn) -> str:
    response = await reader.post(
        "/oauth/authorize",
        data={**_authorize_params(conn), "decision": "approve"},
        follow_redirects=False,
    )
    return re.search(r"code=([^&]+)", response.headers["location"]).group(1)


async def test_the_full_grant_over_http(reader, connector) -> None:
    conn, secret = connector
    code = await _code(reader, conn)
    response = await reader.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": conn.client_id,
            "client_secret": secret,
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["token_type"] == "Bearer"
    assert body["access_token"].startswith("xo_at_")
    assert body["refresh_token"].startswith("xo_rt_")
    # Tokens must never be cached by an intermediary.
    assert response.headers["cache-control"] == "no-store"


async def test_client_credentials_over_http_basic(reader, connector) -> None:
    conn, secret = connector
    code = await _code(reader, conn)
    basic = b64encode(f"{conn.client_id}:{secret}".encode()).decode()
    response = await reader.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
        headers={"Authorization": f"Basic {basic}"},
    )
    assert response.status_code == 200


async def test_a_bad_client_secret_is_401(reader, connector) -> None:
    conn, _ = connector
    code = await _code(reader, conn)
    response = await reader.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": conn.client_id,
            "client_secret": "xos_wrong",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


async def test_a_replayed_code_is_400_over_http(reader, connector) -> None:
    conn, secret = connector
    code = await _code(reader, conn)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "client_id": conn.client_id,
        "client_secret": secret,
    }
    assert (await reader.post("/oauth/token", data=data)).status_code == 200
    replay = await reader.post("/oauth/token", data=data)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


async def test_an_unsupported_grant_type(reader, connector) -> None:
    conn, secret = connector
    response = await reader.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": conn.client_id,
            "client_secret": secret,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


async def test_a_token_from_the_flow_is_accepted_by_the_mcp_gate(
    reader, connector, user, db_conn, monkeypatch
) -> None:
    """The end of the whole chain: a token minted over HTTP resolves, at the
    ASGI gate, to the person whose reading list it acts on.

    Exercised against the middleware directly rather than through the mounted
    transport, because the transport's session manager only runs inside the app
    lifespan, which the test app skips. The gate is the part under test here —
    it is pure ASGI and opens its own DB session (a dependency override would
    not reach it), so `SessionLocal` is rebound to the test transaction.
    """
    import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        async_sessionmaker(
            bind=db_conn,
            expire_on_commit=False,
            class_=AsyncSession,
            join_transaction_mode="create_savepoint",
        ),
    )
    conn, secret = connector
    code = await _code(reader, conn)
    token = (
        await reader.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": VERIFIER,
                "client_id": conn.client_id,
                "client_secret": secret,
            },
        )
    ).json()["access_token"]

    seen: dict = {}

    async def inner(scope, receive, send):
        seen.update(mcp_server._principal.get() or {})

    gate = mcp_server.OAuthMiddleware(inner)
    scope = {
        "type": "http",
        "path": "/mcp/",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    sent: list = []

    async def capture(message):
        sent.append(message)

    await gate(scope, None, capture)
    assert seen.get("user_id") == user.id, "the gate did not resolve the token to a reader"
    assert seen.get("email") == user.email
    assert sent == [], "a valid token must not produce a 401"


async def test_the_gate_rejects_a_token_after_the_reader_is_deactivated(
    reader, connector, user, session, db_conn, monkeypatch
) -> None:
    """Deactivating a reader is a kill switch that does not wait for expiry."""
    import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        async_sessionmaker(
            bind=db_conn,
            expire_on_commit=False,
            class_=AsyncSession,
            join_transaction_mode="create_savepoint",
        ),
    )
    conn, secret = connector
    code = await _code(reader, conn)
    token = (
        await reader.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": VERIFIER,
                "client_id": conn.client_id,
                "client_secret": secret,
            },
        )
    ).json()["access_token"]

    user.is_active = False
    await session.commit()

    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    sent: list = []

    async def capture(message):
        sent.append(message)

    await mcp_server.OAuthMiddleware(inner)(
        {
            "type": "http",
            "path": "/mcp/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        None,
        capture,
    )
    assert not called
    assert sent[0]["status"] == 401


# --- the grant binds to whoever approves ------------------------------------
#
# Previously the token's principal came from `client.user_id` — whoever
# registered the connector — so one connector could only ever act as one person
# and a second reader needed a second connector registered on their behalf.


async def test_a_reader_who_is_not_an_admin_can_connect(as_other, connector) -> None:
    """The whole point. Consent is not an administrative act; it is a reader
    saying "yes, this client may act as me"."""
    conn, secret = connector
    code = await _code(as_other, conn)
    response = await as_other.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": conn.client_id,
            "client_secret": secret,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"].startswith("xo_at_")


async def test_the_token_acts_as_the_approver_not_the_registrant(
    as_other, connector, session, user, other_user
) -> None:
    """The crux. `connector` is registered to `user`, but `other_user` is the
    one who approves — so the token has to resolve to `other_user`, or one
    person's connector would read another person's reading list."""
    from oauth_provider import resolve_access_token

    conn, secret = connector
    assert conn.user_id == user.id, "the connector is registered to someone else"

    code = await _code(as_other, conn)
    body = (
        await as_other.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": VERIFIER,
                "client_id": conn.client_id,
                "client_secret": secret,
            },
        )
    ).json()

    principal = await resolve_access_token(session, body["access_token"])
    assert principal is not None
    assert principal.id == other_user.id, "the token acted as the registrant"


async def test_refreshing_keeps_the_principal(as_other, connector, session, other_user) -> None:
    """Rotation must carry the approver across. Deriving the principal from the
    client at refresh time would silently re-bind the token to the connector's
    registrant — the same bug with a longer fuse, and one that would only show
    up a day later."""
    from oauth_provider import resolve_access_token

    conn, secret = connector
    code = await _code(as_other, conn)
    creds = {"client_id": conn.client_id, "client_secret": secret}
    first = (
        await as_other.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": VERIFIER,
                **creds,
            },
        )
    ).json()

    refreshed = (
        await as_other.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                **creds,
            },
        )
    ).json()

    assert refreshed["access_token"] != first["access_token"]
    principal = await resolve_access_token(session, refreshed["access_token"])
    assert principal is not None and principal.id == other_user.id


async def test_two_readers_share_one_connector(
    client, connector, session, user, other_user
) -> None:
    """One connector definition, a token each, separately revocable. This is
    what removes the need to register a connector per person — and it is the
    Dave-and-Tabitha case stated as a test."""
    from sqlalchemy import select

    from auth import SESSION_COOKIE, _hash, create_session
    from csrf import CSRF_HEADER
    from models.session import UserSession
    from oauth_provider import resolve_access_token

    conn, secret = connector
    creds = {"client_id": conn.client_id, "client_secret": secret}

    async def become(reader_row):
        """Point the shared client at a session for this reader."""
        token = await create_session(session, reader_row)
        client.cookies.set(SESSION_COOKIE, token)
        row = await session.scalar(
            select(UserSession).where(UserSession.token_hash == _hash(token))
        )
        client.headers[CSRF_HEADER] = row.csrf_token

    async def grant() -> str:
        code = await _code(client, conn)
        return (
            await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT,
                    "code_verifier": VERIFIER,
                    **creds,
                },
            )
        ).json()["access_token"]

    await become(user)
    first = await grant()
    await become(other_user)
    second = await grant()

    assert first != second
    resolved_first = await resolve_access_token(session, first)
    resolved_second = await resolve_access_token(session, second)
    assert resolved_first is not None and resolved_first.id == user.id
    assert resolved_second is not None and resolved_second.id == other_user.id
