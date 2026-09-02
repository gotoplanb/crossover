"""Demand-driven enrichment — filling in rows a reader actually cares about.

The property under test is restraint as much as repair: an issue earns a mirror
request by being on somebody's shelf, and a fetched record only gets to write if
it is demonstrably the same issue.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from marvel.mirror import DEFAULT_BASE_URL, MIRROR_SOURCE, MirrorClient
from models.bookmark import Bookmark
from models.catalog import Issue
from models.types import Availability
from service.enrich import DEFAULT_LIMIT, EnrichReport, enrich_bookmarked_issues, thin_issues


def _detail(issue_id: int, series: str, number: str, digital: int = 0) -> dict:
    return {
        "id": issue_id,
        "digitalId": digital,
        "title": f"{series} #{number}",
        "issueNumber": number,
        "seriesName": series,
        "onSaleDate": "1981-04-01T00:00:00-0500",
        "detailUrl": f"https://www.marvel.com/comics/issue/{issue_id}/x",
        "cover": {
            "path": "http://i.annihil.us/u/prod/marvel/i/mg/1/2/abc",
            "extension": "jpg",
        },
        "creators": [{"name": "Frank Miller", "role": "writer"}],
    }


async def _thin_issue(session, user, *, key="daredevil-181", source_id=8164, bookmarked=True):
    issue = Issue(
        key=key,
        series_name="Daredevil",
        series_slug="daredevil",
        issue_number=int(key.rsplit("-", 1)[1]),
        source_id=source_id,
        availability=Availability.UNCONFIRMED.value,
    )
    session.add(issue)
    await session.flush()
    if bookmarked:
        session.add(Bookmark(user_id=user.id, issue_id=issue.id))
    await session.commit()
    await session.refresh(issue)
    return issue


@respx.mock
async def test_a_thin_bookmarked_issue_is_filled_in(session, user) -> None:
    issue = await _thin_issue(session, user)
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))

    await session.refresh(issue)
    assert report.enriched == 1
    assert issue.digital_id == 1672
    assert issue.thumbnail_path, "cover art is the point of enriching"
    assert issue.published_on is not None
    assert issue.creators == ["Frank Miller"]


@respx.mock
async def test_a_confirmed_digital_id_promotes_the_issue_to_linkable(session, user) -> None:
    issue = await _thin_issue(session, user)
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))

    await session.refresh(issue)
    assert report.newly_linkable == 1
    assert issue.availability == Availability.LINKABLE.value
    assert issue.digital_id_source == MIRROR_SOURCE, "the row must say who vouched"


@respx.mock
async def test_a_record_for_a_different_issue_is_refused(session, user) -> None:
    """Gate B at the write boundary. A wrong id does not error — it quietly
    opens a different comic — so a record that is not this issue by series and
    number is reported, never stored."""
    issue = await _thin_issue(session, user)
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Fantastic Four (1961)", "52", 4657))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))

    await session.refresh(issue)
    assert report.identity_mismatch == ["daredevil-181"]
    assert report.enriched == 0
    assert issue.digital_id is None, "nothing may be written from a mismatched record"
    assert issue.availability == Availability.UNCONFIRMED.value


@respx.mock
async def test_a_wrong_number_in_the_same_series_is_also_refused(session, user) -> None:
    """The series matching is not enough on its own — #181 and #182 are one
    tap apart and a reader would not notice."""
    await _thin_issue(session, user)
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Daredevil (1964)", "182", 1673))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))

    assert report.identity_mismatch == ["daredevil-181"]


async def test_an_issue_with_no_lookup_id_is_reported_not_guessed(session, user) -> None:
    """These need a snapshot refresh, not a lookup — there is nothing to ask by."""
    await _thin_issue(session, user, source_id=None)
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))
    assert report.no_lookup_id == ["daredevil-181"]
    assert report.enriched == 0


@respx.mock
@pytest.mark.parametrize("failure", [httpx.Response(429), httpx.Response(500)])
async def test_an_unreachable_mirror_leaves_the_row_alone(session, user, failure) -> None:
    issue = await _thin_issue(session, user)
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(return_value=failure)
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))

    await session.refresh(issue)
    assert report.unresolved == ["daredevil-181"]
    assert issue.digital_id is None


@respx.mock
async def test_an_issue_nobody_bookmarked_is_not_looked_up(session, user) -> None:
    """The whole point is spending the request budget on issues someone
    actually looked at."""
    await _thin_issue(session, user, bookmarked=False)
    route = respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))
    assert report.examined == 0
    assert not route.called


@respx.mock
async def test_an_already_complete_issue_is_not_refetched(session, user) -> None:
    issue = await _thin_issue(session, user)
    issue.digital_id = 1672
    issue.thumbnail_path = "http://i.annihil.us/u/prod/marvel/i/mg/1/2/abc"
    await session.commit()
    route = respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(200, json=_detail(8164, "Daredevil (1964)", "181", 1672))
    )
    async with httpx.AsyncClient() as http:
        report = await enrich_bookmarked_issues(session, MirrorClient(client=http))
    assert report.examined == 0
    assert not route.called


async def test_the_limit_is_a_real_limit(session, user) -> None:
    """Not a page size: the mirror allows 60 requests a minute and each issue
    costs one."""
    for number in (181, 182, 183):
        await _thin_issue(session, user, key=f"daredevil-{number}", source_id=8000 + number)
    assert len(await thin_issues(session, limit=2)) == 2


def test_the_report_names_what_it_could_not_do() -> None:
    """A silent skip reads as success. Every bucket has to show up in the
    summary or a pass that fixed nothing looks like a pass that fixed
    everything."""
    summary = EnrichReport(
        examined=3,
        enriched=1,
        no_lookup_id=["a-1"],
        identity_mismatch=["b-2"],
        unresolved=["c-3"],
    ).summary()
    assert "no lookup id" in summary
    assert "identity mismatch" in summary
    assert "unresolved" in summary
    assert "a-1" in summary and "b-2" in summary and "c-3" in summary


def test_a_clean_report_says_nothing_extra() -> None:
    assert EnrichReport(examined=2, enriched=2).summary().count("\n") == 2


def test_the_cli_default_matches_the_service_default() -> None:
    """The CLI advertises 25 in its --help text but cannot import the constant
    at parser-build time, so pin them together here."""
    assert DEFAULT_LIMIT == 25
    from scripts.cli import main  # noqa: F401  — import guard only
