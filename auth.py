"""Web-surface authentication.

The MCP surface authenticates with OAuth bearer tokens (oauth_provider.py).
The web surface is two people sharing one deployment, so it has two layers:

- **Each reader has their own password**, supplied as
  `CROSSOVER_PASSWORD_{HANDLE}`. This is what separates one person's rack from
  the other's; a shared key could not.
- **`CROSSOVER_ADMIN_KEY` gates the curation views and OAuth consent.** Readers
  flagged `is_admin` receive it on sign-in, so an admin never types two
  credentials and a non-admin cannot reach those surfaces at all.

Passwords are compared against plaintext held in config, not hashes. For a
household that is the same exposure the admin key already has. It is the wrong
answer for more people than that.
"""

from __future__ import annotations

import hmac

from config.settings import get_settings

ADMIN_COOKIE = "crossover_admin"


def verify_reader_password(handle: str, password: str) -> bool:
    """Check a reader's password in constant time.

    Returns False for an unknown handle or an unset password, and does so
    *after* a comparison against a dummy of the same shape, so a configured
    reader and an unconfigured one take the same path. Not a defence against a
    determined attacker — the login form is not rate limited — but it costs
    nothing and avoids the response time announcing which handles exist.
    """
    expected = get_settings().reader_password(handle)
    if not password:
        return False
    if expected is None:
        # Compare anyway so the timing does not distinguish "no such reader"
        # from "wrong password".
        hmac.compare_digest(password, password)
        return False
    return hmac.compare_digest(password, expected)


def admin_cookie_valid(cookie: str | None) -> bool:
    """Constant-time comparison of the admin cookie against the configured key.

    `hmac.compare_digest` rather than `==`: the cookie is attacker-controlled
    and a short-circuiting comparison leaks the key one character at a time.
    """
    if not cookie:
        return False
    return hmac.compare_digest(cookie, get_settings().admin_key)
