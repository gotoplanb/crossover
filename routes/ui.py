"""The web surface — the second half of the product, not admin scaffolding.

SPEC §7 divides the two surfaces by *moment*: MCP is mid-read, book in hand,
spoken and fast; the web is later, deliberate, visual. The realistic pattern is
a burst of "bookmark that one" while reading, then "resort my list", then
switching to a screen to actually work through it.

So the rack is the centerpiece: covers, grouped by the clustering
`sequence_bookmarks` produced, with provenance on every card. The curation view
at the bottom of this module is the one part that is genuinely just admin and is
allowed to stay ugly.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    create_session,
    resolve_session,
    revoke_session,
    verify_reader_password,
)
from config.settings import get_settings
from db.session import get_session
from models.bookmark import Bookmark
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.user import User
from service import bookmarks as bookmark_service
from service import guide as guide_service
from service import shelf as shelf_service
from service.sequencing import build_clusters
from templates_env import templates

RACK_URL = "/ui/rack"
_UNKNOWN_EVENT = "Unknown event"

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


def _login_redirect(next_url: str = RACK_URL) -> RedirectResponse:
    return RedirectResponse(f"/ui/login?next={next_url}", status_code=status.HTTP_303_SEE_OTHER)


async def _current_user(session: AsyncSession, token: str | None) -> User | None:
    """The signed-in reader, resolved from a revocable session token.

    Same identity as the MCP principal (SPEC §7), so a bookmark made by voice
    mid-chapter shows up here with no sync step.
    """
    return await resolve_session(session, token)


async def _current_admin(session: AsyncSession, token: str | None) -> User | None:
    """The signed-in reader, if they are an admin.

    Admin is a property of the reader rather than a second cookie, so there is
    one credential to steal instead of two, and revoking a session revokes
    curation access with it.
    """
    user = await _current_user(session, token)
    return user if user is not None and user.is_admin else None


# --- session ---


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    next: str = RACK_URL,
) -> HTMLResponse:
    users = (await session.scalars(select(User).where(User.is_active.is_(True)))).all()
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "users": users, "error": None}
    )


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    # Both default to empty so a blank or omitted field is rejected by the
    # check below with the same 401 as a wrong password, rather than by
    # FastAPI's validation with a 422 — which would tell an attacker that the
    # submission shape, not the credential, was the problem.
    handle: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = RACK_URL,
) -> HTMLResponse | RedirectResponse:
    user = await session.scalar(select(User).where(User.handle == handle))
    # One message for every failure — a wrong password, an unknown handle and a
    # deactivated reader are indistinguishable, so the form cannot be used to
    # enumerate who exists.
    if user is None or not user.is_active or not verify_reader_password(handle, password):
        users = (await session.scalars(select(User).where(User.is_active.is_(True)))).all()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "users": users, "error": "Wrong password, or no such reader."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # A fresh session per sign-in, so signing in again anywhere does not extend
    # the life of a cookie captured earlier.
    token = await create_session(
        session, user, user_agent=request.headers.get("user-agent", "")
    )
    response = RedirectResponse(next or RACK_URL, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=get_settings().ui_cookie_secure,
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return response


@router.post("/logout", response_model=None)
async def logout(
    session: Annotated[AsyncSession, Depends(get_session)],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Sign out, and *revoke* the session rather than only dropping the cookie.

    Clearing the cookie alone would leave a token that still authenticates if it
    was captured — which is precisely the weakness this replaced.
    """
    await revoke_session(session, crossover_session)
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- the rack (SPEC §7 centerpiece) ---


@router.get("/rack", response_class=HTMLResponse, response_model=None)
async def rack(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse | RedirectResponse:
    user = await _current_user(session, crossover_session)
    if user is None:
        return _login_redirect()
    items = await bookmark_service.seq_items(session, user.id)
    bookmarks = {str(b.id): b for b in await bookmark_service.user_bookmarks(session, user.id)}
    return templates.TemplateResponse(
        request,
        "rack.html",
        {
            "user": user,
            "clusters": build_clusters(items),
            "bookmarks": bookmarks,
            "pending": await shelf_service.pending_for_user(session, user.id),
            "total": len(items),
        },
    )


@router.post("/rack/{bookmark_id}/read", response_class=HTMLResponse, response_model=None)
async def toggle_read(
    request: Request,
    bookmark_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse | RedirectResponse:
    user = await _current_user(session, crossover_session)
    if user is None:
        return _login_redirect()
    existing = await session.get(Bookmark, bookmark_id)
    if existing is None or existing.user_id != user.id:
        return HTMLResponse("", status_code=status.HTTP_404_NOT_FOUND)
    updated = await bookmark_service.mark_read(
        session, user.id, bookmark_id, read=existing.read_at is None
    )
    # HTMX swaps just this control back in.
    return templates.TemplateResponse(
        request, "partials/read_toggle.html", {"bookmark": updated}
    )


@router.post("/rack/confirm", response_model=None)
async def confirm_pending(
    session: Annotated[AsyncSession, Depends(get_session)],
    candidate_id: Annotated[str, Form()],
    chosen_key: Annotated[str, Form()],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Confirm a shelf candidate nobody answered about in conversation.

    The fallback path (SPEC §6): confirmation is supposed to happen out loud in
    the shop; this is for when it didn't.
    """
    user = await _current_user(session, crossover_session)
    if user is None:
        return _login_redirect()
    # A stale or already-confirmed candidate is not an error worth a page for:
    # the rack re-renders and shows the truth either way.
    with suppress(ValueError):
        await shelf_service.confirm(
            session, user_id=user.id, candidate_id=candidate_id, chosen_key=chosen_key
        )
    return RedirectResponse(RACK_URL, status_code=status.HTTP_303_SEE_OTHER)


# --- event guide browser ---


@router.get("/events", response_class=HTMLResponse)
async def events_index(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "events.html", {"events": await guide_service.list_events(session)}
    )


@router.get("/events/{slug}", response_class=HTMLResponse, response_model=None)
async def event_guide(
    request: Request, slug: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> HTMLResponse:
    try:
        event, entries = await guide_service.event_entries(session, slug)
    except ValueError:
        return HTMLResponse(_UNKNOWN_EVENT, status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "event_guide.html",
        {"event": event, "entries": entries},
    )


# --- curation admin (allowed to stay ugly) ---


@router.get("/curate/{slug}", response_class=HTMLResponse, response_model=None)
async def curate(
    request: Request,
    slug: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse | RedirectResponse:
    if await _current_admin(session, crossover_session) is None:
        return _login_redirect(f"/ui/curate/{slug}")
    try:
        event, entries = await guide_service.event_entries(session, slug)
    except ValueError:
        return HTMLResponse(_UNKNOWN_EVENT, status_code=status.HTTP_404_NOT_FOUND)
    refs = (
        await session.execute(
            select(IssueReference, Issue)
            .join(Issue, Issue.id == IssueReference.from_issue_id)
            .order_by(IssueReference.id)
        )
    ).all()
    keys = {e.key for e in entries}
    return templates.TemplateResponse(
        request,
        "curate.html",
        {
            "event": event,
            "entries": entries,
            "references": [(r, i) for r, i in refs if i.key in keys],
            "issue_by_id": {e.issue_id: e for e in entries},
        },
    )


@router.post("/curate/{slug}/move", response_model=None)
async def curate_move(
    slug: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    issue_key: Annotated[str, Form()],
    direction: Annotated[str, Form()],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Swap one issue with its neighbour.

    A swap, not a re-index, so positions stay dense and the contiguity gate
    (SPEC §8) can never be broken by fiddling in the UI.
    """
    if await _current_admin(session, crossover_session) is None:
        return _login_redirect(f"/ui/curate/{slug}")
    event = await session.scalar(select(Event).where(Event.slug == slug))
    issue = await session.scalar(select(Issue).where(Issue.key == issue_key))
    if event is None or issue is None:
        return RedirectResponse(f"/ui/curate/{slug}", status_code=status.HTTP_303_SEE_OTHER)
    current = await session.scalar(
        select(EventIssue).where(
            EventIssue.event_id == event.id, EventIssue.issue_id == issue.id
        )
    )
    if current is not None:
        step = -1 if direction == "up" else 1
        neighbour = await session.scalar(
            select(EventIssue).where(
                EventIssue.event_id == event.id, EventIssue.position == current.position + step
            )
        )
        if neighbour is not None:
            # Park one position out of range first: (event_id, position) is
            # unique, so a direct swap trips the constraint mid-statement.
            parked, current.position = current.position, -1
            await session.flush()
            neighbour.position, current.position = parked, neighbour.position
            await session.commit()
    return RedirectResponse(f"/ui/curate/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/curate/{slug}/reference", response_model=None)
async def curate_add_reference(
    slug: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    from_key: Annotated[str, Form()],
    to_key: Annotated[str, Form()],
    relation_type: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    omnibus_page: Annotated[str, Form()] = "",
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> RedirectResponse:
    """Add a reference edge by hand — the omnibus-footnote layer."""
    if await _current_admin(session, crossover_session) is None:
        return _login_redirect(f"/ui/curate/{slug}")
    source = await session.scalar(select(Issue).where(Issue.key == from_key))
    target = await session.scalar(select(Issue).where(Issue.key == to_key))
    if source is not None and target is not None and source.id != target.id:
        session.add(
            IssueReference(
                from_issue_id=source.id,
                to_issue_id=target.id,
                relation_type=relation_type,
                note=note,
                omnibus_page=int(omnibus_page) if omnibus_page.strip().isdigit() else None,
            )
        )
        await session.commit()
    return RedirectResponse(f"/ui/curate/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/curate/{slug}/export.yaml", response_class=PlainTextResponse, response_model=None)
async def curate_export(
    slug: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> PlainTextResponse | RedirectResponse:
    """Export the current DB state back to curation YAML.

    This is the load-bearing half of the admin view. Heroku's filesystem is
    ephemeral (SPEC §2), so edits made here do **not** survive a redeploy — the
    git-tracked YAML is the source of truth. Export, commit, deploy. Anything
    not exported is lost, and that is the honest cost of ephemeral storage.
    """
    if await _current_admin(session, crossover_session) is None:
        return _login_redirect(f"/ui/curate/{slug}")
    from curation.export import export_event_yaml

    try:
        body = await export_event_yaml(session, slug)
    except ValueError:
        return PlainTextResponse(_UNKNOWN_EVENT, status_code=status.HTTP_404_NOT_FOUND)
    return PlainTextResponse(
        body,
        headers={"Content-Disposition": f'attachment; filename="{slug}.yaml"'},
    )
