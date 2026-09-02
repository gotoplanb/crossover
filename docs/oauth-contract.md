# The fleet's OAuth contract

Four services here speak OAuth to serve an MCP endpoint. Three of them —
`crossover`, `conduct`, and the `bosshardtrealtors` brokerage portal — have
hand-rolled the same authorization server independently: the same four tables,
the same PKCE-on-the-code, the same single-use codes, the same rotation on
refresh, the same revocation check on every resource call. They share a design
and no code.

That is the situation where implementations drift without anyone noticing, and
they already had.

## The divergence that motivated this

`conduct`'s principal is a **machine**. `ClientApp` is a pure machine identity —
name, api key hash, rate limit, no owner — and `conduct` has no `User` model at
all. So deriving a token's principal from its client record is *correct* there:
the client and the principal are the same entity, one to one.

`crossover` ported that design and changed the principal to a **human**. That
makes the relationship one to many — many readers, one connector — but the
derivation came across unchanged. It never became wrong; it was only ever valid
under an assumption the port silently dropped.

Nothing caught it. Each codebase is internally consistent, and reading either
one in isolation tells you nothing. It surfaced only when a connector needed to
serve more than one person, and it had a second form waiting in `refresh`, which
would have re-bound a rotated token to the registrant a day later — far enough
from the cause to be genuinely hard to diagnose.

## The rule

> **The principal is an explicit input to the grant. It is never derived from
> the client record.** Whether the principal equals the consenting actor is app
> policy — self-grant or delegation.

An earlier draft said "the grant binds to whoever consented", and that is wrong.
In `conduct` an admin approves and the token acts as a *machine*: the consenting
actor and the principal are different entities, and that is legitimate
delegation, not a defect. The rule as stated above holds for all three —
`crossover` and the portal self-grant, `conduct` delegates — and both pass or
fail for reasons that survive a change in what a principal *is*.

It also describes crossover's actual defect better than "bound to the wrong
person" did. The principal was never an *input*, so it could not be anything
other than what the connector implied.

## Why a contract and not a library

The obvious move is one shared package. The fleet argues against it: two ORMs
(Django and SQLAlchemy) and two web frameworks, so the three services will share
a *design* far more readily than they will share code. And the bug this is
chasing was behavioural — a library would have prevented it only if every
service actually adopted the library, whereas a suite catches divergence between
implementations that share nothing.

So the deliverable is `oauth_conformance/`: a small adapter protocol
(`contract.py`) and a set of assertions (`suite.py`) that import no service.
Each repo writes an adapter and answers the same questions.

If the three implementations turn out to agree everywhere except where they
should, extracting a library becomes a mechanical follow-up with a test suite
already in place. If they disagree, that finding is worth more than the
extraction.

## What is in scope

The contract asserts behaviour that does not depend on what a principal is:

- **Principal binding.** A token resolves to the approver, not to the client's
  registrant. One client serves many principals, each separately revocable.
- **Refresh preserves the principal**, and rotation retires the old refresh
  token.
- **Codes are single-use**, bound to their client, and bound to the redirect
  uri they were issued for.
- **PKCE.** A wrong verifier is refused, and `plain` can never produce a token.
- **Revocation takes effect on the next resource call**, not at the next
  expiry.
- **An unknown token resolves to nobody.**

## What is deliberately out of scope

- **The consent UI.** Every service renders it differently and should.
- **Who may consent, and for whom.** The portal uses `@login_required`;
  crossover accepts any active reader; conduct requires an admin approving on a
  machine's behalf. That is policy.
- **Sessions and login.** The contract takes an already-resolved principal.
- **Storage.** Django ORM and SQLAlchemy will not share models.
- **TTLs.** Worth agreeing on (24h access / 90d refresh is where crossover and
  the portal both landed) but not a correctness property.
- **Which layer refuses.** Writing the first adapter made this concrete:
  crossover rejects `plain` at its HTTP layer while its provider will still mint
  a code that `verify_pkce` then refuses to redeem. Both are safe. The contract
  asserts that no token comes out, not where the refusal happens.

## Running it

```python
# tests/conformance/test_<service>_conforms.py
class MyOAuth:  # implements oauth_conformance.OAuthImplementation
    ...

@pytest.fixture
async def oauth(session):
    return MyOAuth(session)

class TestMyServiceConforms(OAuthConformanceSuite):
    """Every shared assertion, against this service."""
```

`tests/conformance/test_crossover_conforms.py` is the worked example. The
adapter is the entire file; the assertions are shared.

## Status

| Service | Principal | Adapter | Result |
|---|---|---|---|
| crossover | `("human", user_id)` | yes | 11/11 |
| conduct | `("machine", client_app_id)` | not yet | — |
| brokerage portal | `("user", pk)` | not yet | — |
| davestanton.com | none | out of scope | read-only by design |

Only crossover is verified. The other two adapters are a file each and the
interesting part is what they turn up — particularly `conduct`, whose whole
value here is proving the contract really is indifferent to what a principal is
rather than merely claiming to be.

`davestanton.com` is excluded on purpose: it is read-only and anonymous, so
there is no principal to bind and the suite would mostly assert `N/A`. If it
ever grows writes, it adopts the contract then.
