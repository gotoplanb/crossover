"""The OAuth flow end to end: PKCE happy path, replay, expiry, rotation.

Ported alongside the provider itself. The one behavior worth calling out is
that a token resolves to a **User** — that is the deliberate divergence from
conduct, and the reason bookmarks can be per-person.
"""

from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from models.oauth import OAuthAuthorizationCode, OAuthClient, OAuthToken
from oauth_provider import (
    OAuthError,
    authenticate_client,
    authorization_server_metadata,
    hash_secret,
    issue_authorization_code,
    new_client_id,
    new_client_secret,
    protected_resource_metadata,
    redeem_authorization_code,
    refresh_token_grant,
    resolve_access_token,
)

VERIFIER = "x" * 64


def challenge() -> str:
    digest = hashlib.sha256(VERIFIER.encode("ascii")).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture
async def connector(session, user):
    secret = new_client_secret()
    client = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(secret),
        name="Claude on a phone",
        user_id=user.id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client, secret


async def _code(session, client, user_id=None) -> str:
    # These tests predate principal binding and are about code/PKCE mechanics,
    # so they grant to the connector's own registrant unless told otherwise.
    return await issue_authorization_code(
        session,
        client=client,
        user_id=user_id or client.user_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_challenge=challenge(),
        code_challenge_method="S256",
        scope="mcp",
    )


async def test_happy_path_resolves_to_the_user(session, connector, user) -> None:
    client, _ = connector
    code = await _code(session, client)
    token = await redeem_authorization_code(
        session,
        client=client,
        code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_verifier=VERIFIER,
    )
    resolved = await resolve_access_token(session, token.raw_access_token)
    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.email == user.email


async def test_a_replayed_code_is_rejected(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    kwargs = dict(
        client=client,
        code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_verifier=VERIFIER,
    )
    await redeem_authorization_code(session, **kwargs)
    with pytest.raises(OAuthError, match="already used"):
        await redeem_authorization_code(session, **kwargs)


async def test_a_bad_verifier_is_rejected(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    with pytest.raises(OAuthError, match="PKCE"):
        await redeem_authorization_code(
            session,
            client=client,
            code=code,
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_verifier="y" * 64,
        )


async def test_a_mismatched_redirect_uri_is_rejected(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    with pytest.raises(OAuthError, match="redirect_uri mismatch"):
        await redeem_authorization_code(
            session,
            client=client,
            code=code,
            redirect_uri="https://evil.example/callback",
            code_verifier=VERIFIER,
        )


async def test_an_expired_code_is_rejected(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    row = await session.scalar(select(OAuthAuthorizationCode).where(
        OAuthAuthorizationCode.client_id == client.client_id
    ))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    with pytest.raises(OAuthError, match="expired"):
        await redeem_authorization_code(
            session,
            client=client,
            code=code,
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_verifier=VERIFIER,
        )


async def test_an_expired_access_token_does_not_authenticate(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    token = await redeem_authorization_code(
        session, client=client, code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback", code_verifier=VERIFIER,
    )
    row = await session.scalar(select(OAuthToken).where(
        OAuthToken.access_token_hash == token.access_token_hash
    ))
    row.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert await resolve_access_token(session, token.raw_access_token) is None


async def test_refresh_rotates_and_revokes_the_old_token(session, connector) -> None:
    client, _ = connector
    code = await _code(session, client)
    first = await redeem_authorization_code(
        session, client=client, code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback", code_verifier=VERIFIER,
    )
    second = await refresh_token_grant(
        session, client=client, refresh_token=first.raw_refresh_token
    )
    assert second.raw_access_token != first.raw_access_token
    # The old refresh token is single-use.
    with pytest.raises(OAuthError, match="revoked"):
        await refresh_token_grant(
            session, client=client, refresh_token=first.raw_refresh_token
        )


async def test_deactivating_the_connector_kills_live_tokens(session, connector) -> None:
    """The kill switch: revoking the connector must not wait for expiry."""
    client, _ = connector
    code = await _code(session, client)
    token = await redeem_authorization_code(
        session, client=client, code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback", code_verifier=VERIFIER,
    )
    assert await resolve_access_token(session, token.raw_access_token) is not None
    client.is_active = False
    await session.commit()
    assert await resolve_access_token(session, token.raw_access_token) is None


async def test_deactivating_the_reader_kills_their_tokens(session, connector, user) -> None:
    client, _ = connector
    code = await _code(session, client)
    token = await redeem_authorization_code(
        session, client=client, code=code,
        redirect_uri="https://claude.ai/api/mcp/auth_callback", code_verifier=VERIFIER,
    )
    user.is_active = False
    await session.commit()
    assert await resolve_access_token(session, token.raw_access_token) is None


async def test_client_authentication(session, connector) -> None:
    client, secret = connector
    assert (await authenticate_client(session, client.client_id, secret)).id == client.id
    with pytest.raises(OAuthError, match="bad client secret"):
        await authenticate_client(session, client.client_id, "wrong")
    with pytest.raises(OAuthError, match="unknown or inactive"):
        await authenticate_client(session, "xoc_nope", secret)


async def test_a_garbage_token_resolves_to_nothing(session) -> None:
    assert await resolve_access_token(session, "xo_at_nope") is None
    assert await resolve_access_token(session, "") is None


def test_discovery_metadata_advertises_only_s256() -> None:
    metadata = authorization_server_metadata()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert protected_resource_metadata()["resource"].endswith("/mcp")
