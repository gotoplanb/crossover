"""Loose `issue_ref` resolution.

This gets said out loud, mid-chapter, one-handed. Requiring an exact title
would push the cost of the lookup back onto the reader, which is the friction
the project exists to remove. But guessing wrong sends someone to the wrong
book, so ambiguity has to come back as a question.
"""

from __future__ import annotations

import pytest

from curation.resolve import Candidate, parse_ref, resolve


@pytest.fixture
def pool() -> list[Candidate]:
    return [
        Candidate("king-in-black-1", "King in Black", 1, 2, "core"),
        Candidate("king-in-black-3", "King in Black", 3, 11, "core"),
        Candidate("king-in-black-namor-1", "King in Black: Namor", 1, 4, "essential_tie_in"),
        Candidate("king-in-black-namor-2", "King in Black: Namor", 2, 9, "essential_tie_in"),
        Candidate("venom-31", "Venom", 31, 5, "essential_tie_in"),
        Candidate(
            "king-in-black-return-of-the-valkyries-1",
            "King in Black: Return of the Valkyries", 1, 16, "essential_tie_in",
        ),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("King in Black #3", "king-in-black-3"),
        ("king in black 3", "king-in-black-3"),
        ("KING IN BLACK #3", "king-in-black-3"),
        ("Venom #31", "venom-31"),
        ("venom 31", "venom-31"),
        ("King in Black: Namor #2", "king-in-black-namor-2"),
        ("kib 3", "king-in-black-3"),
    ],
)
def test_exact_and_near_exact_references(raw, expected, pool) -> None:
    resolution = resolve(raw, pool)
    assert resolution.resolved, f"{raw!r} did not resolve"
    assert resolution.matched.key == expected


def test_the_namor_one(pool) -> None:
    """"the Namor one" is a real thing someone says. Two Namor issues exist, so
    the honest answer is to ask which."""
    resolution = resolve("the Namor one", pool)
    assert not resolution.resolved
    assert resolution.needs_disambiguation
    keys = {m.candidate.key for m in resolution.ambiguous}
    assert keys == {"king-in-black-namor-1", "king-in-black-namor-2"}


def test_a_unique_loose_name_resolves_without_a_number(pool) -> None:
    resolution = resolve("the valkyries one", pool)
    assert resolution.resolved
    assert resolution.matched.key == "king-in-black-return-of-the-valkyries-1"


def test_partial_word_matching(pool) -> None:
    resolution = resolve("valkyr 1", pool)
    assert resolution.resolved
    assert resolution.matched.key == "king-in-black-return-of-the-valkyries-1"


def test_bare_number_prefers_the_core_book(pool) -> None:
    """"we just finished issue 3" — with no series, the book they're holding is
    almost certainly the core one."""
    resolution = resolve("issue 3", pool)
    assert resolution.resolved
    assert resolution.matched.key == "king-in-black-3"


def test_bare_number_asks_when_two_events_share_it(pool) -> None:
    """The core-book preference only holds while one core issue owns that number.

    Two curated events each with a #1 is genuinely ambiguous, and the reader is
    holding one specific book — so ask rather than pick the alphabetically
    luckier event.
    """
    contested = [*pool, Candidate("infinity-war-1", "Infinity War", 1, 1, "core")]
    resolution = resolve("issue 1", contested)
    assert not resolution.resolved
    assert resolution.needs_disambiguation
    assert {m.candidate.key for m in resolution.ambiguous} >= {
        "king-in-black-1", "infinity-war-1"
    }


def test_nonsense_resolves_to_nothing(pool) -> None:
    resolution = resolve("Fantastic Four #52", pool)
    assert not resolution.resolved
    assert not resolution.needs_disambiguation


def test_a_wrong_number_falls_back_to_the_name(pool) -> None:
    """A typo in the number is likelier than a typo in the series, so "Venom
    #999" should still find Venom rather than nothing."""
    resolution = resolve("Venom #999", pool)
    assert resolution.resolved
    assert resolution.matched.key == "venom-31"


@pytest.mark.parametrize(
    ("raw", "tokens_contain", "number"),
    [
        ("King in Black #3", "king", 3),
        ("the Namor one", "namor", None),
        ("venom 31", "venom", 31),
        ("issue 3", None, 3),
    ],
)
def test_parse_ref_splits_name_from_number(raw, tokens_contain, number) -> None:
    tokens, parsed = parse_ref(raw)
    assert parsed == number
    if tokens_contain:
        assert tokens_contain in tokens
    else:
        assert tokens == []


def test_hash_number_wins_over_a_trailing_digit() -> None:
    """"King in Black #3" must not be read as a series containing "3"."""
    tokens, number = parse_ref("King in Black #3")
    assert number == 3
    assert "3" not in tokens


def test_resolution_over_the_real_curated_set(curated_events) -> None:
    """The same resolver, against the actual King in Black YAML."""
    from curation.resolve import candidates_from_guide

    pool = candidates_from_guide(curated_events[0].issues)
    assert resolve("King in Black #3", pool).matched.key == "king-in-black-3"
    assert resolve("venom 31", pool).matched.key == "venom-31"
    assert resolve("the marauders one", pool).matched.key == "king-in-black-marauders-1"


def test_a_candidate_with_no_series_name_scores_zero() -> None:
    """Reachable from a partially-synced issue whose series is still blank."""
    from curation.resolve import _score

    score, why = _score(["namor"], Candidate("k", "", 1))
    assert score == 0.0
    assert "no series name" in why


def test_an_empty_query_scores_zero() -> None:
    from curation.resolve import _score

    score, why = _score([], Candidate("k", "King in Black", 1))
    assert score == 0.0
    assert "no series given" in why


def test_a_decisive_gap_resolves_without_a_number(pool) -> None:
    """"venom" beats every King in Black title by more than the gap, so it does
    not need to ask even though no issue number was given."""
    resolution = resolve("venom", pool)
    assert resolution.resolved
    assert resolution.matched.key == "venom-31"


def test_candidates_from_guide_reads_enum_roles() -> None:
    """The adapter takes both GuideEntry (string role) and CuratedIssue (enum),
    so the enum has to be unwrapped or the core-issue preference breaks."""
    from curation.resolve import candidates_from_guide
    from curation.schema import load_all_events

    curated = load_all_events()[0]
    adapted = candidates_from_guide(curated.issues)
    core = [c for c in adapted if c.role == "core"]
    assert len(core) == 5
    assert all(isinstance(c.role, str) for c in adapted)


def test_the_label_falls_back_to_series_and_number() -> None:
    assert Candidate("k", "Venom", 31).label == "Venom #31"
    assert Candidate("k", "Venom", 31, display="Venom #31 (KiB)").label == "Venom #31 (KiB)"


def test_a_clear_winner_among_several_viable_matches_resolves(pool) -> None:
    """The DECISIVE_GAP path: several titles score above the floor, but one is
    far enough ahead that asking would be pedantic."""
    resolution = resolve("king in black return of the valkyries", pool)
    assert resolution.resolved
    assert resolution.matched.key == "king-in-black-return-of-the-valkyries-1"


def test_a_near_tie_asks_instead_of_guessing(pool) -> None:
    """"king in black namor" with no issue number.

    Both Namor issues score identically, and the other King in Black titles
    score close behind on the shared words, so nothing is decisive enough to
    pick. Asking is the right answer — and the Namor issues must lead the
    offered list, since they are what was actually asked for.
    """
    resolution = resolve("king in black namor", pool)
    assert not resolution.resolved
    assert resolution.needs_disambiguation

    offered = [m.candidate.key for m in resolution.ambiguous]
    assert offered[:2] == ["king-in-black-namor-1", "king-in-black-namor-2"]
    assert resolution.ambiguous[0].score > resolution.ambiguous[2].score
