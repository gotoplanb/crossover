import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all model modules so their tables register on Base.metadata.
import models  # noqa: F401
from alembic import context
from config.settings import get_settings
from db.base import Base

config = context.config

# Only configure logging when alembic owns the process (the `alembic` CLI).
# `fileConfig` disables every existing logger and replaces the root handlers, so
# doing it while embedded — the migration-drift test, or any scripted upgrade —
# silently tears down the host application's logging. Alembic's own output still
# goes to stderr either way.
_embedded = config.attributes.get("connection") is not None
if config.config_file_name is not None and not _embedded:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations, reusing an injected connection when one is supplied.

    `config.attributes["connection"]` is alembic's documented hook for driving
    migrations programmatically. Without honoring it, `command.upgrade()` called
    from inside a running event loop hits `asyncio.run() cannot be called from a
    running event loop` — which is what the migration-drift test does, and what
    any future scripted migration would do too.
    """
    injected = config.attributes.get("connection")
    if injected is not None:
        do_run_migrations(injected)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
