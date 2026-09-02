"""Crossover's answers to the fleet's OAuth contract.

The adapter is the whole file — the assertions live in `oauth_conformance.suite`
and are shared with the other services. If this ever fails, the useful question
is which side moved.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from models.oauth import OAuthClient
from models.user import User
from oauth_conformance import Client, Principal, Tokens
from oauth_conformance.suite import OAuthConformanceSuite
from oauth_provider import (
    DEFAULT_SCOPE,
    get_active_client,
    hash_secret,
    issue_authorization_code,
    new_client_id,
    new_client_secret,
    redeem_authorization_code,
    refresh_token_grant,
    resolve_access_token,
)


class CrossoverOAuth:
    """Crossover's principal is a **human** — a `User` row.

    That is the divergence from `conduct`, which the contract is written to
    tolerate: `conduct` would return `Principal("machine", client_app_id)` from
    the same methods and the assertions would not notice.
    """

    kind = "human"

    def __init__(self, session):
        self._session = session

    async def make_principal(self, kind: str = "human") -> Principal:
        from uuid import uuid4

        suffix = uuid4().hex[:8]
        user = User(
            email=f"conformance-{suffix}@example.test",
            handle=f"conf{suffix}",
            display_name="Conformance",
        )
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)
        return Principal(self.kind, user.id)

    async def register_client(
        self, *, redirect_uris: list[str], registrant: Principal | None = None
    ) -> Client:
        owner = registrant or await self.make_principal()
        secret = new_client_secret()
        row = OAuthClient(
            client_id=new_client_id(),
            client_secret_hash=hash_secret(secret),
            name="conformance",
            # Crossover keeps the registrant on the client for audit. The
            # contract's job is to prove tokens do *not* resolve to it.
            user_id=owner.id,
            redirect_uris=list(redirect_uris),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return Client(
            id=row.client_id, secret=secret, redirect_uris=list(redirect_uris), registrant=owner
        )

    async def _row(self, client: Client) -> OAuthClient:
        row = await self._session.scalar(
            select(OAuthClient).where(OAuthClient.client_id == client.id)
        )
        assert row is not None
        return row

    async def authorize(
        self,
        *,
        client: Client,
        principal: Principal,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        return await issue_authorization_code(
            self._session,
            client=await self._row(client),
            user_id=principal.id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=DEFAULT_SCOPE,
        )

    async def exchange(
        self, *, client: Client, code: str, verifier: str, redirect_uri: str
    ) -> Tokens:
        token = await redeem_authorization_code(
            self._session,
            client=await self._row(client),
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
        )
        return Tokens(access=token.raw_access_token, refresh=token.raw_refresh_token)

    async def refresh(self, *, client: Client, refresh_token: str) -> Tokens:
        token = await refresh_token_grant(
            self._session, client=await self._row(client), refresh_token=refresh_token
        )
        return Tokens(access=token.raw_access_token, refresh=token.raw_refresh_token)

    async def resolve(self, access_token: str) -> Principal | None:
        user = await resolve_access_token(self._session, access_token)
        return Principal(self.kind, user.id) if user else None

    async def deactivate_client(self, client: Client) -> None:
        row = await self._row(client)
        row.is_active = False
        await self._session.commit()
        assert await get_active_client(self._session, client.id) is None


@pytest.fixture
async def oauth(session):
    return CrossoverOAuth(session)


class TestCrossoverConforms(OAuthConformanceSuite):
    """Every assertion from the shared suite, against crossover."""
