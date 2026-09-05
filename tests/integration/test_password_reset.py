"""Admin-issued password reset links.

Nothing here sends email, so recovery is an admin minting a link and handing it
over by whatever channel they already use. That makes the link itself the whole
credential — a working one is a full account takeover, sharper than anything
else passed around here — so the properties worth pinning are the ones that
keep it from being reusable, guessable, or survivable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from auth import (
    RESET_TOKEN_PREFIX,
    authenticate,
    consume_reset_token,
    create_reset_token,
    create_session,
    hash_password,
    resolve_reset_token,
    resolve_session,
)
from models.reset import PasswordResetToken

NEW_PASSWORD = "a-brand-new-passphrase"  # pragma: allowlist secret


async def test_a_link_sets_the_password_and_signs_them_in(client, session, user) -> None:
    raw = await create_reset_token(session, user)
    assert raw.startswith(RESET_TOKEN_PREFIX), "prefixed so a leak is recognisable"

    page = (await client.get(f"/ui/reset/{raw}")).text
    assert "Set a new password" in page

    import re

    from csrf import CSRF_FIELD

    token = re.search(rf'name="{CSRF_FIELD}" value="([^"]*)"', page).group(1)
    response = await client.post(
        f"/ui/reset/{raw}",
        data={
            CSRF_FIELD: token,
            "password": NEW_PASSWORD,
            "password_confirm": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/rack"
    assert (await client.get("/ui/rack")).status_code == 200

    await session.refresh(user)
    assert await authenticate(session, user.handle, NEW_PASSWORD) is not None


async def test_using_a_link_signs_them_out_everywhere_else(session, user) -> None:
    """The property worth insisting on. Whoever prompted the reset may be
    holding a live cookie — a reset that leaves it working is not a reset."""
    stolen = await create_session(session, user)
    assert await resolve_session(session, stolen) is not None

    raw = await create_reset_token(session, user)
    assert await consume_reset_token(session, raw, NEW_PASSWORD) is not None

    assert await resolve_session(session, stolen) is None


async def test_a_link_works_exactly_once(session, user) -> None:
    raw = await create_reset_token(session, user)
    assert await consume_reset_token(session, raw, NEW_PASSWORD) is not None
    assert await consume_reset_token(session, raw, "another-passphrase-here") is None
    # The first password stands.
    assert await authenticate(session, user.handle, NEW_PASSWORD) is not None


async def test_issuing_a_new_link_retires_the_previous_one(session, user) -> None:
    """Two live links for one account means the older keeps working after the
    newer has been handed over, which nobody notices until it matters."""
    first = await create_reset_token(session, user)
    second = await create_reset_token(session, user)

    assert await resolve_reset_token(session, first) is None
    assert await resolve_reset_token(session, second) is not None


async def test_an_expired_link_is_refused(session, user) -> None:
    raw = await create_reset_token(session, user)
    row = await session.scalar(select(PasswordResetToken))
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    assert await resolve_reset_token(session, raw) is None
    assert await consume_reset_token(session, raw, NEW_PASSWORD) is None


async def test_a_spent_link_is_kept_rather_than_deleted(session, user) -> None:
    """So a replayed link is distinguishable in the table from one that never
    existed — which is the difference between a stale text message and an
    attack."""
    raw = await create_reset_token(session, user)
    await consume_reset_token(session, raw, NEW_PASSWORD)

    row = await session.scalar(select(PasswordResetToken))
    assert row is not None and row.used_at is not None


async def test_the_raw_token_is_never_stored(session, user) -> None:
    raw = await create_reset_token(session, user)
    row = await session.scalar(select(PasswordResetToken))
    assert raw not in row.token_hash
    assert len(row.token_hash) == 64, "sha256 hex, like every other token here"


async def test_who_issued_it_is_recorded(session, user, other_user) -> None:
    """ "Who let this person back in" is worth being able to answer later."""
    raw = await create_reset_token(session, other_user, issued_by=user)
    assert raw
    row = await session.scalar(select(PasswordResetToken))
    assert row.user_id == other_user.id
    assert row.issued_by == user.id


async def test_a_deactivated_reader_cannot_be_reset_back_in(session, user) -> None:
    raw = await create_reset_token(session, user)
    user.is_active = False
    await session.commit()
    assert await resolve_reset_token(session, raw) is None


@pytest.mark.parametrize("raw", ["xo_rst_nonsense", "", "not-even-prefixed"])
async def test_a_bogus_token_resolves_to_nobody(session, raw) -> None:
    assert await resolve_reset_token(session, raw) is None


async def test_a_short_password_is_refused_and_the_link_survives(session, user) -> None:
    """Rejecting the password must not burn the link — otherwise a typo costs
    the reader another round trip through their admin."""
    raw = await create_reset_token(session, user)
    assert await consume_reset_token(session, raw, "short") is None
    assert await resolve_reset_token(session, raw) is not None


async def test_a_dead_link_explains_itself_rather_than_404ing(client, session, user) -> None:
    """Somebody holding a URL they were handed cannot tell a typo from an
    expiry from a link already used, and "not found" answers none of those."""
    raw = await create_reset_token(session, user)
    await consume_reset_token(session, raw, NEW_PASSWORD)

    response = await client.get(f"/ui/reset/{raw}")
    assert response.status_code == 410
    assert "invalid, expired, or already used" in response.text
    assert "Ask whoever runs this for a new link" in response.text
    assert 'name="password"' not in response.text, "no form on a dead link"


async def test_mismatched_passwords_are_refused_over_http(client, session, user) -> None:
    import re

    from csrf import CSRF_FIELD

    raw = await create_reset_token(session, user)
    page = (await client.get(f"/ui/reset/{raw}")).text
    token = re.search(rf'name="{CSRF_FIELD}" value="([^"]*)"', page).group(1)

    response = await client.post(
        f"/ui/reset/{raw}",
        data={CSRF_FIELD: token, "password": NEW_PASSWORD, "password_confirm": "different"},
    )
    assert response.status_code == 400
    assert "match" in response.text
    assert await resolve_reset_token(session, raw) is not None, "link survives a typo"


async def test_a_reset_replaces_an_existing_password(session, user) -> None:
    user.password_hash = hash_password("the-original-passphrase")
    await session.commit()

    raw = await create_reset_token(session, user)
    await consume_reset_token(session, raw, NEW_PASSWORD)

    assert await authenticate(session, user.handle, "the-original-passphrase") is None
    assert await authenticate(session, user.handle, NEW_PASSWORD) is not None


async def _csrf(client, raw: str) -> str:
    import re

    from csrf import CSRF_FIELD

    page = (await client.get(f"/ui/reset/{raw}")).text
    match = re.search(rf'name="{CSRF_FIELD}" value="([^"]*)"', page)
    return match.group(1)


async def test_posting_to_a_dead_link_explains_itself_too(client, session, user) -> None:
    """The GET path already does; a form submitted a minute too late has to as
    well, or the explanation depends on which request happened to arrive."""
    from csrf import CSRF_FIELD

    raw = await create_reset_token(session, user)
    token = await _csrf(client, raw)
    await consume_reset_token(session, raw, NEW_PASSWORD)

    response = await client.post(
        f"/ui/reset/{raw}",
        data={
            CSRF_FIELD: token,
            "password": "another-passphrase",
            "password_confirm": "another-passphrase",
        },
    )
    assert response.status_code == 410
    assert "invalid, expired, or already used" in response.text


async def test_a_short_password_over_http_keeps_the_form(client, session, user) -> None:
    from csrf import CSRF_FIELD

    raw = await create_reset_token(session, user)
    token = await _csrf(client, raw)
    response = await client.post(
        f"/ui/reset/{raw}",
        data={CSRF_FIELD: token, "password": "short", "password_confirm": "short"},
    )
    assert response.status_code == 400
    assert "at least" in response.text.lower()
    assert 'name="password"' in response.text, "the form comes back, not a dead end"
    assert await resolve_reset_token(session, raw) is not None


async def test_a_link_consumed_mid_request_is_reported_not_crashed(
    client, session, user, monkeypatch
) -> None:
    """Two people opening the same link at once: one wins the consume and the
    other must be told the link is spent rather than seeing a 500. Only
    reachable as a race, so it is simulated — the alternative is a branch
    nobody has ever executed guarding the case that matters."""
    from csrf import CSRF_FIELD

    raw = await create_reset_token(session, user)
    token = await _csrf(client, raw)

    async def lost_the_race(*args, **kwargs):
        return None

    monkeypatch.setattr("routes.ui.consume_reset_token", lost_the_race)
    response = await client.post(
        f"/ui/reset/{raw}",
        data={CSRF_FIELD: token, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )
    assert response.status_code == 410
    assert "invalid, expired, or already used" in response.text
