"""Marvel Comics API client (gateway.marvel.com/v1/public).

Auth is `md5(ts + privateKey + publicKey)` sent alongside `ts` and `apikey`
(SPEC §5). The client refuses to make a call at all without both keys rather
than sending an unsigned request and reading a 409 back.

Every response is handed to the raw cache verbatim (SPEC §3) — including the
attribution text, which is captured from the response rather than hardcoded,
because Marvel's terms tie the required notice to the data.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from observability import metrics
from observability.tracing import span

BASE_URL = "https://gateway.marvel.com/v1/public"

#: Marvel caps `limit` at 100 and rejects anything higher outright.
MAX_LIMIT = 100


class MarvelCredentialsMissing(RuntimeError):
    """Raised instead of making an unauthenticated call. Get a key at
    https://developer.marvel.com and set MARVEL_PUBLIC_KEY / MARVEL_PRIVATE_KEY."""


class MarvelAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Marvel API {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def auth_params(public_key: str, private_key: str, ts: str | None = None) -> dict[str, str]:
    """The auth triple. `ts` is injectable so tests can assert the hash.

    **md5 is not a choice we get to make.** Marvel's API specifies
    `md5(ts + privateKey + publicKey)`; any other digest fails authentication.
    Flagged by ruff S324 and SonarQube S4790 and suppressed in both places
    deliberately — this is not a password hash and not a signature we verify,
    it is a fixed protocol requirement of a third-party API. The private key is
    never logged, never put on a span, and never leaves this function.
    """
    stamp = ts or str(int(time.time()))
    # NOSONAR S4790 — required by Marvel's documented auth scheme, see above.
    digest = hashlib.md5(  # noqa: S324 — Marvel specifies md5; not our choice
        f"{stamp}{private_key}{public_key}".encode()
    ).hexdigest()
    return {"ts": stamp, "apikey": public_key, "hash": digest}


def params_key(params: dict[str, Any]) -> str:
    """Canonical cache key for a param set.

    The auth triple is stripped: `ts` and `hash` change on every single call,
    so leaving them in would make the cache a write-only log.
    """
    stripped = {k: v for k, v in params.items() if k not in {"ts", "apikey", "hash"}}
    return "&".join(f"{k}={stripped[k]}" for k in sorted(stripped))


@dataclass
class MarvelResponse:
    endpoint: str
    params: dict[str, Any]
    body: dict
    etag: str

    @property
    def params_key(self) -> str:
        return params_key(self.params)

    @property
    def attribution_text(self) -> str:
        return self.body.get("attributionText", "")

    @property
    def results(self) -> list[dict]:
        return ((self.body.get("data") or {}).get("results")) or []

    @property
    def total(self) -> int:
        return int((self.body.get("data") or {}).get("total") or 0)


class MarvelClient:
    def __init__(
        self,
        public_key: str | None,
        private_key: str | None,
        *,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._public_key = public_key
        self._private_key = private_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._public_key and self._private_key)

    async def get(self, endpoint: str, **params: Any) -> MarvelResponse:
        if not self.configured:
            raise MarvelCredentialsMissing(
                "MARVEL_PUBLIC_KEY and MARVEL_PRIVATE_KEY must both be set"
            )
        endpoint = endpoint.strip("/")
        signed = {
            **params,
            **auth_params(self._public_key, self._private_key),  # type: ignore[arg-type]
        }
        url = f"{self._base_url}/{endpoint}"
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        # `endpoint` and the non-auth params only — the span must never carry
        # `apikey` or `hash`, and it leaves the process.
        with span(
            "marvel.get",
            **{"marvel.endpoint": endpoint, "marvel.params": params_key(signed)},
        ):
            try:
                response = await client.get(url, params=signed)
            finally:
                if self._client is None:
                    await client.aclose()
        metrics.record_marvel_request(endpoint, str(response.status_code))

        if response.status_code >= 400:
            # Marvel puts a human-readable reason in `status`; surface it,
            # because "409: Limit greater than 100" is a much better error than
            # a bare status code.
            try:
                message = response.json().get("status", response.text)
            except ValueError:
                message = response.text
            raise MarvelAPIError(response.status_code, str(message))

        return MarvelResponse(
            endpoint=endpoint,
            params=signed,
            body=response.json(),
            etag=response.headers.get("etag", ""),
        )

    async def event_comics(
        self, marvel_event_id: int, *, limit: int = MAX_LIMIT, offset: int = 0
    ) -> MarvelResponse:
        """The issue roster for an event — the endpoint the whole premise rests
        on (SPEC §0: "if digital IDs come back mostly null, the whole linking
        premise needs rethinking")."""
        return await self.get(
            f"events/{marvel_event_id}/comics",
            limit=min(limit, MAX_LIMIT),
            offset=offset,
            orderBy="onsaleDate",
        )

    async def all_event_comics(self, marvel_event_id: int) -> list[MarvelResponse]:
        """Page through the whole roster. Returns every page so each can be
        cached verbatim under its own offset."""
        pages: list[MarvelResponse] = []
        offset = 0
        while True:
            page = await self.event_comics(marvel_event_id, offset=offset)
            pages.append(page)
            offset += MAX_LIMIT
            if offset >= page.total or not page.results:
                return pages

    async def search_comics(
        self, *, title: str | None = None, series_id: int | None = None, limit: int = 20
    ) -> MarvelResponse:
        """Best-effort lookup for `add_to_shelf` candidate resolution."""
        params: dict[str, Any] = {"limit": min(limit, MAX_LIMIT), "orderBy": "-onsaleDate"}
        if title:
            params["titleStartsWith"] = title
        if series_id:
            params["series"] = series_id
        return await self.get("comics", **params)

    async def search_series(self, title: str, *, limit: int = 10) -> MarvelResponse:
        return await self.get("series", titleStartsWith=title, limit=min(limit, MAX_LIMIT))

    async def events(self, *, limit: int = MAX_LIMIT, offset: int = 0) -> MarvelResponse:
        return await self.get("events", limit=min(limit, MAX_LIMIT), offset=offset)
