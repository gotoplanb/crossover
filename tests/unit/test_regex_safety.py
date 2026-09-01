"""Regex complexity guards.

Both of these patterns previously had the polynomial-backtracking shape
SonarQube's S5852 flags — adjacent unbounded quantifiers around a literal. That
is theoretical for a series name (short, from Marvel) and much less so for
`parse_guide`, which runs over a whole downloaded HTML page.

Timing tests are usually a bad idea, but the property here is asymptotic, not
absolute: 10x the input must not cost dramatically more than 10x the time. The
thresholds are loose enough to survive a noisy machine and still fail loudly on
a reintroduced catastrophic pattern, which blows up by orders of magnitude.
"""

from __future__ import annotations

import time

import pytest

from marvel.records import series_display_name, series_slug
from scripts.scrape_guide import parse_guide


def _elapsed(fn, *args) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


@pytest.mark.parametrize("fn", [series_display_name, series_slug])
def test_series_name_parsing_is_linear(fn) -> None:
    """Adversarial padding: whitespace and dashes are what the optional
    year-range group can backtrack over."""
    small = "King in Black " + " " * 2_000 + "(" + "-" * 2_000
    large = "King in Black " + " " * 20_000 + "(" + "-" * 20_000

    # Warm up so first-call overhead isn't measured as growth.
    fn(small)
    small_time = max(_elapsed(fn, small), 1e-6)
    large_time = _elapsed(fn, large)

    assert large_time < 0.5, f"10x input took {large_time*1000:.1f}ms — superlinear"
    assert large_time / small_time < 100, (
        f"10x input cost {large_time / small_time:.0f}x the time — likely backtracking"
    )


def test_guide_scraping_is_linear() -> None:
    anchor = '<a href="' + "x" * 60 + "/comics/issue/1/" + "y" * 60 + '" ' * 3 + ">"
    small, large = anchor * 500, anchor * 5_000

    parse_guide(small)
    small_time = max(_elapsed(parse_guide, small), 1e-6)
    large_time = _elapsed(parse_guide, large)

    assert large_time < 2.0, f"10x input took {large_time*1000:.1f}ms — superlinear"
    assert large_time / small_time < 100, (
        f"10x input cost {large_time / small_time:.0f}x the time — likely backtracking"
    )


def test_unterminated_html_does_not_hang() -> None:
    """A truncated save of a guide page is a realistic input."""
    assert parse_guide('<a href="/comics/issue/1/x" >King in Black #1') == []
    assert parse_guide("<a href=" + "\"" * 5_000) == []


def test_year_range_forms_all_strip() -> None:
    """The rewritten pattern must still handle every real form."""
    for name, expected in [
        ("King in Black (2020)", "King in Black"),
        ("King in Black (2020 - 2021)", "King in Black"),
        ("Venom (2018 - Present)", "Venom"),
        ("Venom (2018-Present)", "Venom"),
        ("  Venom (2018 - Present)  ", "Venom"),
        ("Venom", "Venom"),
        ("Marvel Two-in-One (1974 - 1983)", "Marvel Two-in-One"),
    ]:
        assert series_display_name(name) == expected, name
