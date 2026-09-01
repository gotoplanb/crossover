"""The marvel.com reading-guide scraper (SPEC §5 source 2).

Not wired into any automated path — marvel.com returns 403 to programmatic
requests, so it has to be run against saved HTML from a real browser session.
Tested anyway so the parse is known-good whenever someone does that.
"""

from __future__ import annotations

from scripts.scrape_guide import parse_guide, to_yaml

SAMPLE = """
<div class="guide">
  <a href="/comics/issue/86133/king_in_black_2020_1?utm_campaign=guide">King in Black #1</a>
  <a href="/comics/issue/87001/king_in_black_namor_2020_1">King in Black: Namor  #1</a>
  <a href="/comics/issue/86133/king_in_black_2020_1">King in Black #1</a>
  <a href="/comics/series/29142/king_in_black">King in Black</a>
  <a href="/comics/issue/90210/venom_2018_31">Venom #31</a>
</div>
"""


def test_issues_are_parsed_in_page_order() -> None:
    entries = parse_guide(SAMPLE)
    assert [e["key"] for e in entries] == [
        "king-in-black-1", "king-in-black-namor-1", "venom-31",
    ]


def test_duplicate_links_collapse() -> None:
    """Guides link the same issue more than once; a duplicate would break the
    contiguity gate downstream."""
    assert len(parse_guide(SAMPLE)) == 3


def test_series_links_are_ignored() -> None:
    """Only /comics/issue/ URLs with a `#N` title are issues."""
    assert all("series" not in e["key"] for e in parse_guide(SAMPLE))


def test_the_marvel_com_id_is_captured_not_invented() -> None:
    entries = {e["key"]: e for e in parse_guide(SAMPLE)}
    assert entries["king-in-black-1"]["marvel_com_issue_id"] == 86133
    # Never a digital_id: a guide page says nothing about Marvel Unlimited, and
    # inferring one would be exactly the Gate B mistake.
    assert "digital_id" not in entries["king-in-black-1"]


def test_emitted_yaml_is_a_seed_not_a_finished_file() -> None:
    body = to_yaml(parse_guide(SAMPLE))
    assert "position: 1" in body
    assert "provisional: true" in body
    # Roles and clustering are hand work; the scraper must not pretend otherwise.
    assert body.count("role: optional_tie_in") == 3


def test_an_empty_page_yields_nothing() -> None:
    assert parse_guide("<html><body>no issues here</body></html>") == []
