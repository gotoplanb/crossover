"""The remaining branches: cross-client token theft, off-event finds, and the
seeding paths that only run on a fresh deploy.

None of these are exotic. They are the paths that only execute the first time
something happens — or the first time something goes wrong — which is exactly
when nobody is watching.
"""

from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from curation.loader import LoadReport, load_event
from curation.schema import CuratedReference, load_all_events
from marvel.client import BASE_URL, MarvelClient
from models.catalog import Event, Issue, IssueReference
from models.oauth import OAuthClient, OAuthToken
from models.types import Availability, RelationType, ShelfSource
from oauth_provider import (
    OAuthError,
    hash_secret,
    issue_authorization_code,
    new_client_id,
    new_client_secret,
    redeem_authorization_code,
    refresh_token_grant,
)
from service import shelf as shelf_service

VERIFIER = "z" * 64
CHALLENGE = (
    urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


# --- OAuth: one connector must not be able to use another's grant ---


async def _connector(session, user, name: str):
    secret = new_client_secret()
    client = OAuthClient(
        client_id=new_client_id(),
        client_secret_hash=hash_secret(secret),
        name=name,
        user_id=user.id,
        redirect_uris=[REDIRECT],
    )
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client, secret


async def test_a_code_cannot_be_redeemed_by_a_different_client(session, user) -> None:
    """Two connectors on one deployment: A's code must be worthless to B even
    though both belong to the same reader."""
    first, _ = await _connector(session, user, "phone")
    second, _ = await _connector(session, user, "laptop")
    code = await issue_authorization_code(
        session,
        client=first,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="mcp",
    )
    with pytest.raises(OAuthError, match="issued to a different client"):
        await redeem_authorization_code(
            session,
            client=second,
            code=code,
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
        )


async def test_a_refresh_token_cannot_be_used_by_a_different_client(
    session, user
) -> None:
    first, _ = await _connector(session, user, "phone")
    second, _ = await _connector(session, user, "laptop")
    code = await issue_authorization_code(
        session,
        client=first,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="mcp",
    )
    token = await redeem_authorization_code(
        session, client=first, code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    with pytest.raises(OAuthError, match="belongs to another client"):
        await refresh_token_grant(
            session, client=second, refresh_token=token.raw_refresh_token
        )


async def test_an_expired_refresh_token_is_rejected(session, user) -> None:
    client, _ = await _connector(session, user, "phone")
    code = await issue_authorization_code(
        session,
        client=client,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="mcp",
    )
    token = await redeem_authorization_code(
        session, client=client, code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )
    row = await session.scalar(
        select(OAuthToken).where(OAuthToken.access_token_hash == token.access_token_hash)
    )
    row.refresh_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(OAuthError, match="refresh token has expired"):
        await refresh_token_grant(
            session, client=client, refresh_token=token.raw_refresh_token
        )


# --- loader: paths that only run on a fresh deploy or a partial sync ---


async def test_a_curated_marvel_com_id_seeds_the_source_id(session) -> None:
    """Gate C: the Branch `sourceId` is the marvel.com issue id, so a curated
    marvel_com_issue_id has to populate both columns or one-tap links would be
    unbuildable even when the id is known."""
    event = load_all_events()[0]
    seeded = replace(
        event,
        issues=[
            replace(i, marvel_com_issue_id=86133) if i.key == "king-in-black-1" else i
            for i in event.ordered
        ],
    )
    await load_event(session, seeded)

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert issue.marvel_com_issue_id == 86133
    assert issue.source_id == 86133


async def test_a_linkable_claim_without_an_id_is_downgraded_on_load(session) -> None:
    """Belt and braces behind the validation gate: even if a `linkable` entry
    with no digital_id reached the loader, the DB must not claim a link."""
    event = load_all_events()[0]
    lying = replace(
        event,
        issues=[
            replace(i, availability=Availability.LINKABLE)
            if i.key == "king-in-black-1"
            else i
            for i in event.ordered
        ],
    )
    await load_event(session, lying)

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert issue.digital_id is None
    assert issue.availability == Availability.UNCONFIRMED.value


async def test_a_curated_marvel_event_id_is_stored(session) -> None:
    """Without it, `sync-event` has no endpoint to call."""
    event = load_all_events()[0]
    await load_event(session, replace(event, marvel_event_id=860))
    stored = await session.scalar(select(Event).where(Event.slug == event.slug))
    assert stored.marvel_event_id == 860


async def test_an_edge_pointing_outside_the_event_is_skipped_not_fatal(
    session, caplog
) -> None:
    """The data-quality suite turns this into a test failure; at boot, one bad
    edge must not stop the whole guide from loading."""
    event = load_all_events()[0]
    broken = replace(
        event,
        references=[
            *event.references,
            CuratedReference("king-in-black-1", "not-in-this-event-1", RelationType.REFERENCES),
        ],
    )
    report = await load_event(session, broken)
    assert report.references == len(event.references)
    assert "skipping reference" in caplog.text

    edges = (await session.scalars(select(IssueReference))).all()
    assert len(edges) == len(event.references)


def test_the_load_report_reads_as_a_sentence() -> None:
    report = LoadReport(
        events=1, issues_created=40, issues_updated=0, memberships=40, references=15
    )
    summary = report.summary()
    assert "loaded 1 event(s)" in summary
    assert "40 new issues" in summary
    assert "15 reference edges" in summary


# --- shelf: the off-event find ---


@respx.mock
async def test_confirming_an_off_event_find_creates_the_issue(
    session, user, loaded_event
) -> None:
    """A comic-shop find may belong to no curated event at all (SPEC §6), so
    `confirm` has to be able to create the issue record itself — carrying only
    the digital_id that a Marvel response actually returned for it."""
    payload = {
        "attributionText": "Data provided by Marvel. © 2026 MARVEL",
        "data": {
            "total": 1,
            "results": [
                {
                    "id": 90210,
                    "digitalId": 61234,
                    "title": "Fantastic Four (2018) #52",
                    "issueNumber": 52,
                    "series": {"name": "Fantastic Four (2018 - 2022)"},
                    "dates": [{"type": "onsaleDate", "date": "2023-03-01T00:00:00-0500"}],
                    "urls": [
                        {"type": "detail", "url": "http://marvel.com/comics/issue/90210/x"}
                    ],
                    "thumbnail": {"path": "http://i.annihil.us/x", "extension": "jpg"},
                    "characters": {"items": [{"name": "Mister Fantastic"}]},
                    "creators": {"items": []},
                }
            ],
        },
    }
    respx.get(f"{BASE_URL}/comics").mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient() as http:
        proposed = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Fantastic Four 52"],
            source=ShelfSource.PHOTO,
            client=MarvelClient("pub", "priv", client=http),
        )
    entry = proposed["results"][0]
    assert entry["matches"][0]["key"] == "fantastic-four-52"

    result = await shelf_service.confirm(
        session,
        user_id=user.id,
        candidate_id=entry["candidate_id"],
        chosen_key="fantastic-four-52",
        note="saw it on the rack",
    )
    assert result["saved"] == "Fantastic Four #52"

    created = await session.scalar(select(Issue).where(Issue.key == "fantastic-four-52"))
    assert created is not None
    assert created.digital_id == 61234
    assert created.availability == Availability.LINKABLE.value
    assert created.series_slug == "fantastic-four"


@respx.mock
async def test_an_off_event_find_with_no_digital_id_stays_unlinkable(
    session, user, loaded_event
) -> None:
    """Gate B holds for shelf finds too: no id, no link."""
    payload = {
        "data": {
            "total": 1,
            "results": [
                {
                    "id": 90211,
                    "digitalId": 0,
                    "title": "Obscure Comic (1975) #3",
                    "issueNumber": 3,
                    "series": {"name": "Obscure Comic (1975)"},
                    "urls": [],
                    "thumbnail": {},
                }
            ],
        }
    }
    respx.get(f"{BASE_URL}/comics").mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as http:
        proposed = await shelf_service.propose(
            session,
            user_id=user.id,
            candidates=["Obscure Comic 3"],
            source=ShelfSource.TYPED,
            client=MarvelClient("pub", "priv", client=http),
        )
    await shelf_service.confirm(
        session,
        user_id=user.id,
        candidate_id=proposed["results"][0]["candidate_id"],
        chosen_key="obscure-comic-3",
    )
    created = await session.scalar(select(Issue).where(Issue.key == "obscure-comic-3"))
    assert created.digital_id is None
    assert created.availability == Availability.UNCONFIRMED.value


async def test_blank_candidate_strings_are_ignored(session, user, loaded_event) -> None:
    """Claude reading a blurry photo can easily produce an empty line."""
    result = await shelf_service.propose(
        session,
        user_id=user.id,
        candidates=["", "   ", "King in Black #5"],
        source=ShelfSource.PHOTO,
    )
    assert len(result["results"]) == 1
    assert result["results"][0]["raw_text"] == "King in Black #5"


# --- whats_next for an issue in no event ---


async def test_whats_next_for_an_issue_outside_any_event(session, user, loaded_event) -> None:
    """Reachable via a confirmed shelf find, which has no reading order."""
    from service import guide as guide_service

    orphan = Issue(
        key="orphan-comic-1",
        series_name="Orphan Comic",
        series_slug="orphan-comic",
        issue_number=1,
        availability=Availability.UNCONFIRMED.value,
    )
    session.add(orphan)
    await session.commit()
    await session.refresh(orphan)

    entry = guide_service.GuideEntry(
        key=orphan.key,
        position=0,
        series_name=orphan.series_name,
        issue_number=1,
        title="",
        published_on=None,
        role="",
        narrative_role="",
        franchise="",
        note="",
        availability=orphan.availability,
        provisional=False,
        digital_id=None,
        source_id=None,
        issue_id=orphan.id,
    )
    result = await guide_service.whats_next(session, entry)
    assert result["next"] is None
    assert "isn't part of a curated event" in result["note"]
