"""Shared fixtures.

Environment bootstrap (admin key, telemetry off, the `_test` database) lives in
`tests/__init__.py`, because it has to run before `config.settings` is first
imported and a package's `__init__` is the only place guaranteed to.

Two tiers, deliberately:

- **Unit tests need nothing.** The curation gates, link rules, record parsing
  and resolution are all pure, so the data-quality suite (SPEC §8) runs with no
  Postgres and no network. That is what lets it run "before any serving code",
  which is the point of putting it early in the build order.
- **Integration tests need Postgres**, and skip cleanly when it isn't there,
  so a clone with no Docker still gets a meaningful green run.

The DB pattern is conduct's: one outer transaction per test, sessions joined to
it with `create_savepoint`, rolled back at teardown.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

FIXTURES = Path(__file__).parent / "fixtures" / "marvel"


# --- fixture payloads (no DB, no network) ---


@pytest.fixture(scope="session")
def event_comics_payload() -> dict:
    return json.loads((FIXTURES / "event_comics_page1.json").read_text())


@pytest.fixture(scope="session")
def record_index(event_comics_payload: dict) -> dict:
    from marvel.cache import record_index as build

    return build([event_comics_payload])


@pytest.fixture
def recorded_spans():
    """Install a real tracer provider that records spans in memory.

    The save/restore is deliberately against the raw module attribute rather
    than `trace.get_tracer_provider()`. That getter returns a *ProxyTracerProvider*
    when nothing is installed, and assigning the proxy back into
    `trace._TRACER_PROVIDER` makes it delegate to itself — an infinite recursion
    that surfaces much later, in an unrelated test, as
    "RecursionError: maximum recursion depth exceeded".
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous = trace._TRACER_PROVIDER  # noqa: SLF001 — the SDK offers no reset API
    trace._TRACER_PROVIDER = provider  # noqa: SLF001
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = previous  # noqa: SLF001


@pytest.fixture(scope="session")
def curated_events() -> list:
    from curation.schema import load_all_events

    return load_all_events()


# --- Postgres, when available ---


async def _can_connect() -> bool:
    from db.session import engine

    try:
        async with engine.connect():
            return True
    except Exception:  # noqa: BLE001 — asyncpg connect errors are not all SQLAlchemyError
        return False


async def _ensure_test_database() -> bool:
    """Create the `_test` database if it isn't there, then confirm we can connect.

    Returns False when there is no Postgres at all, which is how the integration
    tests come to skip cleanly rather than error.
    """
    if await _can_connect():
        return True

    from sqlalchemy.ext.asyncio import create_async_engine

    from config.settings import get_settings

    url = get_settings().database_url
    base, _, name = url.rpartition("/")
    database = name.partition("?")[0]
    admin_engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    except Exception:  # noqa: BLE001 — no server, or a race with another worker
        pass
    finally:
        await admin_engine.dispose()

    return await _can_connect()


@pytest_asyncio.fixture(scope="session")
async def schema() -> AsyncIterator[bool]:
    """Create the schema once per session, or signal that there's no DB."""
    if not await _ensure_test_database():
        yield False
        return
    import models  # noqa: F401  — registers every table on the metadata
    from db.base import Base
    from db.session import engine

    # Dropped and recreated, not just created. `create_all` adds missing tables
    # but never missing *columns*, so a test database left over from an earlier
    # revision silently drifts from the models and every query fails with
    # "column does not exist" — which reads like a code bug and is not one.
    # Safe because this is a dedicated `_test` database, never the developer's.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield True
    await engine.dispose()


@pytest_asyncio.fixture
async def db_conn(schema: bool) -> AsyncIterator[AsyncConnection]:
    if not schema:
        pytest.skip("no Postgres reachable at DATABASE_URL — run `make up` first")
    from db.session import engine
    from marvel import mirror_cache

    async with engine.connect() as conn:
        trans = await conn.begin()
        # Keep the mirror response cache inside this transaction. The transport
        # opens its own session by design — it must not borrow a caller's
        # mid-request — but in tests that means committing to the developer's
        # database, outside the rollback and surviving the run: anything going
        # through the MCP shelf tool would leave rows behind.
        #
        # Patched here rather than in an autouse fixture on purpose. An autouse
        # fixture would have to depend on `db_conn`, and `db_conn` *skips* when
        # no Postgres is reachable — which would silently skip every unit test
        # in CI's database-free `checks` job. Saved and restored by hand rather
        # than with monkeypatch, so fixture teardown ordering is untouched.
        original = mirror_cache.default_sessions
        mirror_cache.default_sessions = lambda: AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield conn
        finally:
            mirror_cache.default_sessions = original
            await trans.rollback()


@pytest_asyncio.fixture
async def session(db_conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(
        bind=db_conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    ) as db_session:
        yield db_session


@pytest_asyncio.fixture
async def user(session: AsyncSession):
    from models.user import User

    # A unique handle per fixture: it is a unique column, and it names the
    # reader's password variable, so two readers cannot share one.
    suffix = uuid4().hex[:8]
    row = User(
        email=f"reader-{suffix}@example.com",
        handle=f"reader{suffix}",
        display_name="Reader",
        is_admin=True,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest_asyncio.fixture
async def other_user(session: AsyncSession):
    from models.user import User

    suffix = uuid4().hex[:8]
    row = User(
        email=f"other-{suffix}@example.com",
        handle=f"other{suffix}",
        display_name="Other",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest_asyncio.fixture
async def loaded_event(session: AsyncSession, curated_events: list):
    """King in Black, loaded from the real curation YAML."""
    from curation.loader import load_event

    await load_event(session, curated_events[0])
    return curated_events[0]


@pytest_asyncio.fixture
async def app(db_conn: AsyncConnection):
    """The FastAPI app with `get_session` bound to the test transaction.

    The lifespan is bypassed (ASGITransport with lifespan="off"), so the boot
    -time curation load doesn't run against the real database during tests.
    """
    from db.session import get_session
    from main import app as fastapi_app

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(
            bind=db_conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = override_get_session
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def reader_password(user, monkeypatch) -> str:
    """Give the fixture reader a password, the way a deployment would."""
    password = "fixture-reader-password"  # pragma: allowlist secret
    monkeypatch.setenv(f"CROSSOVER_PASSWORD_{user.handle.upper()}", password)
    from config.settings import get_settings

    get_settings.cache_clear()
    yield password
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def signed_in(client, session, user) -> AsyncIterator:
    """A client holding a real session cookie for `user` (an admin).

    Mints a session through `auth.create_session` rather than poking a cookie
    in, so tests exercise the same path a sign-in takes. Replaced ~15 call sites
    that set the old admin-key cookie directly — which stopped being how auth
    works when sessions landed.
    """
    from sqlalchemy import select

    from auth import SESSION_COOKIE, _hash, create_session
    from csrf import CSRF_HEADER
    from models.session import UserSession

    token = await create_session(session, user)
    client.cookies.set(SESSION_COOKIE, token)

    # Carry the session's CSRF token on every request from this client, the way
    # a browser does via the hidden field or the HTMX header. Set once here so
    # each test does not have to remember it — forgetting would produce a 403
    # that looks like an authorization bug.
    row = await session.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
    client.headers[CSRF_HEADER] = row.csrf_token
    yield client


@pytest_asyncio.fixture
async def sign_in(client, session):
    """Sign in through the real form, CSRF token and all.

    The login form has no session, so its token is double-submitted from a
    cookie issued when the page is rendered — meaning a test cannot just POST.
    """

    async def _sign_in(handle: str, password: str, **extra):
        import re

        from csrf import CSRF_FIELD

        # Read the token out of the rendered form rather than the cookie. A
        # browser submits what the page contains, and the two differ when
        # signing in while already signed in — which is exactly the case that
        # broke when this helper used the cookie.
        page = (await client.get("/ui/login")).text
        match = re.search(rf'name="{CSRF_FIELD}" value="([^"]*)"', page)
        response = await client.post(
            "/ui/login",
            data={
                "handle": handle,
                "password": password,
                CSRF_FIELD: match.group(1) if match else "",
                **extra,
            },
            follow_redirects=False,
        )

        # Carry the *new* session's CSRF token, so POSTs after signing in work
        # the way they do in a browser rendering fresh forms.
        from sqlalchemy import select

        from auth import SESSION_COOKIE, _hash
        from csrf import CSRF_HEADER
        from models.session import UserSession

        cookie = client.cookies.get(SESSION_COOKIE)
        if cookie:
            row = await session.scalar(
                select(UserSession).where(UserSession.token_hash == _hash(cookie))
            )
            if row is not None:
                client.headers[CSRF_HEADER] = row.csrf_token
        return response

    return _sign_in


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
