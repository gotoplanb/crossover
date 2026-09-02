"""The mirror client against *recorded real responses*, not invented ones.

`test_mirror.py` builds its payloads by hand. Those tests are precise about
edge cases but they only prove the parser is self-consistent — if the mirror
changed its response format tomorrow, every one of them would still pass.

These replay bodies captured from the live service (`make record-mirror`), so
they fail when reality drifts. They cost no requests: the transport cannot reach
the network, and a missing recording raises rather than quietly returning
nothing.

Each case is one the implementation was actually shaped by, so a regression here
points at a specific decision rather than a vague "parsing broke".
"""

from __future__ import annotations

import httpx
import pytest

from marvel.cassette import DEFAULT_CASSETTE_DIR, available, replay
from marvel.mirror import MirrorClient

pytestmark = pytest.mark.skipif(
    available(DEFAULT_CASSETTE_DIR) == 0,
    reason="no recordings; run `make record-mirror`",
)


async def _candidates(query: str, limit: int = 4):
    async with httpx.AsyncClient(transport=replay()) as http:
        return await MirrorClient(client=http).candidates(query, limit=limit)


async def _record(issue_id: int):
    async with httpx.AsyncClient(transport=replay()) as http:
        return await MirrorClient(client=http).record(issue_id)


async def test_an_off_event_find_resolves_from_a_real_response() -> None:
    """The shelf case: a comic belonging to no curated event."""
    found = await _candidates("Daredevil 181")
    assert found[0].issue_number == "181"
    assert found[0].series_name.startswith("Daredevil")


async def test_the_series_drilldown_reaches_the_middle_of_a_long_run() -> None:
    """The discovery that shaped `candidates`: search returns one 100-result
    page ranked by the mirror's own relevance and ignores `offset`, so
    "Fantastic Four 52" contains no #52 anywhere on it. Only the series endpoint
    reaches it — and this asserts that against the real page, which is the only
    place the shortfall is visible."""
    found = await _candidates("Fantastic Four 52")
    assert found[0].issue_number == "52"


async def test_a_year_disambiguates_a_common_title() -> None:
    """Bare "Venom" buries the 2018 run under decades of other Venom titles."""
    found = await _candidates("Venom 2018 31")
    assert found[0].issue_number == "31"
    assert "2018" in found[0].series_name


async def test_punctuation_the_mirror_rejects_is_handled() -> None:
    """Their search returns a 500 on a colon, so the query is stripped."""
    found = await _candidates("King in Black: Namor 1")
    assert found[0].issue_number == "1"
    assert "Namor" in found[0].series_name


async def test_a_real_detail_response_carries_a_usable_digital_id() -> None:
    """Gate B's payload. Verified against the live reader in docs/gates.md —
    digital id 1672 opens Daredevil #181."""
    record = await _record(8164)
    assert record is not None
    assert (record.series_name, record.issue_number) == ("Daredevil", 181)
    assert record.digital_id == 1672
    assert record.marvel_com_issue_id == 8164
    assert record.published_on is not None and record.published_on.year == 1982
    assert record.thumbnail_path and record.thumbnail_extension
    assert "Frank Miller" in record.creators


async def test_the_series_year_range_is_stripped_for_display() -> None:
    """The mirror says "Daredevil (1964 - 1998)"; a reader says "Daredevil"."""
    record = await _record(8164)
    assert record.series_name == "Daredevil"
    assert "(" not in record.series_name
