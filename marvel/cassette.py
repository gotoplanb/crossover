"""Recorded mirror responses, replayed instead of refetched.

The metadata mirror allows 60 requests a minute, per IP, and that budget is not
ours alone — see issue #28. Two things were spending it needlessly:

- **Reseeding.** Rebuilding one 40-issue snapshot costs roughly 80 requests, so
  it cannot even complete inside a single window. Doing that repeatedly while
  iterating on curation is the largest avoidable draw on the budget.
- **Tests asserting invented shapes.** The suite already never touches the
  network, but its fixtures were hand-written dicts. They prove the parser is
  self-consistent, not that it reads what the mirror actually sends — so a
  change in the real response format would keep every test green.

Both are solved by recording real responses once and replaying them. This is
deliberately built as an httpx *transport* rather than a flag inside
`MirrorClient`: the client already accepts an `httpx.AsyncClient`, so recording
and replay need no changes to it, and no production code path gains a branch
that only tests and scripts use.

    async with httpx.AsyncClient(transport=replay(FIXTURES)) as http:
        records = await MirrorClient(client=http).candidates("Daredevil 181")

A miss during replay raises rather than returning empty. Silently answering
"nothing found" would look exactly like the mirror's own not-found and would
make a stale cassette read as a passing test.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

#: Where recorded responses live. Committed on purpose — they are the evidence
#: that the parser handles real payloads, so they belong in review.
DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mirror"

_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


class CassetteMiss(LookupError):
    """A replayed request has no recording.

    Not an httpx error on purpose: `MirrorClient._get` swallows those and
    degrades to "no candidates", which is right against a live mirror and wrong
    here. A missing recording is a developer error and has to be loud.
    """


def cassette_name(request: httpx.Request) -> str:
    """A stable, human-readable filename for one request.

    Readable because these are committed and reviewed: someone looking at a diff
    should see which query changed without decoding a hash. The hash suffix only
    breaks ties, since the readable part is truncated and lossy.
    """
    path = request.url.path.removeprefix("/v1/").strip("/")
    query = str(request.url.params)
    readable = _UNSAFE.sub("-", f"{path}-{query}").strip("-").lower()[:80]
    digest = hashlib.sha256(f"{request.method} {request.url}".encode()).hexdigest()[:8]
    return f"{readable}-{digest}.json"


def _write(directory: Path, request: httpx.Request, response: httpx.Response, body: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        # Recorded for readability in review, not used on replay — replay is
        # keyed by filename, so an edited URL here cannot silently mismatch.
        "request": {"method": request.method, "url": str(request.url)},
        "response": {"status": response.status_code},
    }
    try:
        payload["response"]["json"] = json.loads(body)
    except ValueError:
        payload["response"]["text"] = body.decode("utf-8", "replace")
    (directory / cassette_name(request)).write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n"
    )


#: Headers that describe the *encoding of the original stream* rather than the
#: content. Meaningless once the body has been read into memory, and actively
#: harmful if replayed alongside a decoded body.
_STRIPPED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def _replayable_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.multi_items() if k.lower() not in _STRIPPED_HEADERS]


class RecordingTransport(httpx.AsyncBaseTransport):
    """Performs the real request, then writes the response to disk.

    Used once, deliberately, by `scripts/record_mirror.py`. Nothing in the app
    or the test suite records — a test that could reach the network would spend
    the shared budget on every CI run.
    """

    def __init__(self, directory: Path = DEFAULT_CASSETTE_DIR) -> None:
        self._directory = Path(directory)
        self._inner = httpx.AsyncHTTPTransport()
        self.recorded = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        if response.status_code < 400:
            _write(self._directory, request, response, body)
            self.recorded += 1
        # The stream is consumed, so hand back a fresh response over the bytes.
        # The original headers must NOT be reused wholesale: `aread()` already
        # decompressed the body, so carrying `Content-Encoding: gzip` through
        # makes httpx decode it a second time. That fails silently — `_get`
        # catches the error and returns None — so every lookup came back as
        # "no results" while the recordings on disk were perfectly good.
        return httpx.Response(
            status_code=response.status_code,
            headers=_replayable_headers(response.headers),
            content=body,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def response_from(path: Path, request: httpx.Request | None = None) -> httpx.Response:
    """Rebuild a response from a recording. Shared by replay and by the
    recorder's skip-if-already-present path, so the two can never diverge."""
    recorded = json.loads(path.read_text())["response"]
    if "json" in recorded:
        return httpx.Response(recorded["status"], json=recorded["json"], request=request)
    return httpx.Response(recorded["status"], text=recorded.get("text", ""), request=request)


class SyncRecordingTransport(httpx.BaseTransport):
    """`RecordingTransport` for a synchronous client.

    Exists because `scripts/fetch_snapshot.py` is a straight-line batch script —
    it paces itself with `time.sleep` and waits out rate limits, which an async
    client would only complicate. Same contract, same files on disk, so a
    snapshot recorded here replays through the async client and vice versa.
    """

    def __init__(self, directory: Path = DEFAULT_CASSETTE_DIR) -> None:
        self._directory = Path(directory)
        self._inner = httpx.HTTPTransport()
        self.recorded = 0
        self.skipped = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = self._directory / cassette_name(request)
        if path.exists():
            self.skipped += 1
            return response_from(path, request)
        response = self._inner.handle_request(request)
        body = response.read()
        response.close()
        if response.status_code < 400:
            _write(self._directory, request, response, body)
            self.recorded += 1
        return httpx.Response(
            status_code=response.status_code,
            headers=_replayable_headers(response.headers),
            content=body,
            request=request,
        )


def replay(directory: Path = DEFAULT_CASSETTE_DIR) -> httpx.MockTransport:
    """A transport that serves recorded responses and never touches the network.

    `httpx.MockTransport` implements both the sync and async interfaces, so this
    one function serves `MirrorClient` and the snapshot script alike.
    """
    directory = Path(directory)

    def handler(request: httpx.Request) -> httpx.Response:
        path = directory / cassette_name(request)
        if not path.exists():
            raise CassetteMiss(
                f"no recording for {request.method} {request.url}\n"
                f"  expected: {path}\n"
                f"  record it with: make record-mirror"
            )
        return response_from(path, request)

    return httpx.MockTransport(handler)


def available(directory: Path = DEFAULT_CASSETTE_DIR) -> int:
    directory = Path(directory)
    return len(list(directory.glob("*.json"))) if directory.exists() else 0
