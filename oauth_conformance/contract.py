"""What an implementation must expose to be checked.

Deliberately small. Every method here is something all three services already
do; nothing has been added to make the suite convenient. If an adapter cannot be
written without changing the service, the suite is asking the wrong question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Principal:
    """Whoever a token acts as.

    `kind` is the axis the fleet disagrees on — "human" in crossover and the
    portal, "machine" in conduct — and the contract is written so that
    disagreement does not matter. An implementation may put anything in `id`
    that it can hand back to `resolve`.
    """

    kind: str
    id: Any

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Principal):
            return NotImplemented
        return self.kind == other.kind and str(self.id) == str(other.id)

    def __hash__(self) -> int:
        return hash((self.kind, str(self.id)))


@dataclass(frozen=True)
class Client:
    """A registered client. `secret` is None for a public client (PKCE only)."""

    id: str
    secret: str | None = None
    redirect_uris: list[str] = field(default_factory=list)
    #: Whoever registered it. Present so the suite can prove a token does NOT
    #: resolve to this — see `test_the_grant_binds_to_the_approver`.
    registrant: Principal | None = None


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str | None = None


@runtime_checkable
class OAuthImplementation(Protocol):
    """One service's authorization server, as the suite needs to poke it.

    Async throughout because two of the three are; a synchronous
    implementation wraps its calls.
    """

    #: Whether this service's model lets one client act for more than one
    #: principal.
    #:
    #: This exists because the first draft of the contract was wrong. It said
    #: the principal must never be derived from the client record — which would
    #: have failed `conduct`, whose principal *is* its client: `OAuthClient` has
    #: a foreign key to exactly one `ClientApp`, so a client there cannot serve
    #: two principals however hard you push. Deriving is not a defect when the
    #: two are provably one-to-one; it became one in `crossover` only because
    #: the port made the relationship one-to-many and kept the derivation.
    #:
    #: False makes the suite skip the assertions that are structurally
    #: meaningless — there is no second principal to confuse — and run a
    #: narrower substitute instead. It is not a way to opt out of the rule: an
    #: implementation that *can* separate them and doesn't will still fail.
    separates_client_from_principal: bool = True

    async def make_principal(self, kind: str = "human") -> Principal:
        """Create an identity a token could act as."""

    async def register_client(
        self, *, redirect_uris: list[str], registrant: Principal | None = None
    ) -> Client:
        """Register a client. `registrant` is who *owns* it, which the contract
        insists is a different question from who its tokens act as."""

    async def authorize(
        self,
        *,
        client: Client,
        principal: Principal,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        """Issue an authorization code for `principal`. Returns the raw code.

        This is where the central invariant lives: the principal is an argument.
        An implementation that ignores it and reads the client's owner instead
        will fail the suite, which is the whole point.
        """

    async def exchange(
        self, *, client: Client, code: str, verifier: str, redirect_uri: str
    ) -> Tokens:
        """Redeem a code. Raises on any failure; the suite only checks that it
        did fail, never how."""

    async def refresh(self, *, client: Client, refresh_token: str) -> Tokens:
        """Exchange a refresh token for a new pair."""

    async def resolve(self, access_token: str) -> Principal | None:
        """The resource-server check: token -> principal, or None."""

    async def deactivate_client(self, client: Client) -> None:
        """Turn a client off. Existing tokens must stop working."""
