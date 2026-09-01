"""Loose `issue_ref` resolution (SPEC §6).

Everything here exists because the reference is *spoken*, mid-chapter, one
-handed: "King in Black #3", "the Namor one", "venom 31", "issue 3". Requiring
an exact title would push the cost of the lookup back onto the reader, which is
the friction the whole project is trying to remove.

Pure functions over lightweight candidates, so this is unit-testable without a
DB. Ambiguity is a first-class result: when the input genuinely matches several
issues, the caller is expected to ask, not guess. Guessing here is a milder
version of the Gate B mistake — sending someone to the wrong book.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Words that carry no signal in a spoken reference. "the Namor one" is really
#: just "namor".
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "one", "ones", "issue", "issues", "comic", "comics",
        "book", "of", "that", "this", "in", "and", "from", "read", "just",
        "finished", "number", "no", "vol", "volume", "part",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")
#: A `#N` always wins over a bare trailing number, so the two are separate
#: patterns rather than one alternation — see parse_ref.
_HASH_NUMBER_RE = re.compile(r"#\s*(\d+)")
_TRAILING_NUMBER_RE = re.compile(r"\b(\d{1,4})\s*$")

#: Common spoken shorthands. Kept tiny and explicit — an aggressive alias table
#: would start silently resolving things nobody meant.
_ALIASES = {
    "kib": "king in black",
    "pots": "planet of the symbiotes",
    "rotv": "return of the valkyries",
}


@dataclass(frozen=True)
class Candidate:
    """The minimum an issue must expose to be resolvable."""

    key: str
    series_name: str
    issue_number: int
    position: int | None = None
    role: str = ""
    display: str = ""

    @property
    def label(self) -> str:
        return self.display or f"{self.series_name} #{self.issue_number}"


@dataclass(frozen=True)
class Match:
    candidate: Candidate
    score: float
    why: str


@dataclass(frozen=True)
class Resolution:
    """The outcome. Exactly one of these three shapes:

    - `matched` set, `ambiguous` empty  → resolved
    - `matched` None, `ambiguous` non-empty → ask the human which one
    - both empty → nothing plausible
    """

    query: str
    matched: Candidate | None
    ambiguous: list[Match]

    @property
    def resolved(self) -> bool:
        return self.matched is not None

    @property
    def needs_disambiguation(self) -> bool:
        return self.matched is None and bool(self.ambiguous)


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    for short, long in _ALIASES.items():
        lowered = re.sub(rf"\b{re.escape(short)}\b", long, lowered)
    return [w for w in _WORD_RE.findall(lowered) if w not in _STOPWORDS and not w.isdigit()]


def parse_ref(raw: str) -> tuple[list[str], int | None]:
    """Split a spoken reference into series tokens and an issue number.

    A `#N` always wins over a bare number, so "King in Black #3" is not read as
    a series containing the token "3".
    """
    hashed = _HASH_NUMBER_RE.search(raw)
    number: int | None = int(hashed.group(1)) if hashed else None
    if number is None:
        trailing = _TRAILING_NUMBER_RE.search(raw.strip())
        if trailing:
            number = int(trailing.group(1))
    return _tokens(raw), number


def _score(query_tokens: list[str], candidate: Candidate) -> tuple[float, str]:
    """Token-overlap score in [0, 1], plus a human reason.

    Weighted toward *coverage of the query*: "namor" should score highly against
    "King in Black: Namor" even though it covers only a quarter of that title.
    A prefix match counts, so "valkyr" finds "Valkyries".
    """
    if not query_tokens:
        return 0.0, "no series given"
    cand_tokens = _tokens(candidate.series_name)
    if not cand_tokens:
        return 0.0, "candidate has no series name"

    hits = 0
    for token in query_tokens:
        if any(t == token for t in cand_tokens):
            hits += 1
        elif any(t.startswith(token) or token.startswith(t) for t in cand_tokens):
            hits += 0.75
    coverage = hits / len(query_tokens)
    # Slight preference for the tighter title, so "King in Black" prefers the
    # core book over "King in Black: Namor" when nothing else distinguishes them.
    precision = hits / len(cand_tokens)
    score = 0.8 * coverage + 0.2 * precision
    return min(score, 1.0), f"matched {hits:g}/{len(query_tokens)} query terms"


#: Below this, a name match isn't worth offering at all.
MIN_SCORE = 0.45
#: A leader must beat the runner-up by this much to be taken as unambiguous.
DECISIVE_GAP = 0.15


def resolve(raw: str, candidates: list[Candidate]) -> Resolution:
    query_tokens, number = parse_ref(raw)

    pool = candidates
    if number is not None:
        numbered = [c for c in candidates if c.issue_number == number]
        # If a number was given but matches nothing, it is more likely a typo in
        # the number than in the series, so fall back to name-only rather than
        # returning nothing.
        pool = numbered or candidates

    if not query_tokens:
        # "issue 3" with no series. Only answerable if a core issue exists at
        # that number — that is the book they're most likely holding.
        core = [c for c in pool if c.role == "core"]
        if number is not None and len(core) == 1:
            return Resolution(raw, core[0], [])
        offers = [Match(c, 0.5, "matched on issue number alone") for c in pool[:8]]
        return Resolution(raw, None, offers)

    scored = sorted(
        (Match(c, *_score(query_tokens, c)) for c in pool),
        key=lambda m: (-m.score, m.candidate.position or 10**6),
    )
    viable = [m for m in scored if m.score >= MIN_SCORE]
    if not viable:
        return Resolution(raw, None, [])

    best = viable[0]
    # An explicit issue number is a strong signal: with one it is enough to be
    # the top name match, without one we need a clear gap to the runner-up.
    if number is not None and len(
        [m for m in viable if abs(m.score - best.score) < 1e-9]
    ) == 1:
        return Resolution(raw, best.candidate, [])
    if len(viable) == 1:
        return Resolution(raw, best.candidate, [])
    if best.score - viable[1].score >= DECISIVE_GAP:
        return Resolution(raw, best.candidate, [])
    return Resolution(raw, None, viable[:8])


def candidates_from_guide(entries: list) -> list[Candidate]:
    """Adapt anything with the right attributes into Candidates.

    Takes `GuideEntry`s from the service layer or `CuratedIssue`s straight from
    YAML, which keeps resolution testable with no DB in the picture.
    """
    out: list[Candidate] = []
    for entry in entries:
        series = getattr(entry, "series_name", None) or getattr(entry, "series", "")
        role = getattr(entry, "role", "")
        out.append(
            Candidate(
                key=entry.key,
                series_name=series,
                issue_number=entry.issue_number,
                position=getattr(entry, "position", None),
                role=role.value if hasattr(role, "value") else str(role),
                display=getattr(entry, "display", ""),
            )
        )
    return out
