"""CSRF protection for the cookie-authenticated web surface.

The threat is narrow and specific: a page on another site causing the reader's
browser to POST here, with the session cookie attached, and the app treating it
as an intentional act. `/oauth/authorize` is the one that matters most — that
POST grants a connector access to a whole reading list.

**Applied as a router-level dependency, not per-route.** The point of #18 was
that a form added later must not be able to silently omit protection, so the
check hangs off the router and covers endpoints nobody has written yet. Adding
an exemption is then a deliberate, visible act — and `tests/integration/
test_csrf.py` asserts the exemption list stays minimal.

Two sources for the expected token, one rule: **the session's token when signed
in, the pre-auth cookie when not.**

- Signed in, the token lives on the `user_sessions` row. Nothing a cross-site
  page can read or set, and it dies with the session.
- The login form has no session yet, so it uses a double-submit cookie issued
  when the form is rendered. Weaker in general — anything that can set a cookie
  for this host can forge it — but `herokuapp.com` is on the Public Suffix List,
  so a sibling app cannot write cookies for our host.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import SESSION_COOKIE, _hash
from db.session import get_session
from models.session import UserSession

#: Hidden field name in every form, and the header HTMX sends.
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

#: Double-submit cookie, used only before a session exists.
CSRF_COOKIE = "crossover_csrf"

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths that authenticate with something other than a cookie, so a cross-site
#: request gains nothing by being sent. Every entry needs a reason, and the test
#: suite asserts this list does not grow silently.
#:
#: - /oauth/token: authenticated by client_id + client_secret, called by a
#:   server, never a browser form.
#: - /mcp: authenticated by an OAuth bearer token in a header, which a
#:   cross-site page cannot set on a simple request.
EXEMPT_PREFIXES = ("/oauth/token", "/mcp")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _submitted(request: Request, form) -> str:
    return (form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER) or "").strip()


async def _expected_token(request: Request, session: AsyncSession) -> str | None:
    """The token this request must present, or None when there is nothing to protect.

    None means the request carries no authenticated context a cross-site page
    could abuse: no live session, and no login form in flight. The route's own
    auth then rejects it — which is better than a bare 403, because a reader
    whose session expired mid-form should land on the login page rather than an
    error.

    That is safe precisely because an attacker cannot manufacture the context
    they lack: every path returning None here is also a path where
    `resolve_session` returns no user, so no state-changing handler will act.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        row = await session.scalar(
            select(UserSession).where(UserSession.token_hash == _hash(cookie))
        )
        if row is not None and not row.revoked and row.csrf_token:
            return row.csrf_token
        # An unknown, revoked or token-less session is not authenticated, so
        # there is nothing to forge. Fall through to the route's auth check.
        return None
    # A login form in flight: no session yet, so the token is double-submitted
    # from the cookie issued when the page was rendered.
    return request.cookies.get(CSRF_COOKIE)


async def csrf_protect(
    request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """Reject an unsafe request that cannot prove it came from our own page.

    Takes the request-scoped session rather than opening its own: the lookup
    belongs in the same transaction as the handler that follows, and a second
    connection per request would be wasted on an eco dyno.

    Stashes the expected token on `request.state` on the way through, so
    templates can render it into forms without a second lookup.
    """
    expected = await _expected_token(request, session)
    request.state.csrf_token = expected or ""

    if request.method not in UNSAFE_METHODS:
        return
    if any(request.url.path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
        return

    if expected is None:
        # Nothing authenticated to protect — see _expected_token.
        return

    form = await request.form()
    submitted = _submitted(request, form)
    # compare_digest on two empty strings matches happily, so an empty token on
    # either side has to fail before any comparison is reached.
    if not expected or not submitted or not hmac.compare_digest(submitted, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "CSRF check failed. Reload the page and try again — this usually "
                "means the form was stale or the request did not come from here."
            ),
        )


def token_for(request: Request) -> str:
    """The token to render into a form. Empty when there is nothing to protect."""
    return getattr(request.state, "csrf_token", "") or ""


