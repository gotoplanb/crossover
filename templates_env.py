"""One Jinja environment for every HTML surface.

Both routes/ui.py and routes/oauth.py render pages that extend base.html, and
base.html calls `attribution()` — Marvel's terms require it wherever their data
is shown. Two separate Jinja2Templates instances meant the OAuth error page
rendered against an environment with no globals and 500'd instead of showing
its error. One environment, registered once.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from marvel.links import NOT_ON_MU, attribution, one_tap_enabled
from marvel.links import build_link as _build_link
from marvel.records import cover_url as _cover_url

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The web surface uses HTML anchors (it is a web page), but the *decision* about
# whether a link exists at all is the same Gate B decision the MCP tools make,
# so it goes through the same function.
templates.env.globals["attribution"] = attribution
templates.env.globals["cover_url"] = _cover_url
templates.env.globals["build_link"] = _build_link
templates.env.globals["one_tap_enabled"] = one_tap_enabled
templates.env.globals["NOT_ON_MU"] = NOT_ON_MU
