from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Deterministic constraint/index names, so Alembic migrations can address them
# (required for SQLite batch migrations and clean downgrades).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def install_sqlite_pragmas(target: AsyncEngine) -> None:
    """Make SQLite behave like the production database on every new connection:
    WAL journaling (readers don't block the writer), a busy timeout instead of
    immediate "database is locked" errors, and enforced foreign keys
    (SQLite ignores them unless asked)."""
    if target.dialect.name != "sqlite":
        return

    @event.listens_for(target.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # reports "memory" for :memory: databases
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)
install_sqlite_pragmas(engine)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def assert_schema_current(target: AsyncEngine | None = None) -> None:
    """Fail fast unless the database is at the Alembic head revision.

    The schema is owned by Alembic (`alembic upgrade head`); the app never
    creates or alters tables itself.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    expected = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()
    async with (target or engine).connect() as conn:
        current = await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
        )
    if current != expected:
        raise RuntimeError(
            f"Database schema is at revision {current!r} but this code expects {expected!r}. "
            "Run `alembic upgrade head` from placement-engine/ before starting the server."
        )
