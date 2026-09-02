"""The contract, checked against something that is not crossover.

`tests/conformance/test_crossover_conforms.py` proves crossover obeys the rules.
It cannot prove the *rules* are any good — a suite written against one
implementation tends to describe that implementation.

So this runs the same assertions against a second, deliberately different one:
an in-memory server whose principal is a **machine**, like `conduct`'s, and
which issues no refresh tokens at all, like `davestanton.com`'s. If the contract
were secretly encoding crossover's shape, this is where that would show.
"""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field

import pytest

from oauth_conformance import Client, Principal, Tokens
from oauth_conformance.suite import OAuthConformanceSuite


class Refused(Exception):
    """This implementation's way of saying no. The suite must not care."""


@dataclass
class _Code:
    principal: Principal
    client_id: str
    redirect_uri: str
    challenge: str
    method: str
    used: bool = False


@dataclass
class InMemoryOAuth:
    """A minimal server holding the contract's rules and nothing else.

    Machine principals, no refresh tokens, plaintext in a dict. None of that is
    a recommendation — it is the point. The assertions should hold for a design
    that shares no structure with crossover's.
    """

    codes: dict[str, _Code] = field(default_factory=dict)
    tokens: dict[str, tuple[Principal, str]] = field(default_factory=dict)
    clients: dict[str, Client] = field(default_factory=dict)
    inactive: set[str] = field(default_factory=set)
    _n: int = 0

    async def make_principal(self, kind: str = "machine") -> Principal:
        self._n += 1
        return Principal("machine", f"machine-{self._n}")

    async def register_client(
        self, *, redirect_uris: list[str], registrant: Principal | None = None
    ) -> Client:
        client = Client(
            id=f"c-{secrets.token_hex(4)}",
            secret=secrets.token_hex(8),
            redirect_uris=list(redirect_uris),
            registrant=registrant or await self.make_principal(),
        )
        self.clients[client.id] = client
        return client

    async def authorize(
        self,
        *,
        client: Client,
        principal: Principal,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        if code_challenge_method != "S256":
            raise Refused("S256 only")
        code = secrets.token_hex(8)
        # The principal is stored because it was *given*, not looked up from
        # `client.registrant`. That single line is the contract's whole subject.
        self.codes[code] = _Code(
            principal=principal,
            client_id=client.id,
            redirect_uri=redirect_uri,
            challenge=code_challenge,
            method=code_challenge_method,
        )
        return code

    async def exchange(
        self, *, client: Client, code: str, verifier: str, redirect_uri: str
    ) -> Tokens:
        row = self.codes.get(code)
        if row is None or row.used:
            raise Refused("unknown or spent code")
        if row.client_id != client.id:
            raise Refused("issued to another client")
        if row.redirect_uri != redirect_uri:
            raise Refused("redirect_uri mismatch")
        digest = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if digest.rstrip(b"=").decode() != row.challenge:
            raise Refused("PKCE failed")
        row.used = True
        access = secrets.token_hex(16)
        self.tokens[access] = (row.principal, client.id)
        return Tokens(access=access, refresh=None)

    async def refresh(self, *, client: Client, refresh_token: str) -> Tokens:
        raise Refused("this implementation issues no refresh tokens")

    async def resolve(self, access_token: str) -> Principal | None:
        found = self.tokens.get(access_token)
        if found is None:
            return None
        principal, client_id = found
        # Revocation has to bite on the next call, not at the next expiry.
        return None if client_id in self.inactive else principal

    async def deactivate_client(self, client: Client) -> None:
        self.inactive.add(client.id)


@pytest.fixture
async def oauth():
    return InMemoryOAuth()


class TestAMachinePrincipalAlsoConforms(OAuthConformanceSuite):
    """The same assertions, a different shape of implementation."""


# --- the value type the suite compares principals with -----------------------


def test_principals_compare_across_id_types() -> None:
    """Implementations store ids as UUIDs, ints or strings. The contract
    compares what they hand back, so it must not care which."""
    from uuid import UUID

    raw = UUID("8c466ba3-1c1e-4491-aa7d-09fd82fc57d1")
    assert Principal("human", raw) == Principal("human", str(raw))
    assert hash(Principal("human", raw)) == hash(Principal("human", str(raw)))


def test_a_principal_is_not_equal_to_other_things() -> None:
    assert Principal("human", 1) != "human-1"
    assert Principal("human", 1) != Principal("machine", 1)


def test_a_principal_can_key_a_dict() -> None:
    """The suite puts them in sets and dicts while comparing grants."""
    seen = {Principal("machine", 7): "a"}
    assert seen[Principal("machine", "7")] == "a"


@dataclass
class OneToOneOAuth(InMemoryOAuth):
    """A client that can only ever act for its own principal, like conduct's.

    Conduct's `OAuthClient` has a foreign key to exactly one `ClientApp`, so
    deriving the principal from the client is sound there. This mirrors that
    shape so the paths conduct exercises are covered here too — and so the
    substitute assertion is checked rather than merely offered.
    """

    separates_client_from_principal = False

    #: Unlike the base class, this one rotates refresh tokens — because conduct
    #: does, and the substitute assertion's "and keeps doing so across a
    #: refresh" clause is otherwise never exercised anywhere in this repo.
    refreshes: dict[str, tuple[Principal, str]] = field(default_factory=dict)

    async def exchange(
        self, *, client: Client, code: str, verifier: str, redirect_uri: str
    ) -> Tokens:
        issued = await super().exchange(
            client=client, code=code, verifier=verifier, redirect_uri=redirect_uri
        )
        principal, client_id = self.tokens[issued.access]
        refresh = secrets.token_hex(16)
        self.refreshes[refresh] = (principal, client_id)
        return Tokens(access=issued.access, refresh=refresh)

    async def refresh(self, *, client: Client, refresh_token: str) -> Tokens:
        found = self.refreshes.pop(refresh_token, None)  # rotation: single use
        if found is None:
            raise Refused("unknown or spent refresh token")
        principal, client_id = found
        access = secrets.token_hex(16)
        self.tokens[access] = (principal, client_id)
        rotated = secrets.token_hex(16)
        self.refreshes[rotated] = (principal, client_id)
        return Tokens(access=access, refresh=rotated)

    async def authorize(
        self,
        *,
        client: Client,
        principal: Principal,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        if principal != client.registrant:
            raise Refused("a grant here can only be for the client's own principal")
        return await super().authorize(
            client=client,
            principal=principal,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )


@pytest.fixture
async def one_to_one():
    return OneToOneOAuth()


class TestAOneToOneModelAlsoConforms(OAuthConformanceSuite):
    """The same assertions where client and principal cannot differ."""

    @pytest.fixture
    def oauth(self, one_to_one):
        return one_to_one


async def test_a_one_to_one_model_refuses_a_foreign_principal(one_to_one) -> None:
    """The property that makes deriving sound in the first place. If a client
    *could* be made to act for someone else, `separates_client_from_principal =
    False` would be a false claim and the skipped assertions would be hiding a
    real defect."""
    client = await one_to_one.register_client(redirect_uris=["https://example.test/callback"])
    stranger = await one_to_one.make_principal()

    with pytest.raises(Refused):
        await one_to_one.authorize(
            client=client,
            principal=stranger,
            redirect_uri="https://example.test/callback",
            code_challenge="x",
        )
