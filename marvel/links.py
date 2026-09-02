"""Link construction — the enforcement point for Gates A and B.

Two rules, both learned the hard way and both tested:

1. **A link exists only if a `digital_id` came back from Marvel for that exact
   issue.** Gate B found that a fabricated id does not error; it serves an
   unrelated comic. So there is no "probably right" link. Everything else
   renders as the literal string `not on Marvel Unlimited` — SPEC §6's "no
   third state".

2. **Links are markdown, never code spans, never HTML.** A URL in backticks
   renders as a code span and is not tappable on iOS, which is the entire
   delivery mechanism. `assert_tappable` exists so tests can say this out loud.

Templates come from `config/links.yaml`, not from this module: Branch link
configs change without notice (docs/gates.md, Gate C).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "links.yaml"

#: The one sanctioned way to say "we can't link this". Tools and templates both
#: use this constant so the phrasing never drifts between surfaces.
NOT_ON_MU = "not on Marvel Unlimited"

#: Marvel returns 0 rather than null for a comic with no digital edition.
#: Treating 0 as an id is the most likely way to reintroduce a Gate B bug.
_ABSENT_IDS = (None, 0)


class LinkableIssue(Protocol):
    """Anything with the two ids a link can be built from. Both `Issue` and
    `Bookmark` satisfy this, which is why bookmarks denormalize them."""

    digital_id: int | None
    source_id: int | None


@lru_cache
def link_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


def attribution() -> str:
    """Marvel's terms require this wherever their data or art is displayed."""
    return link_config()["attribution"]


def one_tap_enabled() -> bool:
    return bool(link_config().get("one_tap", {}).get("enabled", False))


def reader_url(digital_id: int | None) -> str | None:
    """Gate A baseline. None — never a guess — when there is no confirmed id."""
    if digital_id in _ABSENT_IDS:
        return None
    return link_config()["reader"]["template"].format(digital_id=digital_id)


def one_tap_url(source_id: int | None) -> str | None:
    """Gate C link. Returns None unless explicitly enabled in config, because
    the probe only confirmed the web fallback, not the native app hand-off."""
    if not one_tap_enabled() or source_id in _ABSENT_IDS:
        return None
    return link_config()["one_tap"]["template"].format(source_id=source_id)


@dataclass(frozen=True)
class IssueLink:
    """The rendered link for one issue, in every form a caller needs.

    `markdown` is what tools put in their response. It is either a real
    markdown link or exactly NOT_ON_MU — callers never have to decide.
    """

    label: str
    url: str | None
    fallback_url: str | None
    available: bool
    #: When Marvel Unlimited will make it readable, when that is still ahead.
    #: Carried so a caller can *explain* an unavailable issue without a third
    #: link state existing — see `build_link`.
    unlimited_on: date | None = None

    @property
    def markdown(self) -> str:
        if not self.available or not self.url:
            return NOT_ON_MU
        primary = f"[{self.label}]({self.url})"
        if self.fallback_url and self.fallback_url != self.url:
            return f"{primary} (or [read in browser]({self.fallback_url}))"
        return primary


def build_link(issue: LinkableIssue, label: str) -> IssueLink:
    """Resolve an issue to its link, honoring the one-tap config switch.

    With one-tap enabled the Branch link leads and the reader URL is the
    labelled fallback, per SPEC §0 Gate C. With it disabled — the default —
    only the reader URL is emitted.
    """
    # An issue can have a confirmed digital id and still not be readable:
    # Marvel Unlimited trails print by around three months, which is exactly
    # the case a reader following a current event hits. Handing over a link that
    # does not open yet is the same broken promise as handing over a wrong one,
    # so no link is emitted — and SPEC §6's "no third state" holds, because the
    # rendering is still exactly NOT_ON_MU.
    #
    # The date rides along on the returned IssueLink instead, so a caller can
    # say *when* rather than just "no". That keeps the explanation out of the
    # link string, where a third variant would start eroding the one rule the
    # reader's trust actually rests on.
    unlimited_on = getattr(issue, "unlimited_on", None)
    if unlimited_on and unlimited_on > date.today():
        return IssueLink(
            label=label,
            url=None,
            fallback_url=None,
            available=False,
            unlimited_on=unlimited_on,
        )

    reader = reader_url(getattr(issue, "digital_id", None))
    one_tap = one_tap_url(getattr(issue, "source_id", None))
    # A one-tap link is never emitted alone: without a confirmed digital_id we
    # have no Marvel Unlimited evidence for this issue at all, and Gate B says
    # the marvel.com id is not evidence that the *reader* has the book.
    if one_tap and reader:
        return IssueLink(label=label, url=one_tap, fallback_url=reader, available=True)
    if reader:
        return IssueLink(label=label, url=reader, fallback_url=None, available=True)
    return IssueLink(label=label, url=None, fallback_url=None, available=False)


def assert_tappable(text: str) -> None:
    """Raise if `text` breaks the Gate A output rules.

    Used by tests over real tool output rather than over hand-picked strings,
    so a regression anywhere in the response-building path is caught.
    """
    if "`" in text and "http" in text:
        raise AssertionError(
            "URL inside backticks renders as a non-tappable code span (Gate A). "
            f"Offending text: {text!r}"
        )
    if "<a " in text.lower() or "href=" in text.lower():
        raise AssertionError(
            f"HTML anchors are not the tested format; use markdown links. Text: {text!r}"
        )
