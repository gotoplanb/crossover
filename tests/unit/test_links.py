"""Gate A and Gate B, as tests.

Gate A: links must be tappable markdown. A URL in backticks renders as a code
span on iOS and cost a copy-paste during testing.

Gate B: a fabricated digital id does not error — it serves an unrelated comic.
So there is no such thing as a probable link.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from marvel import links


@dataclass
class Fake:
    digital_id: int | None = None
    source_id: int | None = None


@pytest.fixture(autouse=True)
def _fresh_config():
    """link_config is cached; clear it so a test can flip the one-tap switch."""
    links.link_config.cache_clear()
    yield
    links.link_config.cache_clear()


def test_reader_url_from_a_confirmed_id() -> None:
    assert links.reader_url(55901) == "https://read.marvel.com/#/book/55901"


@pytest.mark.parametrize("absent", [None, 0])
def test_no_reader_url_without_a_confirmed_id(absent) -> None:
    """0 is how Marvel says "no digital edition". Treating it as an id is the
    most likely route back to a Gate B bug."""
    assert links.reader_url(absent) is None


def test_missing_id_renders_as_the_one_sanctioned_phrase() -> None:
    link = links.build_link(Fake(), "King in Black #2")
    assert not link.available
    assert link.markdown == links.NOT_ON_MU
    assert "http" not in link.markdown


def test_available_link_is_a_markdown_link() -> None:
    markdown = links.build_link(Fake(digital_id=55901), "King in Black #1").markdown
    assert markdown == "[King in Black #1](https://read.marvel.com/#/book/55901)"
    links.assert_tappable(markdown)


def test_one_tap_is_off_by_default() -> None:
    """Gate C confirmed only the web-fallback redirect from a desktop client.
    Until someone taps one on an iPhone, the reader URL stays primary."""
    assert links.one_tap_enabled() is False
    assert links.one_tap_url(86133) is None
    link = links.build_link(Fake(digital_id=55901, source_id=86133), "x")
    assert link.url == "https://read.marvel.com/#/book/55901"
    assert link.fallback_url is None


def test_one_tap_leads_with_reader_url_as_fallback_when_enabled(monkeypatch) -> None:
    config = dict(links.link_config())
    config["one_tap"] = {**config["one_tap"], "enabled": True}
    monkeypatch.setattr(links, "link_config", lambda: config)

    link = links.build_link(Fake(digital_id=55901, source_id=86133), "King in Black #1")
    assert link.url == "https://marvel.smart.link/fiir7ec77?type=issue&sourceId=86133"
    assert link.fallback_url == "https://read.marvel.com/#/book/55901"
    assert "[King in Black #1](https://marvel.smart.link" in link.markdown
    assert "[read in browser](https://read.marvel.com/#/book/55901)" in link.markdown
    links.assert_tappable(link.markdown)


def test_one_tap_never_emitted_alone(monkeypatch) -> None:
    """A marvel.com id is not evidence that Marvel Unlimited has the book.

    Without a confirmed digital_id we have no Marvel Unlimited observation for
    this issue at all, so a one-tap link would be exactly the kind of plausible
    -but-unverified link Gate B warns about.
    """
    config = dict(links.link_config())
    config["one_tap"] = {**config["one_tap"], "enabled": True}
    monkeypatch.setattr(links, "link_config", lambda: config)

    link = links.build_link(Fake(digital_id=None, source_id=86133), "King in Black #2")
    assert not link.available
    assert link.markdown == links.NOT_ON_MU


def test_assert_tappable_rejects_code_spans() -> None:
    with pytest.raises(AssertionError, match="code span"):
        links.assert_tappable("read it at `https://read.marvel.com/#/book/55901`")


def test_assert_tappable_rejects_html_anchors() -> None:
    with pytest.raises(AssertionError, match="markdown"):
        links.assert_tappable('<a href="https://read.marvel.com/#/book/1">read</a>')


def test_attribution_is_present() -> None:
    """Marvel's terms require it wherever their data or art is displayed."""
    assert "Marvel" in links.attribution()
