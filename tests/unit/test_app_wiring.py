"""App bootstrap wiring.

Cheap assertions on things that are easy to break silently during a refactor and
whose breakage is invisible until someone goes looking for telemetry that isn't
there, or an MCP client starts making two HTTP requests per call.
"""

from __future__ import annotations

import inspect

import main


def test_fastapi_is_instrumented_at_import_time_not_in_the_lifespan() -> None:
    """Starlette freezes the middleware stack before the lifespan body runs, so
    instrumenting there leaves OTel out of the request path entirely and the app
    emits zero server spans. This has bitten conduct; the test is the reminder.
    """
    source = inspect.getsource(main)
    instrument_at = source.index("FastAPIInstrumentor.instrument_app")
    assert "def lifespan" not in source[:instrument_at], (
        "instrument_app must run at module import, not inside a function"
    )

    lifespan_source = inspect.getsource(
        inspect.getmodule(main.lifespan) or main
    )
    assert "FastAPIInstrumentor" not in lifespan_source, (
        "FastAPI instrumentation belongs in main.py at import time, not the lifespan"
    )


def test_scrape_and_health_endpoints_are_excluded_from_tracing() -> None:
    """/metrics is scraped every 15s and /healthz by the platform. Tracing them
    buries the requests that actually matter."""
    source = inspect.getsource(main)
    assert 'excluded_urls="healthz,metrics"' in source


def test_mcp_is_mounted_with_the_trailing_slash_rewrite() -> None:
    """Claude's connector POSTs to /mcp; without the rewrite Starlette 307s to
    /mcp/ and every MCP call becomes two HTTP requests."""
    scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}
    seen = {}

    async def inner(s, receive, send):
        seen.update(s)

    import asyncio

    asyncio.run(main.MCPTrailingSlashRewrite(inner)(scope, None, None))
    assert seen["path"] == "/mcp/"
    assert seen["raw_path"] == b"/mcp/"


def test_the_rewrite_leaves_other_paths_alone() -> None:
    scope = {"type": "http", "path": "/ui/rack", "raw_path": b"/ui/rack"}
    seen = {}

    async def inner(s, receive, send):
        seen.update(s)

    import asyncio

    asyncio.run(main.MCPTrailingSlashRewrite(inner)(scope, None, None))
    assert seen["path"] == "/ui/rack"


def test_non_http_scopes_pass_through_the_rewrite() -> None:
    scope = {"type": "lifespan"}
    seen = {}

    async def inner(s, receive, send):
        seen.update(s)

    import asyncio

    asyncio.run(main.MCPTrailingSlashRewrite(inner)(scope, None, None))
    assert seen == {"type": "lifespan"}


def _all_paths(routes, seen: set[str] | None = None) -> set[str]:
    """Collect every routed path.

    Recursive because this FastAPI version keeps `include_router` results in
    `_IncludedRouter` wrappers rather than flattening them into `app.routes`,
    so a flat comprehension finds only the mounts and the root route.
    """
    seen = set() if seen is None else seen
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            seen.add(path)
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None
        )
        if nested:
            _all_paths(nested, seen)
    return seen


def test_every_router_is_registered() -> None:
    paths = _all_paths(main.app.routes)
    for expected in (
        "/healthz",
        "/metrics",
        "/oauth/token",
        "/oauth/authorize",
        "/.well-known/oauth-protected-resource",
        "/ui/rack",
        "/ui/login",
        "/",
    ):
        assert expected in paths, f"{expected} is not routed"


def test_the_mcp_app_is_mounted() -> None:
    mounts = {
        getattr(r, "path", None)
        for r in main.app.routes
        if type(r).__name__ == "Mount"
    }
    assert "/mcp" in mounts
    assert "/static" in mounts
