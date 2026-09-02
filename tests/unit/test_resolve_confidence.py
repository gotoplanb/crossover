"""The resolver must not be confident about text that names nothing.

`resolve` has three outcomes, and the difference between two of them is the
whole point: a *confident* match is acted on, an *ambiguous* one is read back to
the reader as a question. Returning something confidently wrong is the milder
version of the Gate B mistake — and unlike `add_to_shelf`, `whats_next` has no
confirmation step to catch it, so a reader who names an off-roster issue would
be told what to read next based on a book they never mentioned.

Two defects had to combine to produce that:

1. A one-character token acted as a wildcard prefix. "Empyre's End" tokenizes
   to ["web", "venom", "empyre", "s", "end"], and that possessive "s"
   prefix-matched every query word starting with s.
2. Having no competitor was treated as being right, so the resulting 0.46
   score came back as certain purely because nothing else cleared MIN_SCORE.

These are corpus tests on purpose. The thresholds are tuned numbers, and a test
that asserted the numbers would just restate the source; what matters is that
real references still resolve and invented ones still don't.
"""

from __future__ import annotations

import pytest

from curation.resolve import (
    CONFIDENT_SCORE,
    MIN_PREFIX_LENGTH,
    Candidate,
    _score,
    candidates_from_guide,
    parse_ref,
    resolve,
)


@pytest.fixture(scope="module")
def roster(curated_events):
    return candidates_from_guide(curated_events[0].ordered)


#: References a reader might actually say, with the key they must land on.
#: None means "resolving or asking are both fine, but silence is not".
SPOKEN = [
    ("King in Black #3", "king-in-black-3"),
    ("venom 31", "venom-31"),
    ("Namor 3", "king-in-black-namor-3"),
    ("the union 2", "the-union-2"),
    ("kib 5", "king-in-black-5"),
    ("rotv 2", "king-in-black-return-of-the-valkyries-2"),
    ("pots 2", "king-in-black-planet-of-the-symbiotes-2"),
    ("Return of the Valkyries 1", "king-in-black-return-of-the-valkyries-1"),
    ("Gwenom vs Carnage 1", "king-in-black-gwenom-vs-carnage-1"),
    ("Thunderbolts 2", "king-in-black-thunderbolts-2"),
    ("Black Knight 1", "king-in-black-black-knight-1"),
    ("Ghost Rider 1", "king-in-black-ghost-rider-1"),
    ("planet of the symbiotes 1", "king-in-black-planet-of-the-symbiotes-1"),
    ("Empyre's End", "web-of-venom-empyre-s-end-1"),
    ("iron man doom", "king-in-black-iron-man-doom-1"),
    ("valkyr 1", "king-in-black-return-of-the-valkyries-1"),
    ("the Namor one", None),
]

#: Names of real comics that are simply not in this roster, plus noise. Every
#: one of these must fail to produce a *confident* answer.
NOT_IN_THE_ROSTER = [
    "Some Unknown Series",
    "Saga 12",
    "Sandman 1",
    "Superman 75",
    "Daredevil 181",
    "Fantastic Four 52",
    "Uncanny X-Men 141",
    "Amazing Spider-Man 300",
    "Batman 1",
    "Invincible 33",
    "Utterly Unknown 1",
    "asdf qwer",
]


@pytest.mark.parametrize(("raw", "expected"), SPOKEN, ids=[s[0] for s in SPOKEN])
def test_a_real_reference_still_resolves(roster, raw, expected) -> None:
    resolution = resolve(raw, roster)
    if expected is None:
        assert resolution.matched or resolution.ambiguous, "went silent on a real reference"
        return
    assert resolution.matched is not None, f"{raw!r} stopped resolving"
    assert resolution.matched.key == expected


@pytest.mark.parametrize("raw", NOT_IN_THE_ROSTER)
def test_something_not_in_the_roster_is_never_confident(roster, raw) -> None:
    """It may offer options or say nothing. It may not assert an answer."""
    resolution = resolve(raw, roster)
    assert resolution.matched is None, (
        f"{raw!r} resolved confidently to {resolution.matched.key if resolution.matched else ''}"
    )


def test_a_possessive_s_is_not_a_wildcard(roster) -> None:
    """The specific mechanism. Four different junk queries scored 0.46 against
    "Web of Venom: Empyre's End" through its possessive "s" alone."""
    for raw in ("Some Unknown Series", "Saga 12", "Sandman 1", "Superman 75"):
        tokens, _ = parse_ref(raw)
        empyres = next(c for c in roster if c.key == "web-of-venom-empyre-s-end-1")
        score, _why = _score(tokens, empyres)
        assert score == 0.0, f"{raw!r} still scores {score} on a possessive"


def test_an_exact_short_token_still_matches() -> None:
    """The prefix floor must not break a legitimately short token — the "x" of
    "X-Men" is a real word here, it just cannot be a *prefix* wildcard."""
    assert MIN_PREFIX_LENGTH > 1
    x_men = Candidate(key="uncanny-x-men-141", series_name="Uncanny X-Men", issue_number=141)
    assert resolve("uncanny x-men 141", [x_men]).matched is x_men


def test_a_weak_lone_match_becomes_a_question_not_an_answer() -> None:
    """Having no competitor is not the same as being right. A thin match should
    be offered for confirmation rather than asserted."""
    thin = Candidate(key="thin-1", series_name="Alpha Beta", issue_number=1)
    resolution = resolve("alpha zulu", [thin])
    score = _score(parse_ref("alpha zulu")[0], thin)[0]
    assert 0 < score < CONFIDENT_SCORE, "fixture no longer exercises the weak-match path"
    assert resolution.matched is None
    assert [m.candidate.key for m in resolution.ambiguous] == ["thin-1"]


def test_the_confidence_floor_leaves_room_for_real_references(roster) -> None:
    """A guard on the threshold itself: raising CONFIDENT_SCORE past the weakest
    genuine match would start refusing references readers really say. Recorded
    here so that failure names its cause instead of showing up as a handful of
    unrelated-looking corpus failures."""
    weakest = min(
        _score(parse_ref(raw)[0], resolve(raw, roster).matched)[0]
        for raw, expected in SPOKEN
        if expected is not None
    )
    assert weakest > CONFIDENT_SCORE, (
        f"weakest legitimate match is {weakest:.3f}, at or below the {CONFIDENT_SCORE} floor"
    )
