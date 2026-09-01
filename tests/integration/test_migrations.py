"""Migrations must actually describe the models.

The failure this prevents: someone adds a column, the test suite passes because
the test database is built with `create_all` straight from the models, and the
Heroku release phase — which runs `alembic upgrade head` — produces a schema
without that column. The app then 500s on every query touching it, in
production, at deploy time.

This builds a scratch database *through the migrations* and asserts it matches
the models, which is the only way to catch a migration that was never written.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

MIGRATION_DB = "crossover_migration_check"


def _swap_database(url: str, name: str) -> str:
    base, _, tail = url.rpartition("/")
    _, sep, query = tail.partition("?")
    return f"{base}/{name}{sep}{query}"


@pytest.fixture
async def migrated_url(schema: bool):
    """A scratch database with `alembic upgrade head` applied, dropped after."""
    if not schema:
        pytest.skip("no Postgres reachable")

    from config.settings import get_settings

    admin_url = _swap_database(get_settings().database_url, "postgres")
    target_url = _swap_database(get_settings().database_url, MIGRATION_DB)

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
        await conn.execute(text(f'CREATE DATABASE "{MIGRATION_DB}"'))
    await admin.dispose()

    try:
        yield target_url
    finally:
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
        await admin.dispose()


def _upgrade_and_diff(sync_connection, url: str) -> list:
    """Run migrations on the connection, then diff the result against the models."""
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    import models  # noqa: F401 — registers every table on the metadata
    from alembic import command
    from db.base import Base

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    # Hand alembic the live connection rather than letting env.py open its own,
    # so the upgrade and the comparison see the same database.
    config.attributes["connection"] = sync_connection
    command.upgrade(config, "head")

    context = MigrationContext.configure(sync_connection)
    return compare_metadata(context, Base.metadata)


async def test_migrations_produce_the_schema_the_models_describe(migrated_url) -> None:
    engine = create_async_engine(migrated_url)
    try:
        async with engine.begin() as conn:
            diff = await conn.run_sync(_upgrade_and_diff, migrated_url)
    finally:
        await engine.dispose()

    # `compare_metadata` returns one entry per difference. Anything here means a
    # model changed without a matching migration.
    assert diff == [], (
        "the migrations do not match the models — write a migration with "
        f"`make revision m=\"...\"`. Differences: {diff}"
    )
