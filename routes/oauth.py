"""OAuth 2.0 HTTP endpoints: discovery, authorize (with consent), token.

Ported from conduct. Security logic lives in `oauth_provider`; this module is
request parsing, redirects, and rendering.

Approval is "being logged in", and the reader who approves becomes the principal
the issued token acts as. It used to require *admin*, which was a symptom rather
than a policy: the token's identity came from whoever registered the connector,
so the person at the consent screen was only an approver and the gate stood in
for authority they had no other reason to hold. Now that the grant binds to the
signer, the only question worth asking is who they are.
"""

from __future__ import annotations

from base64 import b64decode
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Cookie, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth import resolve_session
from csrf import csrf_protect
from db.session import get_session
from models.user import User
from oauth_provider import (
    ACCESS_TOKEN_TTL,
    OAuthError,
    authenticate_client,
    authorization_server_metadata,
    get_active_client,
    issue_authorization_code,
    protected_resource_metadata,
    redeem_authorization_code,
    redirect_uri_allowed,
    refresh_token_grant,
)
from templates_env import templates

# The consent POST grants a connector access to a whole reading list, which
# makes it the highest-value target on this surface. /oauth/token is exempt
# inside csrf_protect: it is authenticated by client_id + client_secret and is
# never a browser form.
router = APIRouter(
    tags=["oauth"], include_in_schema=False, dependencies=[Depends(csrf_protect)]
)


async def _reader(session: AsyncSession, token: str | None) -> User | None:
    """The signed-in reader, or None. Any active reader may connect their own
    list; nothing here is an admin act."""
    user = await resolve_session(session, token)
    return user if user is not None and user.is_active else None


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_discovery() -> JSONResponse:
    return JSONResponse(authorization_server_metadata())


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_discovery() -> JSONResponse:
    return JSONResponse(protected_resource_metadata())


def _append_query(url: str, params: dict[str, str]) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode({k: v for k, v in params.items() if v})}"


def _redirect_error(
    redirect_uri: str, error: str, state: str, description: str = ""
) -> RedirectResponse:
    params = {"error": error, "error_description": description, "state": state}
    return RedirectResponse(
        _append_query(redirect_uri, params), status_code=status.HTTP_303_SEE_OTHER
    )


def _error_page(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "oauth_error.html", {"message": message},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.get("/oauth/authorize", response_class=HTMLResponse, response_model=None)
async def authorize_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
    scope: str = "",
    state: str = "",
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse | RedirectResponse:
    client = await get_active_client(session, client_id)
    # If the client or redirect can't be trusted, never redirect — show an error.
    if client is None or not redirect_uri_allowed(client, redirect_uri):
        return _error_page(request, "Unknown client_id or unregistered redirect_uri.")
    if response_type != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type", state)
    if code_challenge_method != "S256" or not code_challenge:
        return _redirect_error(redirect_uri, "invalid_request", state, "PKCE S256 required")

    # Whoever is signed in is who the token will act as, so an anonymous
    # visitor is sent to log in rather than refused — signing in *is* the
    # identity step, not a precondition to it.
    reader = await _reader(session, crossover_session)
    if reader is None:
        next_url = f"{request.url.path}?{request.url.query}"
        return RedirectResponse(
            f"/ui/login?next={quote(next_url, safe='')}", status_code=status.HTTP_303_SEE_OTHER
        )

    return templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {
            "client_name": client.name,
            "reader_name": reader.display_name or reader.handle,
            "reader_email": reader.email,
            "scope": scope or "mcp",
            "fields": {
                "response_type": response_type,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "scope": scope,
                "state": state,
            },
        },
    )


@router.post("/oauth/authorize", response_model=None)
async def authorize_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client_id: Annotated[str, Form()],
    redirect_uri: Annotated[str, Form()],
    code_challenge: Annotated[str, Form()],
    decision: Annotated[str, Form()] = "",
    code_challenge_method: Annotated[str, Form()] = "S256",
    scope: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    crossover_session: Annotated[str | None, Cookie()] = None,
) -> HTMLResponse | RedirectResponse:
    reader = await _reader(session, crossover_session)
    if reader is None:
        return _error_page(request, "Sign in to connect a client to your reading list.")
    client = await get_active_client(session, client_id)
    if client is None or not redirect_uri_allowed(client, redirect_uri):
        return _error_page(request, "Unknown client_id or unregistered redirect_uri.")
    if decision != "approve":
        return _redirect_error(redirect_uri, "access_denied", state)

    code = await issue_authorization_code(
        session,
        client=client,
        user_id=reader.id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope,
    )
    return RedirectResponse(
        _append_query(redirect_uri, {"code": code, "state": state}),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _client_creds(request: Request, form) -> tuple[str, str]:
    """Pull client_id/secret from HTTP Basic auth, falling back to the body."""
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = b64decode(header[6:]).decode("utf-8")
            cid, _, secret = decoded.partition(":")
            return cid, secret
        except ValueError:  # UnicodeDecodeError is a ValueError subclass
            return "", ""
    return form.get("client_id", ""), form.get("client_secret", "")


def _token_error(e: OAuthError) -> JSONResponse:
    code = (
        status.HTTP_401_UNAUTHORIZED
        if e.error == "invalid_client"
        else status.HTTP_400_BAD_REQUEST
    )
    return JSONResponse(
        {"error": e.error, "error_description": e.description},
        status_code=code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/oauth/token", response_model=None)
async def token(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> JSONResponse:
    form = await request.form()
    grant_type = form.get("grant_type", "")
    client_id, client_secret = _client_creds(request, form)

    try:
        client = await authenticate_client(session, client_id, client_secret)
        if grant_type == "authorization_code":
            tok = await redeem_authorization_code(
                session,
                client=client,
                code=form.get("code", ""),
                redirect_uri=form.get("redirect_uri", ""),
                code_verifier=form.get("code_verifier", ""),
            )
        elif grant_type == "refresh_token":
            tok = await refresh_token_grant(
                session, client=client, refresh_token=form.get("refresh_token", "")
            )
        else:
            raise OAuthError("unsupported_grant_type", f"{grant_type!r} is not supported")
    except OAuthError as e:
        return _token_error(e)

    return JSONResponse(
        {
            "access_token": tok.raw_access_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": tok.raw_refresh_token,
            "scope": tok.scope,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
