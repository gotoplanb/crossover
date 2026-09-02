"""A behavioural contract for the OAuth authorization servers in this fleet.

Four services here speak OAuth to serve an MCP endpoint, and three of them —
`crossover`, `conduct`, and the `bosshardtrealtors` brokerage portal — have
hand-rolled the same authorization server independently. They share a design and
no code, which is exactly the situation where implementations drift apart
without anyone noticing.

They already had. `conduct`'s principal is a machine identity, so deriving a
token's principal from its client record is correct there: the client and the
principal are the same entity. `crossover` ported that design and changed the
principal to a human, which makes the relationship one-to-many — and inherited
the derivation unchanged. It never became wrong; it was only ever valid under an
assumption the port silently dropped. Nothing caught it, because each codebase
is internally consistent and reading either one alone tells you nothing.

That is what this is for. It is a suite of assertions, not a library: the three
services use two ORMs and two web frameworks and will share a *design* far more
readily than they will share code. Each supplies a small adapter
(`contract.OAuthImplementation`) and runs the same questions against its own
implementation.

See `docs/oauth-contract.md` for the rules in prose, and `suite.py` for the
executable form. The rule worth reading first:

    The principal is an explicit input to the grant. It is never derived from
    the client record. Whether the principal equals the consenting actor is
    app policy — self-grant or delegation.

Stated that way it holds for all three: `crossover` and the portal self-grant,
`conduct` delegates to a machine, and both pass or fail for reasons that survive
a change in what a principal is.
"""

from oauth_conformance.contract import (
    Client,
    OAuthImplementation,
    Principal,
    Tokens,
)

__all__ = ["Client", "OAuthImplementation", "Principal", "Tokens"]
