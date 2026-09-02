"""Recorded-response transports.

The mirror's budget is 60 requests a minute, shared (#28), so these fixtures
exist to stop the suite and the snapshot script from spending it. Two properties
matter most here: a recording must replay as the *same* response it captured,
and a missing recording must be loud rather than looking like "not found".
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
import respx

from marvel.cassette import (
    CassetteMiss,
    RecordingTransport,
    SyncRecordingTransport,
    available,
    cassette_name,
    replay,
    response_from,
)
from marvel.mirror import DEFAULT_BASE_URL, MirrorClient

BODY = {"query": "Daredevil", "count": 1, "items": [{"id": 8073, "issueNumber": "1"}]}


def _request(url: str = f"{DEFAULT_BASE_URL}/search/issues?q=Daredevil&limit=100"):
    return httpx.Request("GET", url)


# --- naming ------------------------------------------------------------------


def test_a_name_is_readable_and_stable() -> None:
    """These are committed and reviewed, so a diff should show which query
    changed without anyone decoding a hash."""
    name = cassette_name(_request())
    assert name.startswith("search-issues-q-daredevil-limit-100-")
    assert name.endswith(".json")
    assert cassette_name(_request()) == name


def test_different_queries_get_different_names() -> None:
    a = cassette_name(_request(f"{DEFAULT_BASE_URL}/search/issues?q=Venom&limit=100"))
    b = cassette_name(_request(f"{DEFAULT_BASE_URL}/search/issues?q=Venom&limit=40"))
    assert a != b, "limit is part of the request and must be part of the key"


def test_names_stay_filesystem_safe() -> None:
    name = cassette_name(_request(f"{DEFAULT_BASE_URL}/search/issues?q=King+in+Black%3A+Namor"))
    assert "/" not in name and ":" not in name and " " not in name


# --- replay ------------------------------------------------------------------


async def test_a_recording_replays_as_the_same_response(tmp_path) -> None:
    (tmp_path / cassette_name(_request())).write_text(
        json.dumps({"request": {}, "response": {"status": 200, "json": BODY}})
    )
    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        response = await http.get(
            f"{DEFAULT_BASE_URL}/search/issues", params={"q": "Daredevil", "limit": 100}
        )
    assert response.status_code == 200
    assert response.json() == BODY


async def test_a_missing_recording_raises_rather_than_returning_empty(tmp_path) -> None:
    """`MirrorClient._get` swallows httpx errors and degrades to "no
    candidates", which is right against a live mirror and wrong here — a stale
    cassette would read as a passing test."""
    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        with pytest.raises(CassetteMiss, match="no recording"):
            await http.get(f"{DEFAULT_BASE_URL}/search/issues", params={"q": "Nope"})


async def test_a_cassette_miss_is_not_swallowed_by_the_client(tmp_path) -> None:
    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        with pytest.raises(CassetteMiss):
            await MirrorClient(client=http).candidates("Never Recorded 1")


def test_a_non_json_recording_replays_as_text(tmp_path) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"request": {}, "response": {"status": 500, "text": "boom"}}))
    response = response_from(path)
    assert response.status_code == 500 and response.text == "boom"


def test_available_counts_recordings(tmp_path) -> None:
    assert available(tmp_path) == 0
    (tmp_path / "a.json").write_text("{}")
    assert available(tmp_path) == 1
    assert available(tmp_path / "missing") == 0


# --- recording ---------------------------------------------------------------


@respx.mock
async def test_recording_writes_a_replayable_file(tmp_path) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(return_value=httpx.Response(200, json=BODY))
    transport = RecordingTransport(tmp_path)
    async with httpx.AsyncClient(transport=transport) as http:
        await http.get(f"{DEFAULT_BASE_URL}/search/issues", params={"q": "Daredevil", "limit": 100})
    assert transport.recorded == 1

    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        again = await http.get(
            f"{DEFAULT_BASE_URL}/search/issues", params={"q": "Daredevil", "limit": 100}
        )
    assert again.json() == BODY, "what was recorded must replay identically"


@respx.mock
async def test_a_gzipped_response_replays_decoded(tmp_path) -> None:
    """The bug this guards: `aread()` decompresses the body, so replaying the
    original `Content-Encoding: gzip` header made httpx decode it a second time.
    That failed silently — every lookup came back empty while the recordings on
    disk were perfectly good."""
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(
        return_value=httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(json.dumps(BODY).encode()),
        )
    )
    transport = RecordingTransport(tmp_path)
    async with httpx.AsyncClient(transport=transport) as http:
        live = await http.get(f"{DEFAULT_BASE_URL}/issues/1")
    assert live.json() == BODY, "the response handed back mid-record must be usable"

    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        assert (await http.get(f"{DEFAULT_BASE_URL}/issues/1")).json() == BODY


@respx.mock
async def test_an_error_response_is_not_recorded(tmp_path) -> None:
    """A 429 is a fact about the moment, not about the resource. Recording one
    would replay a rate limit forever."""
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(return_value=httpx.Response(429))
    transport = RecordingTransport(tmp_path)
    async with httpx.AsyncClient(transport=transport) as http:
        await http.get(f"{DEFAULT_BASE_URL}/issues/1")
    assert transport.recorded == 0
    assert available(tmp_path) == 0


@respx.mock
def test_the_sync_transport_records_and_then_reuses(tmp_path) -> None:
    """The snapshot script is synchronous, and re-running it must not re-spend
    the budget on requests already captured."""
    route = respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(
        return_value=httpx.Response(200, json=BODY)
    )
    transport = SyncRecordingTransport(tmp_path)
    with httpx.Client(transport=transport) as http:
        first = http.get(f"{DEFAULT_BASE_URL}/issues/1")
        second = http.get(f"{DEFAULT_BASE_URL}/issues/1")
    assert (transport.recorded, transport.skipped) == (1, 1)
    assert route.call_count == 1, "the second call must not reach the network"
    assert first.json() == second.json() == BODY


@respx.mock
async def test_a_non_json_body_is_recorded_as_text(tmp_path) -> None:
    """The mirror answers some malformed queries with an HTML error page. It
    still has to round-trip, or the recording of that behaviour is lost."""
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(
        return_value=httpx.Response(200, text="<html>nope</html>")
    )
    async with httpx.AsyncClient(transport=RecordingTransport(tmp_path)) as http:
        await http.get(f"{DEFAULT_BASE_URL}/issues/1")
    async with httpx.AsyncClient(transport=replay(tmp_path)) as http:
        assert (await http.get(f"{DEFAULT_BASE_URL}/issues/1")).text == "<html>nope</html>"


@respx.mock
def test_the_sync_transport_also_refuses_to_record_errors(tmp_path) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(return_value=httpx.Response(503))
    transport = SyncRecordingTransport(tmp_path)
    with httpx.Client(transport=transport) as http:
        assert http.get(f"{DEFAULT_BASE_URL}/issues/1").status_code == 503
    assert transport.recorded == 0 and available(tmp_path) == 0
