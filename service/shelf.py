"""`add_to_shelf` — capturing issues seen in the wild, in two phases.

**The service does no image recognition** (SPEC §6). Claude reads the photo in
the app and passes resolved text; this module takes strings.

Why two phases. A shelf photo yields several issues at once, blurry spines yield
guesses, and variant covers are distinct records sharing one story — so a cover
photo can easily match the wrong record. Confirmation therefore happens *in
conversation, while the person is standing in the shop holding the book*, not in
a queue they clear later:

  1. `propose` resolves raw text to candidates and returns enough to identify
     each one out loud — series, issue number, cover date, cover thumbnail.
     Nothing confirmed is stored. Claude asks: "I think that's Venom #87 —
     right?" One sentence corrects it.
  2. `confirm` commits the chosen match.

If nobody answers, the entry stays PENDING with the raw text preserved and
surfaces on the rack. Pending is the fallback path, not the primary one — and
per Gate B a pending entry never becomes a linkable id.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from curation.resolve import candidates_from_guide, resolve
from marvel.client import MarvelAPIError, MarvelClient, MarvelCredentialsMissing
from marvel.links import attribution
from marvel.mirror import MIRROR_SOURCE, MirrorClient, Outcome
from marvel.records import ComicRecord, cover_url, parse_comics, series_slug
from marvel.sync import apply_record, promote_availability
from models.bookmark import Bookmark, ShelfCandidate
from models.catalog import Issue
from models.types import (
    Availability,
    BookmarkStatus,
    ShelfSource,
    origin_for_shelf_source,
)
from service.bookmarks import create_bookmark
from service.guide import GuideEntry, all_entries

#: Enough options to be useful out loud, few enough to read aloud in one breath.
MAX_MATCHES = 4

#: Re-exported so callers of this service read one name for the mirror's
#: provenance stamp.
__all__ = ["MAX_MATCHES", "MIRROR_SOURCE", "confirm", "propose"]


def _record_to_match(record: ComicRecord, source: str = "marvel_api") -> dict[str, Any]:
    """A match candidate, described so it can be confirmed verbally.

    Cover date and cover art are both here on purpose: variant covers share a
    story but are separate records, and the art is what someone holding the book
    can actually check against.
    """
    return {
        "source": source,
        "key": record.key,
        "issue": f"{record.series_name} #{record.issue_number}",
        "series": record.series_name,
        "number": record.issue_number,
        "cover_date": record.published_on.isoformat() if record.published_on else None,
        "cover_thumbnail": cover_url(record.thumbnail_path, record.thumbnail_extension),
        # Carried through so `confirm` can commit without a second lookup, but
        # never used to build a link until the human has said yes.
        "digital_id": record.digital_id,
        "marvel_com_issue_id": record.marvel_com_issue_id,
        # The rest of the record, carried so `confirm` can write a *complete*
        # issue row without a second lookup. Phase 1 already paid for this data;
        # re-fetching it would spend another request against a 60/min budget and
        # would fail exactly when the mirror is down.
        "marvel_api_comic_id": record.marvel_api_comic_id,
        "series_slug": record.series_slug,
        "thumbnail_path": record.thumbnail_path,
        "thumbnail_extension": record.thumbnail_extension,
        "characters": list(record.characters),
        "creators": list(record.creators),
        "unlimited_on": record.unlimited_on.isoformat() if record.unlimited_on else None,
        # Gate B's recording half: an id is only linkable if the row can say
        # where it came from. Carried on the match so `confirm` can stamp it
        # onto the issue it creates.
        "digital_id_source": source if record.digital_id else "",
    }


def _entry_to_match(entry: GuideEntry) -> dict[str, Any]:
    return {
        "source": "curated",
        "key": entry.key,
        "issue": entry.display,
        "series": entry.series_name,
        "number": entry.issue_number,
        "cover_date": entry.published_on.isoformat() if entry.published_on else None,
        "cover_thumbnail": entry.cover_url,
        "event": entry.note or None,
        "digital_id": entry.digital_id,
        "marvel_com_issue_id": entry.source_id,
    }


def _curated_matches(raw: str, pool: list[GuideEntry]) -> tuple[list[dict[str, Any]], bool]:
    """Curated matches for raw text, and whether curation was *sure*.

    The flag is trustworthy now, and was not always. `resolve` used to return
    its nearest entry as certain even when that entry shared nothing real with
    the query — "Some Unknown Series" resolved confidently to a King in Black
    tie-in — so an earlier version of this optimization silently suppressed the
    network for arbitrary text. `resolve` now requires a match to clear an
    absolute score, not merely to have no competitor, and a corpus test holds
    that line (tests/unit/test_resolve_confidence.py).

    When curation *is* sure, that answer ends the search: it carries an event
    and a reading position no bare catalogue record has, and topping it up with
    three network alternatives spends a rate-limited request to make the right
    answer harder to pick out.
    """
    resolution = resolve(raw, candidates_from_guide(pool))
    by_key = {e.key: e for e in pool}
    if resolution.matched:
        return [_entry_to_match(by_key[resolution.matched.key])], True
    return [
        _entry_to_match(by_key[m.candidate.key])
        for m in resolution.ambiguous[:MAX_MATCHES]
        if m.candidate.key in by_key
    ], False


async def _marvel_matches(raw: str, client: MarvelClient | None) -> list[dict[str, Any]]:
    if client is None or not client.configured:
        return []
    # Strip a trailing issue number: titleStartsWith wants the series, and
    # "Venom 87" finds nothing while "Venom" finds the run.
    title = raw
    parts = raw.replace("#", " ").split()
    if parts and parts[-1].isdigit():
        title = " ".join(parts[:-1])
        wanted = int(parts[-1])
    else:
        wanted = None
    try:
        response = await client.search_comics(title=title.strip(), limit=40)
    except (MarvelAPIError, MarvelCredentialsMissing):
        # Resolution is best-effort by definition; a Marvel outage should
        # degrade to "pending, confirm later" rather than losing the capture.
        return []
    records = parse_comics(response.body)
    if wanted is not None:
        exact = [r for r in records if r.issue_number == wanted]
        records = exact or records
    return [_record_to_match(r) for r in records[:MAX_MATCHES]]


def record_from_match(match: dict[str, Any]) -> ComicRecord | None:
    """Rebuild the `ComicRecord` a match was built from, or None.

    None for a curated match, which names an issue that already exists in the
    catalog — there is nothing to create and nothing to enrich.

    This exists so the issue created by `confirm` goes through `apply_record`
    like every other API-derived write, rather than a second hand-rolled
    field-by-field copy that could drift from `API_OWNED_COLUMNS`.
    """
    if not match.get("marvel_api_comic_id"):
        return None
    cover_date = match.get("cover_date")
    return ComicRecord(
        marvel_api_comic_id=match["marvel_api_comic_id"],
        series_name=match["series"],
        series_slug=match.get("series_slug") or series_slug(match["series"]),
        issue_number=int(match["number"]),
        title=match.get("issue") or "",
        published_on=date.fromisoformat(cover_date) if cover_date else None,
        digital_id=match.get("digital_id"),
        marvel_com_issue_id=match.get("marvel_com_issue_id"),
        thumbnail_path=match.get("thumbnail_path"),
        thumbnail_extension=match.get("thumbnail_extension"),
        characters=list(match.get("characters") or []),
        creators=list(match.get("creators") or []),
        unlimited_on=(
            date.fromisoformat(match["unlimited_on"]) if match.get("unlimited_on") else None
        ),
    )


async def _mirror_matches(
    raw: str, mirror: MirrorClient | None, limit: int
) -> tuple[list[dict[str, Any]], Outcome]:
    """Candidates from the live mirror, for finds outside the curated set.

    Marvel's API is gone, so without this a comic picked up in a shop that
    belongs to no curated event can never resolve — it goes pending and stays
    there. See `marvel.mirror` for why this one path is allowed to be live.

    Two calls deep on purpose. `candidates` costs one request and returns enough
    to rank; the per-issue detail call that carries the digital id and the cover
    art is made only for the few being offered, because the mirror allows 60
    requests a minute and phase 1 needs the art to be confirmable out loud.
    """
    if mirror is None or limit <= 0:
        return [], Outcome.OK
    found, outcome = await mirror.candidates(raw, limit=limit)
    matches: list[dict[str, Any]] = []
    for candidate in found:
        record, detail_outcome = await mirror.record(candidate.issue_id)
        if record is not None:
            matches.append(_record_to_match(record, source=MIRROR_SOURCE))
        elif detail_outcome.is_failure:
            # Ran out of budget partway through describing the options. Say so:
            # a short list here is not the same as a short list of real matches.
            outcome = detail_outcome
            break
    return matches, outcome


async def _gather_matches(
    raw: str,
    pool: list[GuideEntry],
    client: MarvelClient | None,
    mirror: MirrorClient | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Every source for one raw string, best first, plus whether a lookup failed.

    Order is the point. Curation first and, when it is sure, alone: a curated
    issue carries an event and a reading position that no bare catalogue record
    has. Only an unsure result is topped up from the network.

    The boolean is not "we found nothing" — it is "we could not look". A failed
    lookup says nothing about the comic, and conflating the two is how someone
    holding a book came to be told it does not exist (#29).
    """
    matches, settled = _curated_matches(raw, pool)
    if settled or len(matches) >= MAX_MATCHES:
        return matches, False

    seen = {m["key"] for m in matches}
    matches += [m for m in await _marvel_matches(raw, client) if m["key"] not in seen]
    if len(matches) >= MAX_MATCHES:
        return matches, False

    seen = {m["key"] for m in matches}
    found, outcome = await _mirror_matches(raw, mirror, MAX_MATCHES - len(matches))
    matches += [m for m in found if m["key"] not in seen]
    return matches, outcome.is_failure


async def propose(
    session: AsyncSession,
    *,
    user_id: UUID,
    candidates: list[str],
    source: ShelfSource,
    client: MarvelClient | None = None,
    mirror: MirrorClient | None = None,
) -> dict[str, Any]:
    """Phase 1. Resolve raw text; store the pending entries but nothing confirmed."""
    pool = await all_entries(session)
    results: list[dict[str, Any]] = []

    for raw in candidates:
        raw = raw.strip()
        if not raw:
            continue
        matches, unreachable = await _gather_matches(raw, pool, client, mirror)

        # The pending row is created now so the capture survives even if the
        # conversation ends here — the rack picks it up for confirmation later.
        pending = Bookmark(
            user_id=user_id,
            issue_id=None,
            status=BookmarkStatus.PENDING.value,
            origin=origin_for_shelf_source(source).value,
            raw_text=raw,
            series_name=matches[0]["series"] if matches else "",
            issue_number=matches[0]["number"] if matches else None,
            availability=Availability.UNCONFIRMED.value,
            provenance=f"Seen in the wild ({source.value})",
        )
        session.add(pending)
        await session.flush()
        record = ShelfCandidate(
            user_id=user_id, raw_text=raw, source=source.value, matches=matches[:MAX_MATCHES]
        )
        session.add(record)
        await session.flush()
        entry: dict[str, Any] = {
            "raw_text": raw,
            "candidate_id": str(record.id),
            "pending_bookmark_id": str(pending.id),
            "matches": matches[:MAX_MATCHES],
        }
        # Only present when it happened, so the ordinary payload stays quiet.
        if unreachable:
            entry["catalogue_unavailable"] = True
        results.append(entry)

    await session.commit()
    unreachable_any = any(r.get("catalogue_unavailable") for r in results)
    return {
        "phase": "propose",
        "source": source.value,
        "results": results,
        "next_step": (
            "Nothing is confirmed yet. Read each match back to the person — series, "
            "number, cover date — and call add_to_shelf again with the chosen `key` "
            "and its `candidate_id`. If they don't answer, these stay pending on the "
            "rack with the original text preserved."
            + (
                " Note: entries marked `catalogue_unavailable` could not be looked "
                "up just now — the catalogue was busy or unreachable. That is NOT a "
                "statement that the comic doesn't exist, so don't tell them it "
                "isn't real. Nothing is lost: the entry is on the rack with what "
                "they said. Offer to try again in a moment."
                if unreachable_any
                else ""
            )
        ),
        "attribution": attribution(),
    }


async def confirm(
    session: AsyncSession,
    *,
    user_id: UUID,
    candidate_id: str,
    chosen_key: str,
    note: str = "",
) -> dict[str, Any]:
    """Phase 2. Commit the chosen match against a phase-1 candidate."""
    try:
        record = await session.get(ShelfCandidate, UUID(candidate_id))
    except ValueError as exc:
        raise ValueError("candidate_id is not a valid id") from exc
    if record is None or record.user_id != user_id:
        raise ValueError("no pending shelf candidate with that id")

    match = next((m for m in (record.matches or []) if m.get("key") == chosen_key), None)
    if match is None:
        offered = [m.get("key") for m in record.matches or []]
        raise ValueError(
            f"{chosen_key!r} was not one of the options offered for this candidate "
            f"({offered}). Re-run add_to_shelf rather than committing an id that was "
            "never confirmed against a Marvel record."
        )

    issue = await session.scalar(select(Issue).where(Issue.key == chosen_key))
    if issue is None:
        # An off-event find: create the issue from the confirmed match. The
        # digital_id is copied only because it came from a record for this exact
        # issue in phase 1 — the Gate B rule holds, and `apply_record` stamps
        # which source vouched for it.
        issue = Issue(
            key=chosen_key,
            series_name=match["series"],
            series_slug=match.get("series_slug") or chosen_key.rsplit("-", 1)[0],
            issue_number=int(match["number"]),
            title=match.get("issue") or "",
            availability=Availability.UNCONFIRMED.value,
        )
        # Not `record`: that name is the ShelfCandidate throughout this
        # function, and shadowing it here silently rebound `record.raw_text`.
        comic = record_from_match(match)
        if comic is not None:
            # The same write path a sync uses, so cover art, dates, creators and
            # characters land too — an off-event find used to create a row with
            # none of them — and so this can never write a curated column.
            apply_record(issue, comic, source=match.get("digital_id_source") or "marvel-api")
            promote_availability(issue)
        session.add(issue)
        await session.flush()

    entry = GuideEntry(
        key=issue.key,
        position=0,
        series_name=issue.series_name,
        issue_number=issue.issue_number,
        title=issue.title,
        published_on=issue.published_on,
        role="",
        narrative_role="",
        franchise="",
        note="",
        availability=issue.availability,
        provisional=issue.provisional,
        digital_id=issue.digital_id,
        source_id=issue.source_id,
        unlimited_on=issue.unlimited_on,
        issue_id=issue.id,
        thumbnail_path=issue.thumbnail_path,
        thumbnail_extension=issue.thumbnail_extension,
        characters=list(issue.characters or []),
    )

    # Retire the pending placeholder rather than leaving a duplicate on the rack.
    stale = await session.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user_id,
            Bookmark.raw_text == record.raw_text,
            Bookmark.status == BookmarkStatus.PENDING.value,
        )
    )
    if stale is not None:
        await session.delete(stale)
        await session.flush()

    bookmark = await create_bookmark(
        session,
        user_id=user_id,
        entry=entry,
        note=note,
        origin=origin_for_shelf_source(ShelfSource(record.source)),
        raw_text=record.raw_text,
        provenance=f'Seen in the wild ({record.source}) — "{record.raw_text}"',
    )
    record.resolved_bookmark_id = bookmark.id
    await session.commit()
    return {
        "phase": "confirm",
        "saved": f"{issue.series_name} #{issue.issue_number}",
        "bookmark_id": str(bookmark.id),
        "on_the_rack": True,
        "attribution": attribution(),
    }


async def pending_for_user(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Unconfirmed shelf candidates, for the rack's confirmation section."""
    rows = (
        await session.scalars(
            select(ShelfCandidate)
            .where(
                ShelfCandidate.user_id == user_id,
                ShelfCandidate.resolved_bookmark_id.is_(None),
            )
            .order_by(ShelfCandidate.created_at.desc())
        )
    ).all()
    return [
        {
            "candidate_id": str(r.id),
            "raw_text": r.raw_text,
            "source": r.source,
            "matches": r.matches or [],
        }
        for r in rows
    ]
