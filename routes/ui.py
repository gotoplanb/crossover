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

import hmac
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_COOKIE,
    SESSION_TTL,
    authenticate,
    create_session,
    hash_password,
    resolve_session,
    revoke_session,
)
from config.settings import get_settings
from csrf import CSRF_COOKIE, csrf_protect, new_token, token_for
from db.session import get_session
from models.bookmark import Bookmark
from models.catalog import Event, EventIssue, Issue, IssueReference
from models.user import User, valid_handle
from service import bookmarks as bookmark_service
from service import guide as guide_service
from service import shelf as shelf_service
from service.sequencing import build_clusters
from templates_env import templates

RACK_URL = "/ui/rack"
_UNKNOWN_EVENT = "Unknown event"

# CSRF is enforced on the router, not per-route, so a form added later cannot
# silently omit it. See csrf.py.
router = APIRouter(
    prefix="/ui",
    tags=["ui"],
    include_in_schema=False,
    dependencies=[Depends(csrf_protect)],
)


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
    # Signing in while already signed in is a real case here — two people share
    # a machine and hand it over. csrf_protect will then expect the *existing
    # session's* token, so the form has to carry that one; minting a pre-auth
    # token would render a form that can never be submitted.
    existing = token_for(request)
    # No session: the token is double-submitted from a cookie issued here.
    # Reused when already present, so opening the page in two tabs does not
    # invalidate the first. Set on request.state *before* rendering — Starlette
    # renders a TemplateResponse in its constructor, so assigning afterwards
    # produces a form with an empty token and a guaranteed 403.
    token = existing or request.cookies.get(CSRF_COOKIE) or new_token()
    request.state.csrf_token = token

    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next,
            "error": None,
            "registration_open": get_settings().registration_open,
        },
    )
    if existing:
        return response

    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        secure=get_settings().ui_cookie_secure,
        samesite="lax",
    )
    return response


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
    # One message for every failure — a wrong password, an unknown handle and a
    # deactivated reader are indistinguishable, so the form cannot be used to
    # enumerate who exists.
    user = await authenticate(session, handle, password)
    if user is None:
        # Same ordering rule as above: state before render.
        request.state.csrf_token = request.cookies.get(CSRF_COOKIE) or token_for(request)
        failed = templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next,
                "error": "Wrong password, or no such reader.",
                "registration_open": get_settings().registration_open,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        # Keep the same token so a mistyped password does not leave a form that
        # can never be submitted.
        if request.cookies.get(CSRF_COOKIE):
            failed.set_cookie(
                CSRF_COOKIE,
                request.cookies[CSRF_COOKIE],
                httponly=True,
                secure=get_settings().ui_cookie_secure,
                samesite="lax",
            )
        return failed

    # A fresh session per sign-in, so signing in again anywhere does not extend
    # the life of a cookie captured earlier.
    token = await create_session(session, user, user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse(next or RACK_URL, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=get_settings().ui_cookie_secure,
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    # The pre-auth token has done its job; the session carries its own from here.
    response.delete_cookie(CSRF_COOKIE)
    return response


def _register_page(
    request: Request,
    *,
    error: str | None = None,
    values: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the registration form, preserving what was typed.

    Same CSRF dance as the login form, and for the same reason: the page has no
    session yet, so its token is double-submitted from a cookie, and
    `request.state` must be set *before* the response is constructed because
    Starlette renders in the constructor.
    """
    token = request.cookies.get(CSRF_COOKIE) or new_token()
    request.state.csrf_token = token
    response = templates.TemplateResponse(
        request,
        "register.html",
        {
            "error": error,
            "values": values or {},
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        secure=get_settings().ui_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/register", response_class=HTMLResponse, response_model=None)
async def register_form(request: Request) -> HTMLResponse:
    # 404 rather than a "registration is closed" page: a deployment that has not
    # opted in should not advertise that the route exists at all.
    if not get_settings().registration_open:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _register_page(request)


@router.post("/register", response_model=None)
async def register_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    # Every field defaults to empty so an omitted one is rejected below with a
    # readable message rather than by FastAPI with a 422.
    invite_code: Annotated[str, Form()] = "",
    handle: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    if not settings.registration_open:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    handle = handle.strip().lower()
    email = email.strip().lower()
    kept = {"handle": handle, "email": email, "display_name": display_name}

    def refuse(message: str) -> HTMLResponse:
        return _register_page(
            request, error=message, values=kept, status_code=status.HTTP_400_BAD_REQUEST
        )

    # Constant-time: the invite code is a shared secret, and `==` on a secret
    # leaks its prefix to anyone patient enough to measure.
    if not hmac.compare_digest(invite_code.strip(), settings.invite_code or ""):
        return refuse("That invite code isn't valid.")
    if not valid_handle(handle):
        return refuse(
            "A handle starts with a letter and uses only lowercase letters, digits and underscores."
        )
    if "@" not in email or len(email) < 3:
        return refuse("That doesn't look like an email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        return refuse(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password != password_confirm:
        return refuse("Those passwords don't match.")

    taken = await session.scalar(
        select(User).where((User.handle == handle) | (User.email == email))
    )
    if taken is not None:
        return refuse("That handle or email is already registered.")

    # `is_admin` is never read from the form. Admin is granted deliberately, by
    # someone who already has it — a registration form that could grant it would
    # make the invite code the only thing standing between a stranger and the
    # curation views.
    user = User(
        email=email,
        handle=handle,
        display_name=display_name.strip() or handle.title(),
        password_hash=hash_password(password),
        is_admin=False,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Two people claiming one handle at once. The check above makes this
        # rare; the unique constraint is what makes it impossible.
        await session.rollback()
        return refuse("That handle or email is already registered.")
    await session.refresh(user)

    token = await create_session(session, user, user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse(RACK_URL, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.ui_cookie_secure,
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    response.delete_cookie(CSRF_COOKIE)
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
    return templates.TemplateResponse(request, "partials/read_toggle.html", {"bookmark": updated})


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
        select(EventIssue).where(EventIssue.event_id == event.id, EventIssue.issue_id == issue.id)
    )
    if current is not None:
        step = -1 if direction == "up" else 1
        neighbour = await session.scalar(
            select(EventIssue).where(
                EventIssue.event_id == event.id, EventIssue.position == current.position + step
            )
        )
        if neighbour is not None:
            # Three steps with a flush between each, because (event_id, position)
            # is unique and the database must never hold two rows at the same
            # position, even mid-transaction.
            #
            # Parking one row and then assigning both before a single commit is
            # not enough: SQLAlchemy orders UPDATEs by primary key, not by
            # dependency, so whether the pair collided depended on which row
            # happened to have the lower id. That made reordering fail
            # intermittently with a constraint violation.
            vacated = current.position
            current.position = -1
            await session.flush()

            target = neighbour.position
            neighbour.position = vacated
            await session.flush()

            current.position = target
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
