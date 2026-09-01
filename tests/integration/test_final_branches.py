"""The last unexercised branches.

Every one of these is a defensive path — a Marvel record for an issue nobody
curated, a truncated event with no core run, a malformed Basic auth header. They
exist because the alternative is a 500 on a page, and they are tested because a
defensive path that has never run is only a guess that it works.
"""

from __future__ import annotations

from base64 import b64encode
from dataclasses import replace

import pytest
from sqlalchemy import select

from auth import ADMIN_COOKIE
from config.settings import get_settings
from curation.export import export_event_yaml
from curation.loader import load_event
from curation.schema import load_all_events
from curation.validate import check_linkable_or_flagged
from marvel.sync import SyncReport
from models.catalog import Issue
from models.types import Availability, CurationStatus

# --- export: an event with no core run, and a dangling edge ---


async def test_exporting_an_event_with_no_core_run_omits_the_core_block(
    session, loaded_event
) -> None:
    """Reachable while an event is still being seeded from a tie-in list."""
    from models.catalog import EventIssue
    from models.types import IssueRole

    await session.execute(
        EventIssue.__table__.update().values(role=IssueRole.OPTIONAL_TIE_IN.value)
    )
    await session.commit()

    body = await export_event_yaml(session, "king-in-black")
    assert "core:" not in body


async def test_exporting_emits_availability_and_the_unavailable_note(
    session, loaded_event
) -> None:
    """An "explicitly not on Marvel Unlimited" call is curation work; losing it
    on export would silently downgrade it to "nobody checked"."""
    event = load_all_events()[0]
    flagged = replace(
        event,
        issues=[
            replace(
                i,
                availability=Availability.UNAVAILABLE,
                unavailable_note="print only, checked 2026-09",
            )
            if i.key == "king-in-black-1"
            else i
            for i in event.ordered
        ],
    )
    await load_event(session, flagged)

    body = await export_event_yaml(session, "king-in-black")
    assert "availability: unavailable" in body
    assert 'unavailable_note: "print only, checked 2026-09"' in body


# --- oauth HTTP error branches ---


async def test_an_unsupported_response_type_redirects_with_an_error(
    client, session, user
) -> None:
    from models.oauth import OAuthClient
    from oauth_provider import hash_secret, new_client_id, new_client_secret

    connector = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(new_client_secret()),
        name="phone",
        user_id=user.id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )
    session.add(connector)
    await session.commit()

    client.cookies.set(ADMIN_COOKIE, get_settings().admin_key)
    response = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "token",  # implicit flow — not supported
            "client_id": connector.client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "abc",
            "state": "s",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=unsupported_response_type" in response.headers["location"]


async def test_approving_without_an_admin_session_is_refused(client) -> None:
    response = await client.post(
        "/oauth/authorize",
        data={
            "client_id": "xoc_whatever",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "abc",
            "decision": "approve",
        },
    )
    assert response.status_code == 400
    assert "Admin session required" in response.text


async def test_approving_for_an_unknown_client_is_refused(client) -> None:
    client.cookies.set(ADMIN_COOKIE, get_settings().admin_key)
    response = await client.post(
        "/oauth/authorize",
        data={
            "client_id": "xoc_nope",
            "redirect_uri": "https://evil.example/cb",
            "code_challenge": "abc",
            "decision": "approve",
        },
    )
    assert response.status_code == 400
    assert "Unknown client_id" in response.text


async def test_a_malformed_basic_auth_header_is_rejected_not_crashed(client) -> None:
    """Non-UTF-8 bytes in Basic auth would otherwise raise inside the parser and
    return a 500, which tells an attacker more than a 401 does."""
    bad = b64encode(b"\xff\xfe\x00not-utf8").decode()
    response = await client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code"},
        headers={"Authorization": f"Basic {bad}"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


async def test_the_refresh_grant_over_http(client, session, user) -> None:
    """The branch the happy-path test skips — `grant_type=refresh_token`."""
    import hashlib
    import re
    from base64 import urlsafe_b64encode

    from models.oauth import OAuthClient
    from oauth_provider import hash_secret, new_client_id, new_client_secret

    secret = new_client_secret()
    redirect = "https://claude.ai/api/mcp/auth_callback"
    connector = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(secret),
        name="phone",
        user_id=user.id,
        redirect_uris=[redirect],
    )
    session.add(connector)
    await session.commit()

    verifier = "q" * 64
    challenge = (
        urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    client.cookies.set(ADMIN_COOKIE, get_settings().admin_key)
    approved = await client.post(
        "/oauth/authorize",
        data={
            "client_id": connector.client_id,
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    code = re.search(r"code=([^&]+)", approved.headers["location"]).group(1)
    first = (
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "code_verifier": verifier,
                "client_id": connector.client_id,
                "client_secret": secret,
            },
        )
    ).json()

    refreshed = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": connector.client_id,
            "client_secret": secret,
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != first["access_token"]


# --- misc defensive branches ---


def test_sync_coverage_of_an_empty_roster_is_zero_not_a_divide_by_zero() -> None:
    report = SyncReport(
        event_slug="x",
        pages_fetched=1,
        records_seen=0,
        issues_matched=0,
        issues_unmatched=[],
        digital_ids_confirmed=0,
        newly_linkable=0,
    )
    assert report.digital_id_coverage == 0.0
    assert "0%" in report.summary()


def test_sync_summary_lists_unmatched_curated_issues() -> None:
    """Event tagging quality varies; a curated issue Marvel doesn't tag is a
    fact worth printing, not an error."""
    report = SyncReport(
        event_slug="king-in-black",
        pages_fetched=1,
        records_seen=10,
        issues_matched=8,
        issues_unmatched=["venom-31", "venom-32"],
        digital_ids_confirmed=9,
        newly_linkable=8,
    )
    summary = report.summary()
    assert "unmatched curated issues" in summary
    assert "venom-31, venom-32" in summary
    assert "WARNING" not in summary


async def test_provenance_is_empty_for_an_issue_with_no_id(session) -> None:
    """Reachable for a pending shelf entry, which has no resolved issue yet."""
    from service import bookmarks as bookmark_service
    from service.guide import GuideEntry

    entry = GuideEntry(
        key="k",
        position=0,
        series_name="X",
        issue_number=1,
        title="",
        published_on=None,
        role="",
        narrative_role="",
        franchise="",
        note="",
        availability=Availability.UNCONFIRMED.value,
        provisional=False,
        digital_id=None,
        source_id=None,
        issue_id=None,
    )
    assert await bookmark_service.provenance_for(session, entry) == (None, "")


def test_a_guide_entry_reports_why_it_is_unavailable() -> None:
    """The rack and the tools both need the reason, not just the absence."""
    from service.guide import GuideEntry

    entry = GuideEntry(
        key="k",
        position=1,
        series_name="Obscure",
        issue_number=1,
        title="",
        published_on=None,
        role="optional_tie_in",
        narrative_role="parallel",
        franchise="other",
        note="",
        availability=Availability.UNAVAILABLE.value,
        provisional=False,
        digital_id=None,
        source_id=None,
        unavailable_note="print only",
    )
    payload = entry.to_payload()
    assert payload["why_unavailable"] == "print only"
    assert payload["link"] == "not on Marvel Unlimited"


def test_an_unavailable_issue_with_no_note_is_flagged() -> None:
    """A reader told only "not on Marvel Unlimited" with no reason has nothing
    to act on, so a curated `unavailable` must carry a note."""
    event = load_all_events()[0]
    silent = replace(
        replace(
            event,
            issues=[
                replace(
                    i,
                    availability=Availability.UNAVAILABLE,
                    unavailable_note="",
                    provisional=False,
                )
                for i in event.ordered
            ],
        ),
        curation_status=CurationStatus.CURATED,
    )
    problems = check_linkable_or_flagged(silent)
    assert any("no note" in str(p) for p in problems)


async def test_a_marvel_record_for_an_uncurated_issue_is_ignored(
    session, loaded_event, record_index
) -> None:
    """Marvel's roster contains issues nobody has curated. They must not be
    silently created as catalog rows with no reading order."""
    from marvel.sync import apply_record

    before = len((await session.scalars(select(Issue))).all())
    by_key = {i.key: i for i in (await session.scalars(select(Issue))).all()}
    for key, record in record_index.items():
        if key in by_key:
            apply_record(by_key[key], record)
    await session.commit()
    after = len((await session.scalars(select(Issue))).all())
    assert after == before


async def test_add_to_shelf_with_no_candidates_is_refused(session) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    import mcp_server

    with pytest.raises(ToolError, match="at least one candidate"):
        await mcp_server.add_to_shelf([])


async def test_the_oauth_gate_passes_non_http_scopes_through() -> None:
    """A lifespan or websocket scope must not be given a 401 — it has no
    response to send one on."""
    import mcp_server

    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    await mcp_server.OAuthMiddleware(inner)({"type": "lifespan"}, None, None)
    assert called
