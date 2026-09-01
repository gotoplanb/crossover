"""Test-environment bootstrap.

This runs before conftest.py and before any test module, because Python
imports a package's `__init__` before any of its submodules — which is the only
reliable place to set environment variables that must exist *before*
`config.settings` is first imported. `db/session.py` builds its engine at import
time, so by the time conftest's own imports run it is already too late.

Three things are set up here:

1. `CROSSOVER_ADMIN_KEY`, which has no default in production code (a default in
   a public repo is a published credential), so Settings would refuse to build.
2. `OTEL_ENABLED=false`, because a BatchSpanProcessor with no collector
   reachable retries in the background and makes the suite slow and noisy.
3. `DATABASE_URL` rewritten to `<dbname>_test`, so the suite never touches the
   development database. See `_as_test_database` for why that matters.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("CROSSOVER_ADMIN_KEY", "pytest-admin-key-0123456789")
os.environ["OTEL_ENABLED"] = "false"


def _load_dotenv() -> None:
    """Fold .env into the environment so the rewrite below can see DATABASE_URL.

    pydantic-settings reads .env itself, but only when Settings is constructed —
    by which point the engine exists. Deliberately not a dependency on
    python-dotenv for one loop.
    """
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _as_test_database(url: str) -> str:
    """Point the suite at `<dbname>_test`.

    **The tests get their own database on purpose.** Sharing the development one
    made results depend on whatever happened to be committed there: rows left by
    a `make load` meant `load_event` always took its update path, so the
    create-a-new-event path — the one that runs on a fresh deploy — was never
    executed; and unscoped queries in the shelf tests picked up unrelated
    bookmarks. Both bugs passed silently. Truncating the development database
    instead would destroy local work, so this forks rather than cleans.
    """
    base, _, name = url.rpartition("/")
    if not name or name.endswith("_test"):
        return url
    name, sep, query = name.partition("?")
    return f"{base}/{name}_test{sep}{query}"


_load_dotenv()
os.environ["DATABASE_URL"] = _as_test_database(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://localhost:5433/crossover")
)
