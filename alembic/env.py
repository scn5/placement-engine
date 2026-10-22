"""Alembic environment for placement-engine.

The database URL comes from the application settings (``DATABASE_URL`` from the
environment or ``.env``), so migrations always target the database the app
uses. For a one-off run against another database, set ``DATABASE_URL`` in that
process's environment. Tests may instead pass an open connection via
``config.attributes["connection"]`` (used to migrate in-memory databases).
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

from app.config import settings
from app.database import Base
from app.models import UTCDateTime  # importing app.models registers every table

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context):
    """Render UTCDateTime as a plain timezone-aware DateTime, so migration files
    never import application code (the UTC conversion is Python-side only)."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure_kwargs() -> dict:
    return {
        "target_metadata": target_metadata,
        "render_item": _render_item,
        "render_as_batch": True,  # SQLite needs batch mode for ALTERs; harmless elsewhere
        "compare_type": True,
    }


def run_migrations_offline() -> None:
    """Emit SQL for the configured database URL without connecting."""
    context.configure(
        url=settings.DATABASE_URL,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.DATABASE_URL, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
