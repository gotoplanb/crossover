"""Export DB curation state back to YAML.

The curation admin view edits the database, but the database is not the source
of truth — the git-tracked YAML is (SPEC §3), and Heroku's filesystem is
ephemeral (SPEC §2), so the app cannot write the file itself. This closes the
loop: fiddle with the order in the browser, export, commit the diff, deploy.

Emitting YAML by hand rather than via `yaml.dump` is deliberate: the whole
argument for keeping curation in git is that it is diffable, and a dumper that
reorders keys or reflows strings produces diffs nobody can read.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.catalog import Event, EventIssue, Issue, IssueReference
from service.guide import event_entries


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _header_lines(event) -> list[str]:
    lines = [
        f"# Exported from the curation view for {event.display_name}.",
        "# Review the diff before committing — this file is the source of truth,",
        "# and the database it came from is rebuilt from it on every boot.",
        "",
        f"slug: {event.slug}",
        f"display_name: {_quote(event.display_name)}",
        f"curation_status: {event.curation_status}",
        f"marvel_event_id: {event.marvel_event_id if event.marvel_event_id else 'null'}",
    ]
    if event.started_on:
        lines.append(f"started_on: {event.started_on.isoformat()}")
    if event.ended_on:
        lines.append(f"ended_on: {event.ended_on.isoformat()}")
    if event.summary:
        lines += ["", "summary: >"]
        lines += [
            f"  {line.strip()}"
            for line in event.summary.strip().splitlines()
            if line.strip()
        ]
    return lines


def _core_lines(entries) -> list[str]:
    """Re-derive the `core:` declaration from the exported entries.

    Without it the "every core issue is present and ordered" gate has nothing to
    check against, so a round-tripped file would silently lose that protection.
    """
    core = [e for e in entries if e.is_core]
    if not core:
        return []
    return ["", "core:", f"  series: {_quote(core[0].series_name)}", f"  count: {len(core)}"]


def _issue_lines(entry) -> list[str]:
    lines = [
        f"  - key: {entry.key}",
        f"    position: {entry.position}",
        f"    series: {_quote(entry.series_name)}",
        f"    number: {entry.issue_number}",
    ]
    if entry.published_on:
        lines.append(f"    published_on: {entry.published_on.isoformat()}")
    lines += [
        f"    role: {entry.role}",
        f"    narrative_role: {entry.narrative_role}",
        f"    franchise: {entry.franchise}",
    ]
    # Only ever emitted when it came from a Marvel response for this issue
    # (Gate B). The loader re-verifies it against the cache on the way back in.
    if entry.digital_id:
        lines.append(f"    digital_id: {entry.digital_id}")
    if entry.availability != "unconfirmed":
        lines.append(f"    availability: {entry.availability}")
    if entry.unavailable_note:
        lines.append(f"    unavailable_note: {_quote(entry.unavailable_note)}")
    if entry.provisional:
        lines.append("    provisional: true")
    if entry.note:
        lines.append(f"    note: {_quote(entry.note)}")
    lines.append("")
    return lines


def _reference_lines(ref, source_key: str, target_key: str) -> list[str]:
    lines = [
        f"  - from: {source_key}",
        f"    to: {target_key}",
        f"    type: {ref.relation_type}",
    ]
    if ref.note:
        lines.append(f"    note: {_quote(ref.note)}")
    lines.append(f"    omnibus_page: {ref.omnibus_page if ref.omnibus_page else 'null'}")
    lines.append("")
    return lines


async def _reference_rows(session: AsyncSession, event_id):
    """Reference edges whose *source* is in this event, in reading order."""
    return (
        await session.execute(
            select(IssueReference, Issue)
            .join(Issue, Issue.id == IssueReference.from_issue_id)
            .join(EventIssue, EventIssue.issue_id == IssueReference.from_issue_id)
            .where(EventIssue.event_id == event_id)
            .order_by(EventIssue.position, IssueReference.id)
        )
    ).all()


async def export_event_yaml(session: AsyncSession, slug: str) -> str:
    event, entries = await event_entries(session, slug)

    lines = [*_header_lines(event), *_core_lines(entries), "", "issues:"]
    for entry in entries:
        lines += _issue_lines(entry)

    rows = await _reference_rows(session, event.id)
    if rows:
        lines.append("references:")
        for ref, source in rows:
            target = await session.get(Issue, ref.to_issue_id)
            if target is None:  # pragma: no cover
                # Unreachable in practice: `issue_references.to_issue_id` has a
                # foreign key with ON DELETE CASCADE, so an issue cannot vanish
                # while an edge still points at it. Kept as a guard only for the
                # concurrent case — another transaction deleting the issue
                # between the edge query and this lookup — where the alternative
                # is an AttributeError mid-export.
                continue
            lines += _reference_lines(ref, source.key, target.key)
    return "\n".join(lines).rstrip() + "\n"


async def event_slugs(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(Event.slug).order_by(Event.slug))).all())
