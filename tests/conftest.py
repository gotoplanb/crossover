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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield True
    await engine.dispose()


@pytest_asyncio.fixture
async def db_conn(schema: bool) -> AsyncIterator[AsyncConnection]:
    if not schema:
        pytest.skip("no Postgres reachable at DATABASE_URL — run `make up` first")
    from db.session import engine

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
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

    row = User(email=f"reader-{uuid4().hex[:8]}@example.com", display_name="Reader")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest_asyncio.fixture
async def other_user(session: AsyncSession):
    from models.user import User

    row = User(email=f"other-{uuid4().hex[:8]}@example.com", display_name="Other")
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
async def client(app) -> AsyncIterator:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
