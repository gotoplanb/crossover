"""Record real mirror responses into committed fixtures. Run rarely, on purpose.

The mirror allows 60 requests a minute per IP (#28), so this paces itself to
stay under that and is the only thing in the repo permitted to reach the mirror
outside the running app. Tests and `fetch_snapshot --offline` replay what it
captures; neither ever records, because a test that could reach the network
would spend the shared budget on every CI run.

    make record-mirror                      # the default query set
    python -m scripts.record_mirror --query "Daredevil 181"

Re-running is cheap: an already-recorded request is skipped rather than
refetched, so adding one query costs one query.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from marvel.cassette import (
    DEFAULT_CASSETTE_DIR,
    RecordingTransport,
    available,
    cassette_name,
    response_from,
)
from marvel.mirror import MirrorClient

#: Queries the suite and the shelf path actually use. Kept small and explicit:
#: every entry is roughly six requests, and a fixture nobody asserts against is
#: budget spent for nothing.
DEFAULT_QUERIES = [
    "Daredevil 181",  # off-event find; the canonical shelf example
    "Fantastic Four 52",  # needs the series drill-down — #52 is not on the search page
    "Venom 2018 31",  # year disambiguation, and a King in Black tie-in
    "King in Black: Namor 1",  # punctuation the mirror 500s on
]

#: 60/min is the ceiling, so this sits just under it. A one-time run that takes
#: two minutes is much better than one that trips the limit and records partial
#: responses.
DELAY_S = 1.1


class _PacedTransport(RecordingTransport):
    """Recording, plus a delay between real requests and skip-if-present.

    The skip matters more than the pacing: it makes this rerunnable, so adding a
    query to the list above does not re-spend the budget on every existing one.
    """

    def __init__(self, directory: Path, delay: float = DELAY_S) -> None:
        super().__init__(directory)
        self._delay = delay
        self.skipped = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = Path(self._directory) / cassette_name(request)
        if path.exists():
            self.skipped += 1
            return response_from(path, request)
        await asyncio.sleep(self._delay)
        return await super().handle_async_request(request)


async def record(queries: list[str], directory: Path) -> int:
    transport = _PacedTransport(directory)
    async with httpx.AsyncClient(transport=transport, timeout=30.0) as http:
        mirror = MirrorClient(client=http)
        for query in queries:
            print(f"  {query}", file=sys.stderr)
            candidates = await mirror.candidates(query, limit=4)
            if not candidates:
                print("    ! no candidates — not recorded", file=sys.stderr)
                continue
            for candidate in candidates:
                # The detail call is what carries the digital id, and it is what
                # `confirm` and `enrich` replay.
                await mirror.record(candidate.issue_id)
            print(f"    {len(candidates)} candidates", file=sys.stderr)
    print(f"recorded {transport.recorded} new, reused {transport.skipped}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", default=[], help="repeatable")
    parser.add_argument("--dir", type=Path, default=DEFAULT_CASSETTE_DIR)
    args = parser.parse_args(argv)

    queries = args.query or DEFAULT_QUERIES
    print(f"{available(args.dir)} recording(s) already present", file=sys.stderr)
    return asyncio.run(record(queries, args.dir))


if __name__ == "__main__":
    sys.exit(main())
