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

from auth import ADMIN_COOKIE
from config.settings import get_settings
from models.oauth import OAuthClient
from oauth_provider import hash_secret, new_client_id, new_client_secret

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = (
    urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)


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
def admin(client):
    client.cookies.set(ADMIN_COOKIE, get_settings().admin_key)
    return client


async def test_authorize_bounces_to_login_without_an_admin_session(
    client, connector
) -> None:
    """Approval is being signed in as admin — appropriate for two people."""
    conn, _ = connector
    response = await client.get(
        "/oauth/authorize", params=_authorize_params(conn), follow_redirects=False
    )
    assert response.status_code == 303
    assert "/ui/login" in response.headers["location"]


async def test_the_consent_screen_names_the_reader(admin, connector, user) -> None:
    conn, _ = connector
    response = await admin.get("/oauth/authorize", params=_authorize_params(conn))
    assert response.status_code == 200
    assert "Connect Claude iOS?" in response.text
    assert user.email in response.text


async def test_plain_pkce_is_refused_before_any_code_is_minted(admin, connector) -> None:
    conn, _ = connector
    params = {**_authorize_params(conn), "code_challenge_method": "plain"}
    response = await admin.get(
        "/oauth/authorize", params=params, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=invalid_request" in response.headers["location"]
    assert "PKCE+S256+required" in response.headers["location"]


async def test_an_unregistered_redirect_uri_never_redirects(admin, connector) -> None:
    """An open redirect to an attacker's URI would be the bug here."""
    conn, _ = connector
    params = {**_authorize_params(conn), "redirect_uri": "https://evil.example/cb"}
    response = await admin.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "unregistered redirect_uri" in response.text


async def test_denying_consent_redirects_with_access_denied(admin, connector) -> None:
    conn, _ = connector
    response = await admin.post(
        "/oauth/authorize",
        data={**_authorize_params(conn), "decision": "deny"},
        follow_redirects=False,
    )
    assert "error=access_denied" in response.headers["location"]
    assert "state=xyz" in response.headers["location"]


async def _code(admin, conn) -> str:
    response = await admin.post(
        "/oauth/authorize",
        data={**_authorize_params(conn), "decision": "approve"},
        follow_redirects=False,
    )
    return re.search(r"code=([^&]+)", response.headers["location"]).group(1)


async def test_the_full_grant_over_http(admin, connector) -> None:
    conn, secret = connector
    code = await _code(admin, conn)
    response = await admin.post(
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


async def test_client_credentials_over_http_basic(admin, connector) -> None:
    conn, secret = connector
    code = await _code(admin, conn)
    basic = b64encode(f"{conn.client_id}:{secret}".encode()).decode()
    response = await admin.post(
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


async def test_a_bad_client_secret_is_401(admin, connector) -> None:
    conn, _ = connector
    code = await _code(admin, conn)
    response = await admin.post(
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


async def test_a_replayed_code_is_400_over_http(admin, connector) -> None:
    conn, secret = connector
    code = await _code(admin, conn)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "client_id": conn.client_id,
        "client_secret": secret,
    }
    assert (await admin.post("/oauth/token", data=data)).status_code == 200
    replay = await admin.post("/oauth/token", data=data)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


async def test_an_unsupported_grant_type(admin, connector) -> None:
    conn, secret = connector
    response = await admin.post(
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
    admin, connector, user, db_conn, monkeypatch
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
    code = await _code(admin, conn)
    token = (
        await admin.post(
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
    admin, connector, user, session, db_conn, monkeypatch
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
    code = await _code(admin, conn)
    token = (
        await admin.post(
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
