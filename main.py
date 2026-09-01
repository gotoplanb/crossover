from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from config.settings import get_settings
from lifespan import lifespan
from mcp_server import build_mcp_app
from routes import health, oauth, ui
from routes import metrics as metrics_route

app = FastAPI(
    title="Crossover",
    description="Marvel event tie-in guide as an MCP service",
    version="0.1.0",
    lifespan=lifespan,
)

_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(health.router)
app.include_router(metrics_route.router)
app.include_router(oauth.router)
app.include_router(ui.router)

# Remote MCP server for Claude custom connectors, behind the OAuth bearer gate.
app.mount("/mcp", build_mcp_app())


class MCPTrailingSlashRewrite:
    """Treat /mcp as /mcp/ so MCP clients aren't bounced through a 307.

    Claude's connector POSTs to /mcp; Starlette's Mount would otherwise
    redirect, doubling every MCP call into two HTTP requests.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


app.add_middleware(MCPTrailingSlashRewrite)

# Must run at import time, not in the lifespan: Starlette freezes the middleware
# stack before the lifespan body executes, so instrumenting there leaves the
# OTel middleware out of the request path and the app emits zero server spans.
# The tracer provider itself is set later by init_tracing(); the proxy tracer
# picks it up.
if get_settings().otel_enabled:
    FastAPIInstrumentor.instrument_app(
        app,
        # /metrics is scraped every few seconds and /healthz by the platform.
        # Tracing them buries the request you actually care about.
        excluded_urls="healthz,metrics",
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/rack")
