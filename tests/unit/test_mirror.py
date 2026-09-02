"""Mirror-client tests against recorded fixtures — the suite never hits the net.

Shapes here are the mirror's real ones, trimmed. The interesting cases are the
three quirks that shaped the client: search ranks by its own relevance and
ignores `offset`, `/issues` ignores every filter it accepts, and the whole thing
is capped at 60 requests a minute.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from marvel.mirror import (
    DEFAULT_BASE_URL,
    MirrorClient,
    normalize_series,
    search_query,
    to_marvel_shape,
)


def _hit(issue_id: int, series: str, number: str, series_id: int = 1) -> dict:
    year = series.split("(")[-1].split(" ")[0] if "(" in series else "2020"
    return {
        "id": issue_id,
        "title": f"{series} #{number}",
        "issueNumber": number,
        "seriesId": series_id,
        "seriesName": series,
        "onSaleDate": f"{year}-01-01T00:00:00-0500",
    }


def _detail(issue_id: int, series: str, number: str, digital: int = 0) -> dict:
    return {
        "id": issue_id,
        "digitalId": digital,
        "title": f"{series} #{number}",
        "issueNumber": number,
        "seriesName": series,
        "onSaleDate": "2020-12-02T00:00:00-0500",
        "unlimitedDate": "2021-03-01T00:00:00-0500",
        "detailUrl": f"https://www.marvel.com/comics/issue/{issue_id}/x",
        "cover": {"path": "http://i.annihil.us/u/prod/marvel/i/mg/1/2/abc", "extension": "jpg"},
        "creators": [{"name": "Donny Cates", "role": "writer"}],
    }


# --- query shaping -----------------------------------------------------------


def test_search_query_strips_punctuation_because_a_colon_500s() -> None:
    assert search_query("King in Black: Namor") == "King in Black Namor"


def test_search_query_of_pure_punctuation_is_empty() -> None:
    assert search_query("!!! ---") == ""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The Amazing Spider-Man", "amazing spider man"),
        ("Amazing Spider-Man", "amazing spider man"),
        ("Venom", "venom"),
        ("Theodore Rex", "theodore rex"),  # only a standalone article is dropped
    ],
)
def test_normalize_series_folds_articles_and_punctuation(name: str, expected: str) -> None:
    """The mirror says "The Amazing Spider-Man"; a reader says "Amazing
    Spider-Man". Neither difference means a different series."""
    assert normalize_series(name) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Venom 87", ("87", None, "Venom")),
        ("Venom #87", ("87", None, "Venom")),
        ("Venom 2018 31", ("31", "2018", "Venom")),
        ("Venom 2018", (None, "2018", "Venom")),
        ("Venom", (None, None, "Venom")),
        ("Venom 007", ("7", None, "Venom")),
        ("1985", (None, "1985", "1985")),
    ],
)
def test_parse_splits_number_year_and_series(text: str, expected: tuple) -> None:
    """A trailing 2018 is the run, not issue #2018."""
    assert MirrorClient._parse(text) == expected


def test_number_treats_leading_zeros_as_the_same_issue() -> None:
    assert MirrorClient._number({"issueNumber": "007"}) == "7"
    assert MirrorClient._number({"issueNumber": "0"}) == "0"
    assert MirrorClient._number({}) == "0"


def test_matching_series_ranks_requested_run_then_exact_then_prefix() -> None:
    hits = [
        _hit(1, "Venom: Lethal Protector (1993)", "1", series_id=10),
        _hit(2, "Venom (2003 - 2004)", "1", series_id=20),
        _hit(3, "Venom (2018 - 2021)", "1", series_id=30),
    ]
    assert MirrorClient._matching_series(hits, "Venom", "2018") == [30, 20, 10]
    assert MirrorClient._matching_series(hits, "Venom", None) == [20, 30, 10]


def test_matching_series_of_an_empty_query_matches_nothing() -> None:
    """Otherwise every series is a prefix match on "" and the drill-down would
    walk the entire mirror."""
    assert MirrorClient._matching_series([_hit(1, "Venom (2018)", "1")], "") == []


# --- the network surface -----------------------------------------------------


@respx.mock
async def test_candidates_ranks_the_requested_number_first() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _hit(1, "Daredevil (1964)", "1"),
                    _hit(181, "Daredevil (1964)", "181"),
                ]
            },
        )
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with httpx.AsyncClient() as http:
        found = await MirrorClient(client=http).candidates("Daredevil 181")
    assert [c.issue_number for c in found] == ["181", "1"]


@respx.mock
async def test_candidates_reaches_past_the_search_page_via_the_series() -> None:
    """The search page is a hard ceiling — `offset` is ignored — so a middle
    issue of a long run is only reachable through the series endpoint."""
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [_hit(1, "Fantastic Four (1961)", "1")]})
    )
    series = respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": [_hit(52, "Fantastic Four (1961)", "52")]})
    )
    async with httpx.AsyncClient() as http:
        found = await MirrorClient(client=http).candidates("Fantastic Four 52")
    assert series.called
    assert found[0].issue_number == "52", "the drill-down result must outrank the search page"


@respx.mock
async def test_a_number_ranks_but_never_filters() -> None:
    """A number misread off a spine is likelier than a wholly wrong series, so a
    miss still offers the series."""
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [_hit(1, "Venom (2018 - 2021)", "1")]})
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with httpx.AsyncClient() as http:
        found = await MirrorClient(client=http).candidates("Venom 999")
    assert [c.issue_number for c in found] == ["1"]


@respx.mock
async def test_a_year_is_searched_with_then_without() -> None:
    """Mirror titles embed the run, so the year is a strong discriminator — but
    it must not turn a findable series into no result at all."""
    route = respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        side_effect=[
            httpx.Response(200, json={"items": []}),
            httpx.Response(200, json={"items": [_hit(1, "Venom (2018 - 2021)", "1")]}),
        ]
    )
    respx.get(f"{DEFAULT_BASE_URL}/series/1/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with httpx.AsyncClient() as http:
        found = await MirrorClient(client=http).candidates("Venom 2018 1")
    assert [dict(c.request.url.params)["q"] for c in route.calls] == ["Venom 2018", "Venom"]
    assert found[0].issue_number == "1"


@respx.mock
async def test_candidates_of_an_unsearchable_string_is_empty() -> None:
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).candidates("???") == []


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, json={}),  # the 60/min cap
        httpx.Response(500, json={}),
        httpx.ConnectError("down"),
    ],
)
async def test_every_failure_degrades_to_no_candidates(failure) -> None:
    """A shelf capture must survive the mirror being down: the entry stays
    pending with its raw text rather than the call raising."""
    key = "side_effect" if isinstance(failure, Exception) else "return_value"
    kwargs = {key: failure}
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(**kwargs)
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).candidates("Venom 1") == []


@respx.mock
async def test_a_rate_limited_lookup_never_waits_it_out() -> None:
    """Honouring Retry-After is right for the batch snapshot script; here it
    would hold a reader's tool call open."""
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(429, headers={"retry-after": "60"}, json={})
    )
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).candidates("Venom 1") == []


@respx.mock
async def test_malformed_json_is_a_failure_not_a_crash() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(200, content=b"not json")
    )
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).candidates("Venom 1") == []


@respx.mock
async def test_record_carries_the_digital_id() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/issues/8164").mock(
        return_value=httpx.Response(
            200, json=_detail(8164, "Daredevil (1964)", "181", digital=1672)
        )
    )
    async with httpx.AsyncClient() as http:
        record = await MirrorClient(client=http).record(8164)
    assert record is not None
    assert (record.series_name, record.issue_number) == ("Daredevil", 181)
    assert record.digital_id == 1672
    assert record.marvel_com_issue_id == 8164


@respx.mock
@pytest.mark.parametrize("body", [{}, {"id": None}])
async def test_record_of_an_empty_body_is_none(body: dict) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).record(1) is None


@respx.mock
async def test_series_issues_returns_empty_when_the_series_is_unknown() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/series/9/issues").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        assert await MirrorClient(client=http).series_issues(9) == []


@respx.mock
async def test_a_hit_without_an_id_is_skipped() -> None:
    respx.get(f"{DEFAULT_BASE_URL}/search/issues").mock(
        return_value=httpx.Response(
            200, json={"items": [{"title": "no id"}, _hit(2, "Venom (2018)", "2")]}
        )
    )
    async with httpx.AsyncClient() as http:
        found = await MirrorClient(client=http).candidates("Venom")
    assert [c.issue_id for c in found] == [2]


# --- connection lifetime -----------------------------------------------------


@respx.mock
async def test_an_owned_client_is_reused_then_closed() -> None:
    """Pooling is not an optimisation here: a fresh handshake per call pushed
    sequential lookups past the timeout and they resolved to nothing."""
    respx.get(f"{DEFAULT_BASE_URL}/issues/1").mock(
        return_value=httpx.Response(200, json=_detail(1, "Venom (2018)", "1", digital=5))
    )
    async with MirrorClient() as mirror:
        await mirror.record(1)
        first = mirror._owned
        await mirror.record(1)
        assert mirror._owned is first, "the connection must be reused across calls"
    assert mirror._owned is None, "the client must be closed on exit"


async def test_closing_does_not_touch_an_injected_client() -> None:
    """It belongs to the caller."""
    async with httpx.AsyncClient() as http:
        mirror = MirrorClient(client=http)
        await mirror.aclose()
        assert not http.is_closed


async def test_aclose_is_safe_when_nothing_was_opened() -> None:
    await MirrorClient().aclose()


# --- envelope ----------------------------------------------------------------


def test_to_marvel_shape_maps_both_dates_and_keeps_characters_empty() -> None:
    """The mirror carries no character list; inventing names would be worse than
    an empty one."""
    shaped = to_marvel_shape(_detail(1, "Venom (2018)", "31", digital=55836))
    assert [d["type"] for d in shaped["dates"]] == ["onsaleDate", "unlimitedDate"]
    assert shaped["characters"] == {"items": []}
    assert shaped["creators"]["items"] == [{"name": "Donny Cates", "role": "writer"}]
    assert shaped["digitalId"] == 55836


def test_to_marvel_shape_of_a_bare_record_stays_parseable() -> None:
    shaped = to_marvel_shape({"id": 7})
    assert shaped["digitalId"] == 0
    assert shaped["dates"] == [] and shaped["urls"] == []


def test_matching_series_accepts_a_prefix_when_nothing_matches_exactly() -> None:
    """ "Venom" should still reach "Venom: Lethal Protector" when the Venom run
    itself is not on the search page."""
    hits = [_hit(1, "Venom: Lethal Protector (1993)", "1", series_id=10)]
    assert MirrorClient._matching_series(hits, "Venom") == [10]


def test_matching_series_skips_hits_with_no_series_id() -> None:
    hits = [{"title": "orphan"}, _hit(1, "Venom (2018 - 2021)", "1", series_id=30)]
    assert MirrorClient._matching_series(hits, "Venom") == [30]


def test_matching_series_ignores_an_unrelated_series() -> None:
    """A substring search for "Venom" drags in titles that merely mention it;
    those must not become drill-down targets."""
    hits = [
        _hit(1, "Carnage (2015 - 2016)", "1", series_id=40),
        _hit(2, "Venom (2018 - 2021)", "1", series_id=30),
    ]
    assert MirrorClient._matching_series(hits, "Venom") == [30]


def test_matching_series_deduplicates_a_series_seen_twice() -> None:
    """Every issue of a run is its own hit; the series is one drill-down."""
    hits = [
        _hit(1, "Venom (2018 - 2021)", "1", series_id=30),
        _hit(2, "Venom (2018 - 2021)", "2", series_id=30),
    ]
    assert MirrorClient._matching_series(hits, "Venom") == [30]
