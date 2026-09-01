"""Loading curation YAML into Postgres, and the refetch/curation boundary.

The load runs on every boot (lifespan.py), so it has to be idempotent, and it
has to be impossible for a subsequent Marvel sync to undo it.
"""

from __future__ import annotations

from sqlalchemy import func, select

from curation.loader import UntraceableDigitalId, load_all, load_event
from marvel.sync import apply_record, promote_availability
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.types import Availability


async def test_load_projects_the_yaml_into_the_db(session, curated_events) -> None:
    report = await load_all(session)
    assert report.events == 1

    event = await session.scalar(select(Event).where(Event.slug == "king-in-black"))
    assert event is not None
    assert event.display_name == "King in Black"

    issues = await session.scalar(select(func.count()).select_from(EventIssue))
    assert issues == len(curated_events[0].issues)
    refs = await session.scalar(select(func.count()).select_from(IssueReference))
    assert refs == len(curated_events[0].references)


async def test_load_is_idempotent(session) -> None:
    """"edit the YAML, deploy, done" only works if a second boot is a no-op."""
    await load_all(session)
    first = await session.scalar(select(func.count()).select_from(EventIssue))
    await load_all(session)
    second = await session.scalar(select(func.count()).select_from(EventIssue))
    assert first == second


async def test_removing_a_line_from_the_yaml_removes_the_row(session, curated_events) -> None:
    """Membership is rebuilt wholesale, so a deleted YAML entry actually
    disappears instead of lingering as a stale row."""
    from dataclasses import replace

    full = curated_events[0]
    await load_event(session, full)
    before = await session.scalar(select(func.count()).select_from(EventIssue))

    # Drop the last issue and renumber so the contiguity rule still holds.
    trimmed_issues = [i for i in full.ordered][:-1]
    trimmed = replace(
        full,
        issues=trimmed_issues,
        references=[
            r
            for r in full.references
            if {r.from_key, r.to_key} <= {i.key for i in trimmed_issues}
        ],
    )
    await load_event(session, trimmed)
    after = await session.scalar(select(func.count()).select_from(EventIssue))
    assert after == before - 1


async def test_loader_refuses_an_unverifiable_digital_id(session, curated_events) -> None:
    """Gate B at load time, not just at test time.

    A curated digital_id with no cached Marvel record behind it is exactly the
    hazard Gate B identified — it opens a different comic rather than failing —
    so the load fails loudly instead of shipping it.
    """
    from dataclasses import replace

    event = curated_events[0]
    faked = replace(
        event,
        issues=[
            replace(i, digital_id=999999) if i.key == "king-in-black-1" else i
            for i in event.ordered
        ],
    )
    try:
        await load_event(session, faked, record_index={})
    except UntraceableDigitalId as exc:
        assert "king-in-black-1" in str(exc)
    else:
        raise AssertionError("an untraceable digital_id was accepted")


async def test_loader_accepts_a_traceable_digital_id(session, curated_events, record_index) -> None:
    from dataclasses import replace

    event = curated_events[0]
    real = record_index["king-in-black-1"]
    verified = replace(
        event,
        issues=[
            replace(i, digital_id=real.digital_id) if i.key == "king-in-black-1" else i
            for i in event.ordered
        ],
    )
    await load_event(session, verified, record_index=record_index)
    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert issue.digital_id == real.digital_id


async def test_a_refetch_cannot_clobber_curation(session, curated_events, record_index) -> None:
    """The single most important structural rule in the project (SPEC §3).

    A curator marks an issue unavailable with a note; Marvel then returns a
    digital id for it. The sync fills in the API-derived fields and leaves the
    curated statement standing.
    """
    from dataclasses import replace

    event = curated_events[0]
    curated_call = replace(
        event,
        issues=[
            replace(
                i,
                availability=Availability.UNAVAILABLE,
                unavailable_note="checked by hand — print only",
            )
            if i.key == "king-in-black-1"
            else i
            for i in event.ordered
        ],
    )
    await load_event(session, curated_call)

    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    promote_availability(issue)
    await session.commit()

    refreshed = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert refreshed.digital_id == record_index["king-in-black-1"].digital_id
    assert refreshed.availability == Availability.UNAVAILABLE.value
    assert refreshed.unavailable_note == "checked by hand — print only"


async def test_reloading_after_a_sync_keeps_the_synced_data(session, record_index) -> None:
    """Boot order is sync-then-reload in practice, so the reload must not blank
    out cover art and dates the sync just fetched."""
    await load_all(session)
    issue = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    apply_record(issue, record_index["king-in-black-1"])
    await session.commit()

    await load_all(session)
    after = await session.scalar(select(Issue).where(Issue.key == "king-in-black-1"))
    assert after.thumbnail_path == record_index["king-in-black-1"].thumbnail_path
    assert after.characters == ["Venom", "Knull"]


async def test_concurrent_loads_do_not_collide(schema, curated_events) -> None:
    """Two boots at once must not race.

    Observed for real on the first Heroku deploy: two lifespans started ~30ms
    apart and collided inserting the same event row, one dying with "duplicate
    key value violates unique constraint" while the other succeeded. The load
    was idempotent but not *concurrent*-safe — a different property, and the one
    a platform that overlaps old and new dynos actually needs.

    Runs against real connections rather than the shared test transaction,
    because the advisory lock is what is under test and a single transaction
    cannot demonstrate it.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from curation.loader import load_all, load_lock
    from db.session import engine

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def boot() -> None:
        async with load_lock(engine), maker() as session:
            await load_all(session)

    try:
        # Without the lock this raises IntegrityError from whichever loses.
        await asyncio.gather(boot(), boot(), boot())

        from sqlalchemy import func, select

        from models.catalog import Event

        async with maker() as session:
            events = await session.scalar(select(func.count()).select_from(Event))
        assert events == len(curated_events), "a concurrent load duplicated an event"
    finally:
        # These committed outside the test transaction, so clean up explicitly.
        from sqlalchemy import delete

        from models.catalog import Event, Issue

        async with maker() as session:
            await session.execute(delete(Event))
            await session.execute(delete(Issue))
            await session.commit()
