"""The MCP tool surface — the reason this is not a website.

Six tools, kept small on purpose (SPEC §6). The entry point is someone holding
a physical book saying where they are, one-handed, on a couch. Every tool is
shaped around that: loose input, short output, tappable links.

Mounted at /mcp by main.py behind an OAuth bearer gate. The ASGI middleware
resolves the token to a **User** and stashes it in a contextvar, so every tool
acts as that person — reading lists are per-person and two people share this
deployment.

MCP tools run outside FastAPI's dependency-injection system, so they open their
own DB sessions. Easy to forget on the first write-path tool; conduct hit the
same thing.
"""

from __future__ import annotations

import contextvars
import functools
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from config.settings import get_settings
from curation.resolve import candidates_from_guide, resolve
from db.session import SessionLocal
from marvel.client import MarvelClient
from marvel.links import attribution
from models.types import ShelfSource
from oauth_provider import resolve_access_token
from observability import metrics
from observability.tracing import span
from service import bookmarks as bookmark_service
from service import guide as guide_service
from service import shelf as shelf_service

#: Set per-request by the OAuth middleware; read by the tools.
_principal: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "mcp_principal", default=None
)


def _user_id() -> UUID:
    principal = _principal.get()
    if principal is None:
        raise ToolError("not authenticated")
    return principal["user_id"]


def instrumented(tool_name: str) -> Callable:
    """Wrap a tool in a span plus a call counter labelled by outcome.

    Applied *under* `@mcp.tool()` so the registered callable is the instrumented
    one. `functools.wraps` carries the annotations and docstring across, which is
    what the SDK builds the input schema and tool description from — asserted by
    test, because losing the docstring would silently strip every tool's
    description.

    `outcome` distinguishes `clarify` from `ok` and `error`: a tool that keeps
    asking which issue was meant is not failing, but it *is* a signal that loose
    resolution needs work, and it would be invisible collapsed into "ok".
    """

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            outcome = "ok"
            # Attributes are catalog data only — never a token or a user id.
            with span(f"tool.{tool_name}", **{"mcp.tool": tool_name}) as current:
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    outcome = "error"
                    raise
                else:
                    if isinstance(result, dict) and "need_to_ask" in result:
                        outcome = "clarify"
                    return result
                finally:
                    current.set_attribute("mcp.outcome", outcome)
                    metrics.record_tool_call(
                        tool_name, outcome, time.perf_counter() - started
                    )

        return wrapper

    return decorate


def _transport_security() -> TransportSecuritySettings:
    """The SDK's DNS-rebinding guard defaults to localhost-only, which 421s
    every request once we are behind a public host."""
    host = urlparse(get_settings().public_base_url.rstrip("/")).netloc or "localhost:8000"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=[
            # The real origin, whatever it is configured to be — https in any
            # deployment, since CROSSOVER_PUBLIC_URL is what Claude's connector
            # hits and OAuth requires https there.
            get_settings().public_base_url.rstrip("/"),
            # Loopback-only http, for local development. SonarQube S5332 flags
            # the http:// literals; they are scoped to localhost and 127.0.0.1,
            # which cannot be intercepted off-host, and dropping them would
            # make `make run` unusable without a TLS proxy.
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
    )


#: In mcp 2.x, FastMCP is MCPServer and the transport options moved from the
#: constructor onto streamable_http_app() — see build_mcp_app() at the bottom.
mcp = MCPServer("Crossover")


def _marvel_client() -> MarvelClient:
    settings = get_settings()
    return MarvelClient(settings.marvel_public_key, settings.marvel_private_key)


class NeedsClarification(Exception):
    """The reference matched several issues. Carries the options as data.

    Deliberately *not* surfaced as a tool error. Ambiguity is the expected
    outcome of loose spoken input ("the Namor one" really is two issues), and
    the right response is to ask — so the tools return a normal payload listing
    the options rather than an error string. Guessing here would be a milder
    version of the Gate B mistake: sending someone to the wrong book.
    """

    def __init__(self, issue_ref: str, options: list[dict[str, Any]]) -> None:
        super().__init__(issue_ref)
        self.issue_ref = issue_ref
        self.options = options

    def payload(self) -> dict[str, Any]:
        return {
            "need_to_ask": (
                f"{self.issue_ref!r} matches more than one issue. Ask which one, then "
                "call again with the `issue_ref` they confirm."
            ),
            "options": self.options,
            "attribution": attribution(),
        }


async def _resolve_entry(session, issue_ref: str) -> guide_service.GuideEntry:
    """Turn a spoken reference into exactly one issue.

    Raises NeedsClarification when the input genuinely matches several issues,
    and ToolError when it matches nothing — the latter is a mistake the caller
    can correct, so its message has to reach the client.
    """
    entries = await guide_service.all_entries(session)
    resolution = resolve(issue_ref, candidates_from_guide(entries))
    if resolution.matched:
        metrics.record_resolution("resolved")
        by_key = {e.key: e for e in entries}
        return by_key[resolution.matched.key]
    if resolution.needs_disambiguation:
        metrics.record_resolution("ambiguous")
        raise NeedsClarification(
            issue_ref,
            [
                {"issue_ref": m.candidate.label, "key": m.candidate.key}
                for m in resolution.ambiguous
            ],
        )
    metrics.record_resolution("not_found")
    raise ToolError(
        f"Couldn't find {issue_ref!r} in the curated events. Try list_events() to see "
        "what's covered — coverage outside a few events is best-effort by design."
    )


# --- read-only tools ---


@mcp.tool()
@instrumented("list_events")
async def list_events() -> dict[str, Any]:
    """List the Marvel events this guide covers, and how deeply each is curated.

    `curated` means the reading order and reference edges have been checked;
    `best_effort` means the roster is provisional. Completeness across the
    Marvel catalog is an explicit non-goal — a few events are covered properly
    rather than everything covered badly.
    """
    async with SessionLocal() as session:
        events = await guide_service.list_events(session)
    return {
        "events": [e.to_payload() for e in events],
        "coverage_note": (
            "Deeply curated for a small number of events, best-effort for anything "
            "else."
        ),
        "attribution": attribution(),
    }


@mcp.tool()
@instrumented("get_event_guide")
async def get_event_guide(event_slug: str) -> dict[str, Any]:
    """The full curated reading order for one event, with links and roles.

    Each entry carries its role (core / essential tie-in / optional tie-in) so
    a reader can take the spine only, and a link that is either a real Marvel
    Unlimited URL or an explicit "not on Marvel Unlimited" — never a guess.
    """
    async with SessionLocal() as session:
        try:
            return await guide_service.get_event_guide(session, event_slug)
        except ValueError as exc:
            raise ToolError(
                f"{exc} Call list_events() for the slugs that exist."
            ) from exc


@mcp.tool()
@instrumented("whats_next")
async def whats_next(issue_ref: str) -> dict[str, Any]:
    """Given the issue you just finished, what to read next and what expands on it.

    `issue_ref` is loose on purpose — "King in Black #3", "the Namor one",
    "venom 31" all work, because this gets said out loud mid-chapter.

    Returns the next core issue, anything worth reading before it, and the
    curated reference edges: the omnibus-footnote layer saying which tie-in
    expands the scene you just read. That last part comes from hand curation,
    not from any Marvel API.

    If the reference matches several issues, the result carries `need_to_ask`
    and `options` instead — ask which one and call again.
    """
    async with SessionLocal() as session:
        try:
            entry = await _resolve_entry(session, issue_ref)
        except NeedsClarification as ask:
            return ask.payload()
        return await guide_service.whats_next(session, entry)


# --- write-path tools ---


@mcp.tool()
@instrumented("bookmark_issue")
async def bookmark_issue(
    issue_ref: str, note: str = "", omnibus_page: int | None = None
) -> dict[str, Any]:
    """Save an issue to read later, without leaving the story.

    Meant to cost one sentence: "bookmark the Namor one". `note` is why it
    looked interesting and `omnibus_page` is the page you were on — both
    optional, both worth having a week later when the pile needs sorting.

    If the issue is the target of a curated footnote, the reason it was
    referenced is attached automatically, so the card on the rack explains
    itself.
    """
    async with SessionLocal() as session:
        try:
            entry = await _resolve_entry(session, issue_ref)
        except NeedsClarification as ask:
            # Better to ask than to save the wrong book: an unexplained wrong
            # card on the rack is worse than one extra question.
            return ask.payload()
        reference_id, provenance = await bookmark_service.provenance_for(session, entry)
        bookmark = await bookmark_service.create_bookmark(
            session,
            user_id=_user_id(),
            entry=entry,
            note=note,
            omnibus_page=omnibus_page,
            source_reference_id=reference_id,
            provenance=provenance,
        )
        return {
            "saved": entry.display,
            "bookmark_id": str(bookmark.id),
            "why_you_saved_it": provenance or note or None,
            "link": entry.link_markdown(),
            "note": "On your rack. Keep reading.",
        }


@mcp.tool()
@instrumented("sequence_bookmarks")
async def sequence_bookmarks(ordering: str = "clustered") -> dict[str, Any]:
    """Your saved tie-ins, grouped into batches that read well together.

    Not a chronological sort. Tie-ins jump around — some are flashbacks
    reaching years back, some run parallel to the core issues, some are
    epilogues — so read in publication order they feel like errands rather than
    a story. The default groups them by in-universe timing and character family.

    Every issue keeps all its clustering axes (anchor, franchise, narrative
    role, series, date), so the groups can be reshaped on request without
    another call. Pass ordering="chronological" for strict publication order.
    """
    async with SessionLocal() as session:
        return await bookmark_service.sequence_bookmarks(
            session, _user_id(), ordering=ordering
        )


@mcp.tool()
@instrumented("add_to_shelf")
async def add_to_shelf(
    candidates: list[str],
    source: str = "typed",
    confirm_key: str = "",
    candidate_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Capture issues seen in the wild — a comic shop rack, or a title said aloud.

    This tool takes **text**, not images: read the photo yourself and pass what
    you see. `source` is "photo" or "typed".

    Two phases, because a blurry spine or a variant cover matches the wrong
    record easily:

      1. Call with `candidates` only. Returns match candidates with series,
         number, cover date and cover art. Nothing is confirmed yet — read them
         back and ask, plainly: "I think that's Venom #87 — right?" Someone
         standing in the shop holding the book can correct it in one sentence.
      2. Call again with `candidate_id` and `confirm_key` from step 1 to commit.

    If nobody answers, the entry stays pending on the rack with the original
    text preserved. Pending is the fallback, not the plan.
    """
    try:
        shelf_source = ShelfSource(source)
    except ValueError as exc:
        raise ToolError('source must be "photo" or "typed"') from exc

    async with SessionLocal() as session:
        if confirm_key:
            if not candidate_id:
                raise ToolError(
                    "confirming needs the `candidate_id` from the first call, so the "
                    "choice is matched against the options actually offered"
                )
            try:
                return await shelf_service.confirm(
                    session,
                    user_id=_user_id(),
                    candidate_id=candidate_id,
                    chosen_key=confirm_key,
                    note=note,
                )
            except ValueError as exc:
                # Refusing an unoffered key is Gate B at the commit boundary,
                # and the caller needs to be told why rather than seeing a
                # generic failure.
                raise ToolError(str(exc)) from exc
        if not candidates:
            raise ToolError("pass at least one candidate string")
        return await shelf_service.propose(
            session,
            user_id=_user_id(),
            candidates=candidates,
            source=shelf_source,
            client=_marvel_client(),
        )


# --- OAuth gate (pure ASGI, so SSE streaming is not buffered) ---


def _bearer_from_scope(scope: dict) -> str:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header = value.decode("latin-1")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
    return ""


def _resource_metadata_url() -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/.well-known/oauth-protected-resource"


async def _send_unauthorized(send) -> None:
    challenge = f'Bearer resource_metadata="{_resource_metadata_url()}"'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", challenge.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


class OAuthMiddleware:
    """Bearer-token gate. Resolves the token to a User and exposes it via the
    contextvar the tools read."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _bearer_from_scope(scope)
        principal = None
        if token:
            async with SessionLocal() as session:
                user = await resolve_access_token(session, token)
                if user is not None:
                    principal = {"user_id": user.id, "email": user.email}
        if principal is None:
            await _send_unauthorized(send)
            return

        reset = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(reset)


_mcp_app = None


def build_mcp_app():
    """The /mcp ASGI app: the streamable-HTTP transport behind the OAuth gate.

    Built once and memoized, because `mcp.session_manager` is created lazily by
    `streamable_http_app()` and the lifespan needs the *same* manager instance
    this app is serving. Calling it twice would start a manager nothing routes to.
    """
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = OAuthMiddleware(
            mcp.streamable_http_app(
                streamable_http_path="/",
                json_response=False,
                # Stateless: each request stands alone, which is what a phone on
                # a flaky connection wants and what Heroku's single dyno can serve.
                stateless_http=True,
                transport_security=_transport_security(),
            )
        )
    return _mcp_app
