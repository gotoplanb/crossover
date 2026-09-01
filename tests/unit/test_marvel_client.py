"""API-client tests against recorded fixtures — the suite never hits Marvel."""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from marvel.client import (
    BASE_URL,
    MarvelAPIError,
    MarvelClient,
    MarvelCredentialsMissing,
    auth_params,
    params_key,
)


def test_auth_hash_is_md5_of_ts_private_public() -> None:
    """Marvel specifies md5(ts + privateKey + publicKey). Not our choice."""
    params = auth_params("pub", "priv", ts="1000")
    assert params == {
        "ts": "1000",
        "apikey": "pub",
        "hash": hashlib.md5(b"1000privpub").hexdigest(),  # noqa: S324
    }


def test_params_key_strips_the_auth_triple() -> None:
    """ts and hash change on every call, so leaving them in the cache key would
    make the cache a write-only log."""
    key = params_key({"limit": 100, "offset": 0, "ts": "1", "apikey": "p", "hash": "h"})
    assert key == "limit=100&offset=0"


def test_params_key_is_order_independent() -> None:
    assert params_key({"b": 2, "a": 1}) == params_key({"a": 1, "b": 2})


async def test_missing_credentials_refuses_to_call() -> None:
    """Better than sending an unsigned request and reading a 409 back."""
    client = MarvelClient(None, None)
    assert not client.configured
    with pytest.raises(MarvelCredentialsMissing):
        await client.get("comics")


@respx.mock
async def test_event_comics_signs_and_parses(event_comics_payload) -> None:
    route = respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload, headers={"etag": "e1"})
    )
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        response = await client.event_comics(860)

    assert route.called
    sent = dict(route.calls.last.request.url.params)
    assert sent["apikey"] == "pub"
    assert {"ts", "hash"} <= sent.keys()
    assert sent["orderBy"] == "onsaleDate"
    assert response.etag == "e1"
    assert response.total == 3
    assert response.attribution_text == "Data provided by Marvel. © 2026 MARVEL"
    # The cache key must not carry the per-call auth values.
    assert "hash=" not in response.params_key


@respx.mock
async def test_limit_is_capped_at_marvels_maximum() -> None:
    """Marvel rejects limit > 100 outright with a 409."""
    route = respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    async with httpx.AsyncClient() as http:
        await MarvelClient("pub", "priv", client=http).search_comics(title="Venom", limit=5000)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"


@respx.mock
async def test_api_errors_surface_marvels_own_reason() -> None:
    """"409: Limit greater than 100" beats a bare status code."""
    respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(409, json={"code": 409, "status": "Limit greater than 100."})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(MarvelAPIError, match="Limit greater than 100"):
            await MarvelClient("pub", "priv", client=http).get("comics")


@respx.mock
async def test_pagination_stops_once_total_is_covered(event_comics_payload) -> None:
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=event_comics_payload)
    )
    async with httpx.AsyncClient() as http:
        pages = await MarvelClient("pub", "priv", client=http).all_event_comics(860)
    # total=3 fits in one page of 100, so exactly one request.
    assert len(pages) == 1


@respx.mock
async def test_a_non_json_error_body_still_surfaces() -> None:
    """Marvel's gateway returns HTML on some 5xx. The client must not raise a
    JSON decode error on top of the real failure."""
    respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(MarvelAPIError, match="Bad Gateway") as exc:
            await MarvelClient("pub", "priv", client=http).get("comics")
    assert exc.value.status_code == 502


@respx.mock
async def test_the_client_closes_a_connection_it_created() -> None:
    """Without an injected client, each call owns its AsyncClient and must close
    it — a leaked connection per call adds up over a sync."""
    respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    client = MarvelClient("pub", "priv")
    response = await client.get("comics")
    assert response.total == 0


@respx.mock
async def test_an_error_still_closes_a_self_created_connection() -> None:
    respx.get(f"{BASE_URL}/comics").mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(MarvelAPIError):
        await MarvelClient("pub", "priv").get("comics")


@respx.mock
async def test_pagination_walks_multiple_pages() -> None:
    """A real event roster runs past Marvel's 100-item page cap."""
    first = {
        "attributionText": "x",
        "data": {"total": 150, "results": [
            {"id": i, "issueNumber": i, "series": {"name": "S"}} for i in range(100)
        ]},
    }
    second = {
        "attributionText": "x",
        "data": {"total": 150, "results": [
            {"id": i, "issueNumber": i, "series": {"name": "S"}} for i in range(100, 150)
        ]},
    }
    route = respx.get(f"{BASE_URL}/events/860/comics").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    async with httpx.AsyncClient() as http:
        pages = await MarvelClient("pub", "priv", client=http).all_event_comics(860)

    assert len(pages) == 2
    assert route.call_count == 2
    offsets = [dict(c.request.url.params)["offset"] for c in route.calls]
    assert offsets == ["0", "100"]


@respx.mock
async def test_pagination_stops_on_an_empty_page() -> None:
    """Defends against a `total` that over-reports and would otherwise loop."""
    payload = {"data": {"total": 10_000, "results": []}}
    respx.get(f"{BASE_URL}/events/860/comics").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with httpx.AsyncClient() as http:
        pages = await MarvelClient("pub", "priv", client=http).all_event_comics(860)
    assert len(pages) == 1


@respx.mock
async def test_search_comics_by_series_id() -> None:
    route = respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    async with httpx.AsyncClient() as http:
        await MarvelClient("pub", "priv", client=http).search_comics(series_id=29142)
    params = dict(route.calls.last.request.url.params)
    assert params["series"] == "29142"
    assert "titleStartsWith" not in params


@respx.mock
async def test_search_series_and_events_endpoints() -> None:
    series = respx.get(f"{BASE_URL}/series").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    events = respx.get(f"{BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    async with httpx.AsyncClient() as http:
        client = MarvelClient("pub", "priv", client=http)
        await client.search_series("King in Black")
        await client.events(offset=100)

    assert dict(series.calls.last.request.url.params)["titleStartsWith"] == "King in Black"
    assert dict(events.calls.last.request.url.params)["offset"] == "100"


@respx.mock
async def test_a_leading_slash_on_the_endpoint_is_tolerated() -> None:
    """Callers write both forms; the cache key must not fork because of it."""
    route = respx.get(f"{BASE_URL}/comics").mock(
        return_value=httpx.Response(200, json={"data": {"results": [], "total": 0}})
    )
    async with httpx.AsyncClient() as http:
        response = await MarvelClient("pub", "priv", client=http).get("/comics/")
    assert route.called
    assert response.endpoint == "comics"
