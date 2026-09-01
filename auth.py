"""Admin-session helpers for the web surface.

The MCP surface authenticates with OAuth bearer tokens (oauth_provider.py).
The web surface is a two-person deployment behind a single admin key — the
curation views are explicitly allowed to stay ugly (SPEC §7), and that extends
to their auth.
"""

from __future__ import annotations

import hmac

from config.settings import get_settings

ADMIN_COOKIE = "crossover_admin"


def admin_cookie_valid(cookie: str | None) -> bool:
    """Constant-time comparison of the admin cookie against the configured key.

    `hmac.compare_digest` rather than `==`: the cookie is attacker-controlled
    and a short-circuiting comparison leaks the key one character at a time.
    """
    if not cookie:
        return False
    return hmac.compare_digest(cookie, get_settings().admin_key)
