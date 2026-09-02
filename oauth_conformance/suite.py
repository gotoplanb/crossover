"""The questions every implementation in the fleet has to answer the same way.

Subclass `OAuthConformanceSuite` in a service's own test tree and provide an
`oauth` fixture returning that service's `OAuthImplementation` adapter. Nothing
here imports any service, so the same file runs against a Django ORM and a
SQLAlchemy one without knowing which it is looking at.

These are behavioural assertions, not a specification of internals. Where an
implementation is expected to *fail*, the suite checks only that it did — never
how, since the three services raise three different exception types and that is
none of the contract's business.
"""

from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode

import pytest

REDIRECT = "https://example.test/callback"
OTHER_REDIRECT = "https://example.test/other"
VERIFIER = "v" * 64
OTHER_VERIFIER = "w" * 64


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode()


CHALLENGE = challenge_for(VERIFIER)


class OAuthConformanceSuite:
    """Run against one implementation via an `oauth` fixture."""

    # --- the invariant this whole exercise exists for ------------------------

    async def test_the_grant_binds_to_the_approver_not_the_registrant(self, oauth) -> None:
        """The principal is an explicit input to the grant, never derived from
        the client record.

        This is the defect that motivated the contract. `crossover` derived a
        token's principal from `client.user_id` — whoever *registered* the
        connector — which is correct only when the client and the principal are
        the same entity, as they are in `conduct`. Change the principal to a
        human and the relationship becomes one-to-many, and one person's client
        starts reading another person's data.
        """
        registrant = await oauth.make_principal()
        approver = await oauth.make_principal()
        assert registrant != approver, "adapter must mint distinct principals"

        client = await oauth.register_client(redirect_uris=[REDIRECT], registrant=registrant)
        code = await oauth.authorize(
            client=client, principal=approver, redirect_uri=REDIRECT, code_challenge=CHALLENGE
        )
        tokens = await oauth.exchange(
            client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT
        )

        assert await oauth.resolve(tokens.access) == approver, (
            "the token acted as the client's registrant instead of the approver"
        )

    async def test_refresh_preserves_the_principal(self, oauth) -> None:
        """The same bug with a longer fuse. Deriving the principal again at
        refresh time re-binds a rotated token to the registrant — a day later,
        far from anything that would suggest a cause."""
        registrant = await oauth.make_principal()
        approver = await oauth.make_principal()
        client = await oauth.register_client(redirect_uris=[REDIRECT], registrant=registrant)
        code = await oauth.authorize(
            client=client, principal=approver, redirect_uri=REDIRECT, code_challenge=CHALLENGE
        )
        first = await oauth.exchange(
            client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT
        )
        if first.refresh is None:
            pytest.skip("implementation issues no refresh tokens")

        second = await oauth.refresh(client=client, refresh_token=first.refresh)
        assert await oauth.resolve(second.access) == approver

    async def test_one_client_serves_many_principals(self, oauth) -> None:
        """The practical consequence, and the reason the rule is worth having:
        a single registered client should serve everyone, each with their own
        separately-revocable token."""
        client = await oauth.register_client(redirect_uris=[REDIRECT])
        first, second = await oauth.make_principal(), await oauth.make_principal()

        issued = []
        for principal in (first, second):
            code = await oauth.authorize(
                client=client,
                principal=principal,
                redirect_uri=REDIRECT,
                code_challenge=CHALLENGE,
            )
            issued.append(
                await oauth.exchange(
                    client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT
                )
            )

        assert issued[0].access != issued[1].access
        assert await oauth.resolve(issued[0].access) == first
        assert await oauth.resolve(issued[1].access) == second

    # --- authorization codes -------------------------------------------------

    async def test_a_code_is_single_use(self, oauth) -> None:
        """A replayed code is the classic interception attack."""
        client, code = await self._granted(oauth)
        await oauth.exchange(client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT)
        with pytest.raises(Exception):  # noqa: B017,PT011 — any refusal will do
            await oauth.exchange(client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT)

    async def test_a_code_is_bound_to_the_client_it_was_issued_to(self, oauth) -> None:
        """Two clients on one deployment: A's code must be worthless to B."""
        # The client the code was issued to is irrelevant here — what matters
        # is that a *different* one cannot spend it.
        _issued_to, code = await self._granted(oauth)
        other = await oauth.register_client(redirect_uris=[REDIRECT])
        with pytest.raises(Exception):  # noqa: B017,PT011
            await oauth.exchange(client=other, code=code, verifier=VERIFIER, redirect_uri=REDIRECT)

    async def test_the_redirect_uri_must_match_the_one_the_code_was_issued_for(self, oauth) -> None:
        client, code = await self._granted(oauth)
        with pytest.raises(Exception):  # noqa: B017,PT011
            await oauth.exchange(
                client=client, code=code, verifier=VERIFIER, redirect_uri=OTHER_REDIRECT
            )

    # --- PKCE ----------------------------------------------------------------

    async def test_a_wrong_verifier_is_refused(self, oauth) -> None:
        client, code = await self._granted(oauth)
        with pytest.raises(Exception):  # noqa: B017,PT011
            await oauth.exchange(
                client=client, code=code, verifier=OTHER_VERIFIER, redirect_uri=REDIRECT
            )

    async def test_plain_pkce_can_never_produce_a_token(self, oauth) -> None:
        """`plain` makes PKCE decorative: anyone who intercepts the request gets
        the verifier for free.

        Deliberately phrased as "cannot produce a token" rather than "is
        refused at authorize". Writing the first adapter showed why: crossover
        rejects the method at its HTTP layer while its provider will still mint
        a code that `verify_pkce` then refuses to redeem. Both are safe, and
        which layer says no is an implementation choice this contract has no
        business dictating. What it may insist on is that no token comes out.
        """
        client = await oauth.register_client(redirect_uris=[REDIRECT])
        principal = await oauth.make_principal()
        with pytest.raises(Exception):  # noqa: B017,PT011
            code = await oauth.authorize(
                client=client,
                principal=principal,
                redirect_uri=REDIRECT,
                code_challenge=VERIFIER,
                code_challenge_method="plain",
            )
            # Refused later rather than sooner is still refused.
            await oauth.exchange(client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT)

    # --- tokens --------------------------------------------------------------

    async def test_rotation_retires_the_old_refresh_token(self, oauth) -> None:
        """Without rotation a leaked refresh token is good until it expires,
        and nothing reveals that it leaked."""
        client, code = await self._granted(oauth)
        first = await oauth.exchange(
            client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT
        )
        if first.refresh is None:
            pytest.skip("implementation issues no refresh tokens")

        await oauth.refresh(client=client, refresh_token=first.refresh)
        with pytest.raises(Exception):  # noqa: B017,PT011
            await oauth.refresh(client=client, refresh_token=first.refresh)

    async def test_deactivating_a_client_stops_its_existing_tokens(self, oauth) -> None:
        """The kill switch has to take effect on the next resource call, not at
        the next expiry — otherwise revoking is a request to please stop."""
        client, code = await self._granted(oauth)
        tokens = await oauth.exchange(
            client=client, code=code, verifier=VERIFIER, redirect_uri=REDIRECT
        )
        assert await oauth.resolve(tokens.access) is not None

        await oauth.deactivate_client(client)
        assert await oauth.resolve(tokens.access) is None

    async def test_an_unknown_token_resolves_to_nobody(self, oauth) -> None:
        assert await oauth.resolve("definitely-not-a-token") is None
        assert await oauth.resolve("") is None

    # --- helper --------------------------------------------------------------

    @staticmethod
    async def _granted(oauth):
        client = await oauth.register_client(redirect_uris=[REDIRECT])
        principal = await oauth.make_principal()
        code = await oauth.authorize(
            client=client, principal=principal, redirect_uri=REDIRECT, code_challenge=CHALLENGE
        )
        return client, code
